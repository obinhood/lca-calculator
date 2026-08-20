"""Utility-bill extraction: the validation layer, not the OCR.

The hard part of turning a utility bill into activity data is not reading the
characters — it is knowing which numbers on the page are consumption, whether the
reading is real, and whether the arithmetic closes. This module is that layer.
Character recognition is a pluggable step behind `Extractor`, deliberately, and
the reasons are set out under LICENCES below.

THE GRAIN IS PER SUPPLY POINT PER REGISTER PER PERIOD, NEVER PER BILL. One PDF
can carry several supply points and several registers, and a bill-level record is
structurally wrong before any number is read.

WHAT MAKES A NUMBER TRUSTWORTHY HERE IS ARITHMETIC, NOT A MODEL SCORE. Every
extraction is checked against relations that must hold on a correct bill: the
registers sum to the stated total; quantity times rate plus standing charge times
days equals the subtotal; subtotal plus VAT equals the total; the period length
matches the stated day count; gas volume converted at the bill's own calorific
value equals the stated kWh; the MPAN passes its mod-11 check. A field the
arithmetic contradicts is REJECTED whatever confidence the extractor attached to
it.

FIVE TRAPS, each of which silently changes a reported tonne:

1. A SCANNED PDF RETURNS EMPTY TEXT, NOT AN ERROR. The zero flows straight
   through. Worse is a text layer with no ToUnicode CMap, which returns garbage
   that parses. Both are detected before extraction rather than after.

2. kVArh, kVA AND kW SIT IN THE SAME TABLE AS kWh, with numeric values and
   k-prefixed units. Reactive energy has no emission factor and maximum demand is
   a rate, not a quantity. This is the commonest false positive in Scope 2
   extraction, so those units are rejected by name.

3. ON CT/PT-METERED SUPPLIES THE REGISTER DIFFERENCE IS NOT THE CONSUMPTION. A
   200:5 current transformer multiplies by 40; with a 2.4:1 potential transformer,
   by 96. Missing the multiplier under-reports a large site by 40 to 100 times.

4. ESTIMATED-VERSUS-ACTUAL IS PER REGISTER, NOT PER BILL. One bill routinely
   carries one actual and one estimated register, so a boolean on the bill loses
   the distinction. Read quality is modelled on the ESPI enumeration and
   `is_estimated` is DERIVED from it, never stored beside it.

5. THE CATCH-UP BILL AFTER AN ESTIMATED PERIOD RESTATES THE EARLIER ONE. Adding
   it double counts; overwriting it destroys the corrected-alongside-original
   record SECR requires. It is modelled as supersession with lineage.

LICENCES, because getting these backwards either bans a safe library or ships a
copyleft one into a hosted service. pdfplumber is MIT — the claim that it is AGPL
is a widely repeated error. PyMuPDF is the AGPL one. OCRmyPDF's own licence is
MPL-2.0 but it shells out to Ghostscript, which is AGPL, so the Python package is
clean and the container is not. docTR's headline Apache-2.0 is undermined by
GPLv2+ transitive dependencies. Surya is GPL-3.0 code with non-commercial
weights. None is imported here; the operator chooses.
"""
import hashlib
import math
import re
from typing import Optional, Protocol

EXTRACTION_VERSION = "bx-v1"

# Below this many extractable characters per page the document is scanned and
# needs OCR. A scanned PDF returns EMPTY text rather than raising, so this must be
# checked rather than relied upon to fail.
TEXT_LAYER_MIN_CHARS_PER_PAGE = 100
# A text layer whose glyph-to-unicode mapping is broken returns garbage that
# parses. Above this share of replacement characters, treat it as unusable.
MAX_REPLACEMENT_CHAR_RATIO = 0.05

# Provenance tiers, best first. An OCR-derived row must never inherit a
# text-layer row's confidence.
PROVENANCE_TIERS = ("embedded_xml", "text_layer", "ocr")

# PDF/A-3 attachments carrying a structured invoice. If one is present it is read
# byte-for-byte and the document is never OCR'd.
EMBEDDED_INVOICE_ATTACHMENTS = ("factur-x.xml", "zugferd-invoice.xml",
                                "xrechnung.xml")

# ESPI QualityOfReading. Modelled as an enumeration rather than a boolean because
# "estimated" is one of several ways a reading can be less than a real read.
READ_QUALITY = {
    0: "valid", 7: "manually edited", 8: "estimated (reference day)",
    9: "estimated (interpolation)", 10: "questionable", 11: "derived",
    12: "projected", 13: "mixed", 14: "raw", 15: "weather normalized",
    17: "validated", 18: "verified", 19: "revenue quality",
}
ESTIMATED_QUALITIES = {8, 9, 12}
# Qualities that are not a measured read of the period.
NON_ACTUAL_QUALITIES = ESTIMATED_QUALITIES | {10, 11, 13}

# Units that appear beside kWh on the same table and are NOT activity data.
REJECTED_UNITS = {
    "kvarh": "reactive energy — carries no emission factor",
    "kvar": "reactive power — a rate, not a quantity",
    "kva": "apparent power / available capacity — a rate, not a quantity",
    "kw": "maximum demand — a rate, not a quantity",
    "mw": "maximum demand — a rate, not a quantity",
}
ACCEPTED_UNITS = {"kwh", "mwh", "m3", "m³", "therm", "kg", "litre", "l"}

# UK gas volume to energy. The calorific value is printed on the bill and must be
# read from it — substituting a default silently rewrites the figure.
GAS_VOLUME_CORRECTION = 1.02264
GAS_KWH_DIVISOR = 3.6
# MPAN mod-11 primes, applied to the 13-digit core (the bottom line).
MPAN_PRIMES = (3, 5, 7, 13, 17, 19, 23, 29, 31, 37, 41, 43)

# Arithmetic must close to this relative tolerance before a field is trusted.
RECONCILIATION_TOLERANCE = 0.005


class Extractor(Protocol):
    """Whatever turns document bytes into text and attachments.

    Deliberately not implemented here. The operator chooses the library, and the
    licence position differs sharply between them — see the module docstring.
    """

    def text_pages(self, data: bytes) -> list: ...

    def attachments(self, data: bytes) -> dict: ...


def document_hash(data: bytes) -> str:
    """SHA-256 of the original bytes. Every derived row carries it."""
    return hashlib.sha256(data).hexdigest()


def triage(pages: list, attachments: Optional[dict] = None) -> dict:
    """Decide how a document should be read, BEFORE reading it.

    The check that matters: a scanned PDF returns empty text rather than raising,
    so an unguarded pipeline turns it into a silent zero.
    """
    attachments = attachments or {}
    embedded = [n for n in EMBEDDED_INVOICE_ATTACHMENTS
                if n in {k.lower() for k in attachments}]
    if embedded:
        return {
            "route": "embedded_xml", "provenance_tier": "embedded_xml",
            "attachment": embedded[0], "pages": len(pages),
            "note": "A PDF/A-3 structured invoice is present (UN/CEFACT CII, "
                    "EN 16931). It is read byte-for-byte and the document is never "
                    "OCR'd — recognising characters that are already machine "
                    "readable can only lose information.",
        }

    total_chars = sum(len(p or "") for p in pages)
    per_page = total_chars / len(pages) if pages else 0
    replacement = sum((p or "").count("�") for p in pages)
    replacement_ratio = replacement / total_chars if total_chars else 0.0

    if replacement_ratio > MAX_REPLACEMENT_CHAR_RATIO:
        return {
            "route": "ocr", "provenance_tier": "ocr", "pages": len(pages),
            "chars_per_page": round(per_page, 1),
            "replacement_char_ratio": round(replacement_ratio, 4),
            "note": "A text layer is present but its glyph-to-unicode mapping is "
                    "broken (no ToUnicode CMap). This is WORSE than a scan: it "
                    "returns garbage that parses, so it is routed to OCR rather "
                    "than trusted.",
        }
    if per_page < TEXT_LAYER_MIN_CHARS_PER_PAGE:
        return {
            "route": "ocr", "provenance_tier": "ocr", "pages": len(pages),
            "chars_per_page": round(per_page, 1),
            "note": f"Fewer than {TEXT_LAYER_MIN_CHARS_PER_PAGE} extractable "
                    f"characters per page: this is a scan. A scanned PDF returns "
                    f"EMPTY text rather than an error, so an unguarded pipeline "
                    f"would emit a silent zero.",
        }
    return {"route": "text_layer", "provenance_tier": "text_layer",
            "pages": len(pages), "chars_per_page": round(per_page, 1),
            "replacement_char_ratio": round(replacement_ratio, 4), "note": None}


def unit_verdict(unit: Optional[str]) -> dict:
    """Whether a unit on the bill is activity data at all."""
    u = (unit or "").strip().lower()
    if not u:
        return {"accepted": False, "reason": "no unit; units are never guessed"}
    if u in REJECTED_UNITS:
        return {"accepted": False, "unit": u, "reason": REJECTED_UNITS[u],
                "note": "This sits in the same table as kWh with a numeric value and "
                        "a k-prefixed unit, and is the commonest false positive in "
                        "Scope 2 extraction."}
    if u in ACCEPTED_UNITS:
        return {"accepted": True, "unit": u}
    return {"accepted": False, "unit": u,
            "reason": "unrecognised unit — raised for review rather than converted"}


def mpan_valid(mpan_core: str) -> dict:
    """Mod-11 check on the 13-digit MPAN core.

    The core is the BOTTOM line. The 8-digit top line is not the identifier and
    treating it as one silently mis-keys a supply point.
    """
    digits = re.sub(r"\D", "", mpan_core or "")
    if len(digits) != 13:
        return {"valid": False,
                "reason": f"an MPAN core is 13 digits (the bottom line); got "
                          f"{len(digits)}. The 8-digit top line is not the "
                          f"identifier."}
    total = sum(int(d) * p for d, p in zip(digits[:12], MPAN_PRIMES))
    check = (total % 11) % 10
    return {"valid": check == int(digits[12]), "core": digits,
            "expected_check_digit": check, "given_check_digit": int(digits[12])}


def consumption_from_registers(previous: float, current: float, *,
                               multiplier: float = 1.0,
                               dial_count: int = 5) -> dict:
    """(current - previous) x multiplier, with rollover handled explicitly.

    The multiplier is not optional on CT/PT-metered commercial supplies: a 200:5
    current transformer multiplies by 40, and with a 2.4:1 potential transformer
    by 96. Omitting it under-reports a large site by 40 to 100 times.
    """
    if not all(isinstance(v, (int, float)) and math.isfinite(v)
               for v in (previous, current, multiplier)):
        return {"determinable": False,
                "reason": "readings and multiplier must be finite numbers"}
    if multiplier <= 0:
        return {"determinable": False, "reason": "multiplier must be > 0"}

    rolled = current < previous
    diff = (current + 10 ** dial_count - previous) if rolled else (current - previous)
    return {
        "determinable": True,
        "register_difference": diff,
        "multiplier": multiplier,
        "consumption": diff * multiplier,
        "rollover_applied": rolled,
        "rollover_note": (
            f"current < previous, so the meter rolled over a {dial_count}-dial "
            f"register; 10^{dial_count} was added rather than producing a negative "
            f"consumption") if rolled else None,
        "multiplier_note": (
            "multiplier is 1.0 — confirm this is a directly-metered supply. On a "
            "CT/PT-metered commercial supply the register difference is NOT the "
            "consumption.") if multiplier == 1.0 else None,
    }


def gas_volume_to_kwh(cubic_metres: float, calorific_value: float, *,
                      cv_basis: str = "gross") -> dict:
    """m3 -> kWh at the bill's OWN calorific value.

    The CV is printed on the specific bill and varies by day and region.
    Substituting a default silently rewrites the figure. UK Government factors and
    UK bills are both gross-CV, so the basis is recorded rather than assumed.
    """
    if not all(isinstance(v, (int, float)) and math.isfinite(v) and v > 0
               for v in (cubic_metres, calorific_value)):
        return {"determinable": False,
                "reason": "volume and calorific value must be positive finite "
                          "numbers; the CV must come from the bill, never a default"}
    if cv_basis not in ("gross", "net"):
        return {"determinable": False, "reason": "cv_basis must be 'gross' or 'net'"}
    kwh = cubic_metres * GAS_VOLUME_CORRECTION * calorific_value / GAS_KWH_DIVISOR
    return {"determinable": True, "kwh": kwh, "cv_basis": cv_basis,
            "calorific_value": calorific_value,
            "volume_correction": GAS_VOLUME_CORRECTION,
            "note": "UK Government conversion factors and UK bills are both "
                    "GROSS-CV. Mixing a net-CV figure into a gross-CV factor "
                    "understates by about 10%."}


def _close(a: Optional[float], b: Optional[float]) -> Optional[bool]:
    if a is None or b is None:
        return None
    if not (math.isfinite(a) and math.isfinite(b)):
        return None
    scale = max(abs(a), abs(b), 1e-9)
    return abs(a - b) <= scale * RECONCILIATION_TOLERANCE


def reconcile(bill: dict) -> dict:
    """Check the arithmetic that must hold on a correct bill.

    This is the confidence layer. A field the arithmetic contradicts is rejected
    whatever score an extractor attached to it — a model's certainty about a
    misread digit is worth nothing against a sum that does not close.
    """
    checks = []

    def check(name, ok, detail):
        checks.append({"check": name,
                       "result": ("pass" if ok else "fail") if ok is not None
                                 else "not_assessable",
                       "detail": detail})

    registers = bill.get("registers") or []
    reg_sum = sum(r.get("consumption") or 0.0 for r in registers) if registers else None
    check("registers_sum_to_total", _close(reg_sum, bill.get("total_consumption")),
          f"registers {reg_sum} vs stated {bill.get('total_consumption')}")

    qty, rate = bill.get("total_consumption"), bill.get("unit_rate")
    standing, days = bill.get("standing_charge"), bill.get("days")
    computed_subtotal = None
    if None not in (qty, rate, standing, days):
        computed_subtotal = qty * rate + standing * days
    check("line_items_sum_to_subtotal",
          _close(computed_subtotal, bill.get("subtotal")),
          f"qty*rate + standing*days = {computed_subtotal} vs stated "
          f"{bill.get('subtotal')}")

    sub, vat, total = bill.get("subtotal"), bill.get("vat"), bill.get("total")
    check("subtotal_plus_vat_equals_total",
          _close(None if None in (sub, vat) else sub + vat, total),
          f"{sub} + {vat} vs stated {total}")

    start, end, stated_days = bill.get("period_start"), bill.get("period_end"), days
    computed_days = _inclusive_days(start, end)
    check("period_length_matches_day_count",
          _close(computed_days, stated_days),
          f"{start}..{end} inclusive = {computed_days} vs stated {stated_days}")

    vol, cv = bill.get("gas_volume_m3"), bill.get("calorific_value")
    if vol and cv:
        g = gas_volume_to_kwh(vol, cv)
        check("gas_volume_converts_to_stated_kwh",
              _close(g.get("kwh"), bill.get("total_consumption")),
              f"{vol} m3 at CV {cv} = {g.get('kwh')} kWh vs stated "
              f"{bill.get('total_consumption')}")

    if bill.get("mpan_core"):
        m = mpan_valid(bill["mpan_core"])
        check("mpan_check_digit", m["valid"], m.get("reason") or
              f"expected {m.get('expected_check_digit')}, got "
              f"{m.get('given_check_digit')}")

    failed = [c for c in checks if c["result"] == "fail"]
    unassessed = [c for c in checks if c["result"] == "not_assessable"]
    return {
        "version": EXTRACTION_VERSION,
        "checks": checks,
        "passed": len(checks) - len(failed) - len(unassessed),
        "failed": len(failed), "not_assessable": len(unassessed),
        "reconciles": not failed,
        "trustworthy": not failed and not unassessed,
        "note": "A field the arithmetic contradicts is REJECTED whatever confidence "
                "the extractor attached to it. A not_assessable check is not a pass — "
                "it means the inputs to that relation were not all present.",
    }


def _inclusive_days(start: Optional[str], end: Optional[str]) -> Optional[int]:
    """Inclusive day count.

    ESPM's endDate is inclusive ("last date of the reading period") while ESPI's
    billingPeriod is a start instant plus a duration in seconds. Mixing the two
    creates systematic off-by-one errors and false gap and overlap flags.
    """
    from datetime import datetime
    try:
        a = datetime.strptime((start or "")[:10], "%Y-%m-%d")
        b = datetime.strptime((end or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return (b - a).days + 1 if b >= a else None


def read_quality(code: Optional[int]) -> dict:
    """Interpret an ESPI QualityOfReading code.

    Per REGISTER, never per bill: one bill routinely carries one actual and one
    estimated register, and a boolean on the bill loses that.
    """
    if code is None:
        return {"known": False, "code": None, "is_estimated": None,
                "is_actual": None,
                "note": "no read quality recorded; whether this is a real read is "
                        "UNKNOWN, which is not the same as actual"}
    if code not in READ_QUALITY:
        return {"known": False, "code": code, "is_estimated": None,
                "is_actual": None,
                "note": f"{code} is not an ESPI QualityOfReading value"}
    return {
        "known": True, "code": code, "label": READ_QUALITY[code],
        # DERIVED, never stored beside the code — otherwise the two can disagree.
        "is_estimated": code in ESTIMATED_QUALITIES,
        "is_actual": code not in NON_ACTUAL_QUALITIES,
        "note": "is_estimated is derived from the quality code, never stored "
                "independently of it.",
    }


def supersession(previous_bill: dict, catch_up_bill: dict) -> dict:
    """A catch-up bill RESTATES the estimated period it follows.

    Adding it double counts. Overwriting it destroys the corrected-figure-alongside
    -the-original record SECR requires. It is modelled as supersession with
    lineage, so both figures survive and only one is counted.
    """
    prev_end = previous_bill.get("period_end")
    new_start = catch_up_bill.get("period_start")
    overlaps = bool(prev_end and new_start and new_start <= prev_end)
    if not overlaps:
        return {"supersedes": False,
                "note": "the periods do not overlap; this is a subsequent bill, not "
                        "a restatement"}
    return {
        "supersedes": True,
        "superseded_period": {"start": previous_bill.get("period_start"),
                              "end": prev_end,
                              "consumption": previous_bill.get("total_consumption"),
                              "read_quality": previous_bill.get("read_quality")},
        "restating_period": {"start": new_start,
                             "end": catch_up_bill.get("period_end"),
                             "consumption": catch_up_bill.get("total_consumption"),
                             "read_quality": catch_up_bill.get("read_quality")},
        "action": "mark the earlier estimate superseded and count only the "
                  "restatement",
        "note": "Adding the catch-up bill to the estimate double counts the same "
                "energy. Overwriting the estimate destroys the corrected-alongside-"
                "original record SECR requires. Both rows are retained and only the "
                "restatement is counted.",
    }


def to_activity_rows(bill: dict, *, doc_hash: str, provenance_tier: str) -> dict:
    """Candidate activity rows — one per supply point per register per period.

    Every row carries the document hash and the provenance tier, so an OCR-derived
    figure can never be mistaken for one read from a structured attachment. Rows
    are SUGGESTIONS: nothing enters an inventory without review, and the
    reconciliation verdict travels with them.
    """
    if provenance_tier not in PROVENANCE_TIERS:
        return {"rows": [], "reason": f"provenance_tier must be one of "
                                      f"{list(PROVENANCE_TIERS)}"}
    recon = reconcile(bill)
    rows, rejected = [], []
    for r in (bill.get("registers") or []):
        u = unit_verdict(r.get("unit"))
        if not u["accepted"]:
            rejected.append({"register": r.get("register_id"), "unit": r.get("unit"),
                             "reason": u["reason"]})
            continue
        q = read_quality(r.get("read_quality"))
        rows.append({
            "supply_point": bill.get("mpan_core") or bill.get("supply_point"),
            "register_id": r.get("register_id"),
            "period_start": bill.get("period_start"),
            "period_end": bill.get("period_end"),
            "quantity": r.get("consumption"),
            "unit": u["unit"],
            "read_quality": q,
            "provenance_tier": provenance_tier,
            "source_document_hash": doc_hash,
            "page": r.get("page"),
            "bounding_box": r.get("bounding_box"),
            "reconciles": recon["reconciles"],
        })
    return {
        "rows": rows, "rejected_registers": rejected,
        "reconciliation": recon,
        "grain": "one row per supply point per register per period",
        "note": "These are SUGGESTIONS. Nothing enters an inventory unreviewed, and "
                "the reconciliation verdict travels with the rows so a reviewer sees "
                "whether the bill's own arithmetic closed.",
    }
