"""Pre-calculation screening: an assurance-grade exception register.

Every carbon platform advertises "anomaly detection". The differentiator is not
better outlier maths — it is that each finding carries a stated EXPECTATION, a
stated THRESHOLD, and an auditable DISPOSITION, because that is what an assuror
actually tests. PCAOB Staff Audit Practice Alert No. 11 is blunt about the
alternative: "Verifying that a review was signed off provides little or no
evidence by itself about the control's effectiveness."

So this is built as the MISSTATEMENT LEDGER that ISAE 3410 (paras 50-56) and
ISSA 5000 (paras 153-161) require a practitioner to assemble by hand. ISSA 5000
para 161 requires the engagement file to contain "all misstatements accumulated
during the engagement, other than those that are clearly trivial ... and whether
they have been corrected". Every finding therefore carries a quantified effect,
a correction-or-acceptance decision, and a running total of UNCORRECTED effect
against materiality.

WHAT THIS DELIBERATELY DOES NOT DO, AND WHY
The obvious check — the GHG Protocol Corporate Standard chapter 7 rule that
"changes of over 10 percent from year to year may warrant further
investigation" — is NOT implemented, because this schema cannot support it
honestly. ActivityRecord carries no series identifier: no metering point, site,
asset or account. Two physically distinct sites are indistinguishable rows, and
the platform's own demo data proves it — HQ and workshop electricity share
category, subcategory, unit, geography and entity, and differ only in a free-text
description. Any series key built from the available columns would merge them and
report their sum as one trend, so a real 40% jump at one site would vanish inside
a flat total, and a site opening would read as a data error. Worse, only
`activity_category` and `activity_unit` are frozen onto the line item, so a series
keyed on anything else would have to join live ActivityRecord — the exact
live-vs-frozen defect the report renderers exist to prevent. Period-over-period
screening needs a series identifier first, and the honest form of one is
DECLARED rather than inferred: a nullable preparer-supplied key that the engine
never writes back, exactly as `scope` and `ghgp_category` work, where NULL means
"not enrolled" and the screen reports the unenrolled share by name. That is a
schema change and its own piece of work; inventing the inference here would
produce precisely the confidently-wrong number this platform exists to avoid.

WHAT IT DOES DO is the deterministic layer, which needs no series identity and
which the literature agrees catches most real defects: non-physical values,
units outside the allow-list for a category, duplicate and overlapping rows, and
the unit-signature test. That last one is the highest-value check in the set —
where two otherwise-identical rows differ by a factor within tolerance of a known
conversion constant (1000, 3600, 293.1 ...), the diagnosis is near-certain and it
names the remedy, rather than handing a reviewer an unexplainable score.

NO STATISTICS ON RAW LEVELS. A classical z-score cannot do this job at all:
Shiffler (1988) proves max|z| = (n-1)/sqrt(n), so with twelve monthly points no
observation can ever exceed 3.175, and with ten or fewer nothing can exceed 3. A
three-sigma rule on a year of monthly data is not conservative, it is blind.
"""
import hashlib
import json
import math
from collections import defaultdict
from typing import Optional

from sqlalchemy.orm import Session

from ..models import ActivityFinding, ActivityRecord, EmissionFactor

SCREENING_VERSION = "scr-v1"

# ISAE 3410 A112 / ISSA 5000 A470: "clearly trivial" is NOT another expression for
# "not material" — trivial items are of a wholly different, smaller order of
# magnitude. 5% of the inventory is the customary materiality in GHG verification
# practice; the trivial floor sits an order of magnitude below it so the register
# does not drown in noise. A register full of trivia trains its reader to click
# through, which is worse than no register.
DEFAULT_MATERIALITY_PCT = 5.0
DEFAULT_TRIVIAL_FLOOR_PCT = 0.25

# Ratios that betray a unit error rather than a real change. Tolerance is +/-2%.
#
# NEAR-UNITY CONSTANTS ARE DELIBERATELY ABSENT, and this is the most important
# decision in the module. A first cut included 1.10231 (tonne<->US short ton),
# 1.60934 (mile<->km) and 2.20462 (kg<->lb). It then flagged a perfectly ordinary
# month-on-month move from 1,000 to 1,100 kWh as a probable unit error, because 1.1
# sits 0.2% from 1.10231. Ratios below about 3 are the ordinary weather-and-occupancy
# range of a metered series; a constant living in that range cannot distinguish a
# unit error from a normal month, and a check that fires on normal months trains its
# reader to click through — the precise failure ISAE 3410 A112 warns against.
#
# Severity is graded by how implausible an operational explanation is:
#   >= HIGH_CONFIDENCE_RATIO  -> a real change of this size is barely credible: high
#   below it                  -> a seasonal swing explains it too: medium, and the
#                                finding says so rather than asserting a diagnosis.
UNIT_SIGNATURES = {
    1000.0: "kWh<->MWh, g<->kg, kg<->tonne, L<->m3 (x1000)",
    3600.0: "kWh<->GJ",
    293.1: "kWh<->MMBtu",
    35.3147: "m3<->ft3",
    29.3: "kWh<->therm",
    3.78541: "US gallon<->litre",
    3.6: "kWh<->MJ",
}
UNIT_SIGNATURE_TOLERANCE = 0.02

# Below this the check is not run at all: no conversion constant this small can be
# told apart from ordinary operational variation.
MIN_DIAGNOSTIC_RATIO = 3.0
# At or above this, an operational explanation stops being credible and the match
# is treated as a diagnosis rather than a prompt.
HIGH_CONFIDENCE_RATIO = 10.0

# Units a category may legitimately be recorded in. A unit outside its category's
# list is not necessarily wrong — the list is not exhaustive across every industry
# — so this raises a finding for a human, never an automatic correction. Categories
# absent from the map are not screened on this check at all, which is reported as
# coverage rather than passed silently.
CATEGORY_UNIT_ALLOWLIST = {
    "electricity": {"kWh", "MWh", "GWh", "J", "MJ", "GJ"},
    "gas": {"kWh", "MWh", "m3", "MJ", "GJ", "therm"},
    "natural_gas": {"kWh", "MWh", "m3", "MJ", "GJ", "therm"},
    "diesel": {"L", "litre", "liter", "m3", "kg", "tonne", "kWh"},
    "petrol": {"L", "litre", "liter", "m3", "kg", "tonne", "kWh"},
    "gasoline": {"L", "litre", "liter", "m3", "kg", "tonne", "kWh"},
    "flight": {"km", "mile", "pkm"},
    "train": {"km", "mile", "pkm"},
    "car": {"km", "mile", "pkm"},
    "freight": {"tkm"},
    "waste": {"kg", "tonne", "t"},
    "water": {"m3", "L", "litre", "liter"},
}

# A category whose consumption cannot physically be zero for an operating site.
# Zero is still only informational — a genuinely vacant site reads zero — but it
# is worth a reviewer's glance.
METERED_CATEGORIES = {"electricity", "gas", "natural_gas", "water"}

SEVERITIES = ("blocking", "high", "medium", "informational")

CHECK_CODES = (
    "non_physical_quantity",
    "zero_on_metered_category",
    "missing_unit",
    "unit_not_allowed_for_category",
    "duplicate_row",
    "overlapping_coverage_window",
    "unit_signature",
)

# A closed vocabulary, so accepted findings stay queryable instead of decaying
# into free text nobody can aggregate.
DISPOSITION_REASON_CODES = (
    "genuine_operational_change",
    "corrected_at_source",
    "restated_prior_period",
    "unit_error_fixed",
    "boundary_change",
    "benchmark_not_applicable",
    "accepted_immaterial",
)

FINDING_STATUSES = ("open", "corrected", "accepted", "superseded")


def _norm(v) -> str:
    return (str(v).strip().lower() if v is not None else "")


def finding_key(check_code: str, activity_ids, detail: str = "") -> str:
    """A STABLE identity for a finding, so re-screening updates rather than duplicates.

    Keyed on the check and the activities it concerns — never on the detected
    time or a row id — so the same defect found twice is the same finding, and a
    disposition made last month still attaches to it.
    """
    ids = ",".join(str(i) for i in sorted(activity_ids))
    blob = f"{SCREENING_VERSION}|{check_code}|{ids}|{detail}"
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _effect_kg(db: Session, activity: ActivityRecord) -> Optional[float]:
    """The emissions at risk on this row, or None when it cannot be determined.

    None is not zero. A row with no factor bound, or an unconvertible unit, has an
    UNKNOWN effect, and the accumulated-uncorrected total must say so rather than
    quietly treating it as nil — an unquantified misstatement is exactly the kind
    an assuror most wants flagged.
    """
    if activity.factor_id is None or activity.quantity is None:
        return None
    if not isinstance(activity.quantity, (int, float)) or not math.isfinite(activity.quantity):
        return None
    factor = db.get(EmissionFactor, activity.factor_id)
    if factor is None or factor.value is None or not math.isfinite(factor.value):
        return None
    from .units import convert, UnitConversionError
    try:
        qty = convert(abs(activity.quantity), activity.unit, factor.unit)
    except UnitConversionError:
        return None
    return abs(qty * factor.value)


def _mk(check_code, severity, activity_ids, expectation, threshold, observed,
        detail_key=""):
    return {
        "check_code": check_code, "severity": severity,
        "activity_ids": sorted(activity_ids),
        "expectation": expectation, "threshold": threshold, "observed": observed,
        "finding_key": finding_key(check_code, activity_ids, detail_key),
    }


def _check_row_level(acts: list) -> list:
    """Deterministic per-row checks. No statistics, no expectation model."""
    out = []
    for a in acts:
        q = a.quantity
        if q is None or not isinstance(q, (int, float)) or not math.isfinite(q):
            out.append(_mk("non_physical_quantity", "blocking", [a.id],
                           "a finite numeric quantity",
                           "quantity is not None and math.isfinite(quantity)",
                           f"quantity={q!r}"))
            continue
        if q < 0:
            out.append(_mk("non_physical_quantity", "blocking", [a.id],
                           "consumption is non-negative",
                           "quantity >= 0", f"quantity={q}"))
            continue
        if q == 0 and _norm(a.category) in METERED_CATEGORIES:
            out.append(_mk("zero_on_metered_category", "informational", [a.id],
                           f"a metered {a.category} row usually carries non-zero "
                           f"consumption",
                           "quantity > 0 for a metered category",
                           "quantity=0 — confirm the site was genuinely vacant or "
                           "the meter genuinely read nil"))

        unit = (a.unit or "").strip()
        if not unit:
            out.append(_mk("missing_unit", "blocking", [a.id],
                           "every quantity carries a unit",
                           "unit is non-empty",
                           "unit is missing — units are never guessed, so this row "
                           "will fail unit conversion until corrected"))
            continue

        allowed = CATEGORY_UNIT_ALLOWLIST.get(_norm(a.category))
        if allowed and unit not in allowed:
            out.append(_mk("unit_not_allowed_for_category", "high", [a.id],
                           f"{a.category} is normally recorded in one of "
                           f"{sorted(allowed)}",
                           "unit in the category allow-list",
                           f"unit={unit!r} — gas billed in kWh versus m3 is a silent "
                           f"order-of-magnitude difference, so this is raised for a "
                           f"human rather than converted"))
    return out


def _row_signature(a: ActivityRecord) -> tuple:
    """Everything that identifies a row EXCEPT its quantity."""
    return (a.entity_id, _norm(a.category), _norm(a.subcategory), _norm(a.geo),
            _norm(a.unit), (a.date or ""), _norm(a.description))


def _check_duplicates(acts: list) -> list:
    """Exact duplicates across the WHOLE activity set, not just one upload.

    services/qa.py already flags duplicates inside a single uploaded DataFrame.
    That misses the far more common defect: the same invoice uploaded twice in
    two files, months apart, which double counts.
    """
    out = []
    groups = defaultdict(list)
    for a in acts:
        if a.quantity is None:
            continue
        groups[_row_signature(a) + (round(float(a.quantity), 9),)].append(a)
    for _, rows in groups.items():
        if len(rows) < 2:
            continue
        ids = [r.id for r in rows]
        out.append(_mk("duplicate_row", "high", ids,
                       "one row per (date, category, subcategory, unit, geography, "
                       "description, quantity)",
                       "no exact repeat across the whole activity set",
                       f"{len(rows)} identical rows — the same invoice uploaded twice "
                       f"double counts; qa.py only sees repeats WITHIN one upload"))
    return out


def _check_unit_signature(acts: list) -> list:
    """Two otherwise-identical rows whose quantities differ by a conversion constant.

    The highest-precision check in the set. A generic "this is 40x the others"
    flag is weak evidence; "these two rows are identical except one is exactly
    1000x the other, which is the kWh<->MWh constant" is a near-certain diagnosis
    that names its own remedy and can be re-performed by an assuror by hand.
    """
    out = []
    groups = defaultdict(list)
    for a in acts:
        if a.quantity is None or not isinstance(a.quantity, (int, float)):
            continue
        if not math.isfinite(a.quantity) or a.quantity <= 0:
            continue
        # Deliberately excludes `date` from the signature: a unit error usually
        # shows up as one month recorded in the wrong unit alongside its siblings.
        groups[(a.entity_id, _norm(a.category), _norm(a.subcategory),
                _norm(a.geo), _norm(a.unit))].append(a)

    for _, rows in groups.items():
        if len(rows) < 2:
            continue
        qs = sorted(rows, key=lambda r: r.quantity)
        lo, hi = qs[0], qs[-1]
        if lo.quantity <= 0:
            continue
        ratio = hi.quantity / lo.quantity
        if ratio < MIN_DIAGNOSTIC_RATIO:
            # Ordinary operational variation. No constant this small is diagnostic.
            continue
        for const, label in UNIT_SIGNATURES.items():
            if abs(ratio - const) <= const * UNIT_SIGNATURE_TOLERANCE:
                confident = ratio >= HIGH_CONFIDENCE_RATIO
                out.append(_mk(
                    "unit_signature", "high" if confident else "medium", [lo.id, hi.id],
                    f"rows sharing category, subcategory, unit and geography differ "
                    f"by an operational amount, not by a unit-conversion constant",
                    f"ratio >= {MIN_DIAGNOSTIC_RATIO} and "
                    f"|ratio - {const}| <= {UNIT_SIGNATURE_TOLERANCE:.0%} of {const}",
                    (f"ratio={ratio:.4f} is within tolerance of {const} ({label}) — "
                     f"activity {hi.id} is probably recorded in the wrong unit rather "
                     f"than genuinely {ratio:.0f}x activity {lo.id}")
                    if confident else
                    (f"ratio={ratio:.4f} is within tolerance of {const} ({label}), which "
                     f"MAY mean activity {hi.id} is recorded in the wrong unit — but a "
                     f"{ratio:.1f}x seasonal or operational swing explains it equally "
                     f"well. Check the unit label; this is a prompt, not a diagnosis"),
                    detail_key=f"{const}"))
                break
    return out


def _overlaps(a: ActivityRecord, b: ActivityRecord) -> bool:
    if not (a.coverage_start and a.coverage_end and b.coverage_start and b.coverage_end):
        return False
    return a.coverage_start <= b.coverage_end and b.coverage_start <= a.coverage_end


def _check_coverage_overlap(acts: list) -> list:
    """Two declared consumption windows for the same series that overlap in time.

    An overlapping pair double counts the shared days, and the proration in
    calc.coverage_overlap will apply to both.
    """
    out = []
    groups = defaultdict(list)
    for a in acts:
        if a.coverage_start and a.coverage_end:
            groups[(a.entity_id, _norm(a.category), _norm(a.subcategory),
                    _norm(a.geo), _norm(a.description))].append(a)
    for _, rows in groups.items():
        rows = sorted(rows, key=lambda r: (r.coverage_start or "", r.id))
        for i in range(len(rows) - 1):
            for j in range(i + 1, len(rows)):
                if _overlaps(rows[i], rows[j]):
                    out.append(_mk(
                        "overlapping_coverage_window", "high",
                        [rows[i].id, rows[j].id],
                        "declared consumption windows for the same series do not overlap",
                        "coverage windows are disjoint",
                        f"{rows[i].coverage_start}..{rows[i].coverage_end} overlaps "
                        f"{rows[j].coverage_start}..{rows[j].coverage_end} — the shared "
                        f"days are counted in both rows"))
    return out


def screen(db: Session, organisation_id: int, *,
           materiality_pct: float = DEFAULT_MATERIALITY_PCT,
           trivial_floor_pct: float = DEFAULT_TRIVIAL_FLOOR_PCT,
           now: Optional[str] = None) -> dict:
    """Run every deterministic check over an organisation's activities.

    Findings are persisted by stable key: an existing finding keeps its
    disposition, a resolved defect that reappears re-opens, and a defect that has
    gone away is marked superseded rather than deleted. ISAE 3410 para 69 forbids
    discarding engagement documentation, and a register that silently drops
    cleared items cannot answer "what did you know on the day you signed".
    """
    from .calc import _utcnow_iso
    now = now or _utcnow_iso()

    acts = db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == organisation_id).order_by(
        ActivityRecord.id).all()

    raw = (_check_row_level(acts) + _check_duplicates(acts)
           + _check_unit_signature(acts) + _check_coverage_overlap(acts))

    by_id = {a.id: a for a in acts}
    inventory_kg = 0.0
    for a in acts:
        eff = _effect_kg(db, a)
        if eff is not None:
            inventory_kg += eff
    trivial_floor_kg = inventory_kg * trivial_floor_pct / 100.0
    materiality_kg = inventory_kg * materiality_pct / 100.0

    existing = {f.finding_key: f for f in db.query(ActivityFinding).filter(
        ActivityFinding.organisation_id == organisation_id).all()}
    seen = set()
    created = reopened = 0

    for f in raw:
        # The effect at risk is the largest of the activities the finding names —
        # a duplicated pair puts one copy's worth of emissions at risk.
        effects = [_effect_kg(db, by_id[i]) for i in f["activity_ids"] if i in by_id]
        known = [e for e in effects if e is not None]
        effect = max(known) if known else None
        unquantifiable = len(known) < len(effects)

        # ISAE 3410 A112: below the clearly-trivial floor a finding need not be
        # accumulated. An UNQUANTIFIABLE effect is never trivial — that is exactly
        # the misstatement an assuror most wants raised.
        trivial = (effect is not None and not unquantifiable
                   and trivial_floor_kg > 0 and effect < trivial_floor_kg
                   and f["severity"] not in ("blocking", "high"))

        key = f["finding_key"]
        seen.add(key)
        row = existing.get(key)
        if row is None:
            if trivial:
                continue
            db.add(ActivityFinding(
                organisation_id=organisation_id,
                activity_id=f["activity_ids"][0],
                related_activity_ids=json.dumps(f["activity_ids"]),
                finding_key=key, check_code=f["check_code"],
                severity=f["severity"], status="open",
                expectation=f["expectation"], threshold=f["threshold"],
                observed=f["observed"],
                estimated_effect_kg=effect,
                effect_quantifiable=not unquantifiable,
                screening_version=SCREENING_VERSION,
                detected_at=now, created_at=now))
            created += 1
        else:
            # The defect is still present. Refresh the observation and the effect,
            # keep the disposition, and RE-OPEN anything previously superseded.
            row.observed = f["observed"]
            row.estimated_effect_kg = effect
            row.effect_quantifiable = not unquantifiable
            if row.status == "superseded":
                row.status = "open"
                row.disposition_reason_code = None
                row.disposition_note = None
                row.dispositioned_at = None
                reopened += 1

    superseded = 0
    for key, row in existing.items():
        if key not in seen and row.status != "superseded":
            # The defect is gone. Never deleted — ISAE 3410 para 69.
            row.status = "superseded"
            superseded += 1

    db.commit()
    return summary(db, organisation_id,
                   materiality_pct=materiality_pct,
                   trivial_floor_pct=trivial_floor_pct) | {
        "screened_at": now,
        "activities_screened": len(acts),
        "findings_created": created,
        "findings_reopened": reopened,
        "findings_superseded": superseded,
    }


def summary(db: Session, organisation_id: int, *,
            materiality_pct: float = DEFAULT_MATERIALITY_PCT,
            trivial_floor_pct: float = DEFAULT_TRIVIAL_FLOOR_PCT) -> dict:
    """The misstatement ledger: what was found, what was cleared, what remains.

    The figure that matters is `accumulated_uncorrected_effect_kg` — ISAE 3410
    para 51(b) has the practitioner revise the plan when accumulated misstatements
    approach materiality, and ISSA 5000 para 160 requires the same evaluation. A
    platform that hands that total over pre-computed has removed real fieldwork.
    """
    rows = db.query(ActivityFinding).filter(
        ActivityFinding.organisation_id == organisation_id).order_by(
        ActivityFinding.id).all()
    live = [r for r in rows if r.status != "superseded"]

    by_status, by_severity, by_check = defaultdict(int), defaultdict(int), defaultdict(int)
    for r in live:
        by_status[r.status] += 1
        by_severity[r.severity] += 1
        by_check[r.check_code] += 1

    # UNCORRECTED = open or explicitly accepted. A corrected finding no longer
    # contributes: its defect is gone from the figures.
    uncorrected = [r for r in live if r.status in ("open", "accepted")]
    quantified = [r for r in uncorrected
                  if r.effect_quantifiable and r.estimated_effect_kg is not None]
    unquantified = [r for r in uncorrected if r not in quantified]
    accumulated = sum(r.estimated_effect_kg for r in quantified)

    inventory_kg = 0.0
    for a in db.query(ActivityRecord).filter(
            ActivityRecord.organisation_id == organisation_id).all():
        eff = _effect_kg(db, a)
        if eff is not None:
            inventory_kg += eff
    materiality_kg = inventory_kg * materiality_pct / 100.0

    blocking_open = [r for r in live
                     if r.severity == "blocking" and r.status == "open"]

    return {
        "screening_version": SCREENING_VERSION,
        "findings_total": len(live),
        "by_status": dict(by_status),
        "by_severity": dict(by_severity),
        "by_check": dict(by_check),
        "superseded_retained": len(rows) - len(live),
        "misstatement_ledger": {
            "accumulated_uncorrected_effect_kg": round(accumulated, 6),
            "uncorrected_findings": len(uncorrected),
            "uncorrected_unquantifiable": len(unquantified),
            "materiality_kg": round(materiality_kg, 6),
            "materiality_pct": materiality_pct,
            "trivial_floor_pct": trivial_floor_pct,
            "exceeds_materiality": (materiality_kg > 0
                                    and accumulated > materiality_kg),
            "note": "Uncorrected = open OR explicitly accepted. A finding whose "
                    "effect could not be quantified is counted separately and is "
                    "NEVER treated as nil — an unquantified misstatement is exactly "
                    "the kind an assuror most wants raised. Mirrors ISAE 3410 "
                    "paras 50-56 / ISSA 5000 paras 153-161.",
        },
        "open_blocking": len(blocking_open),
        "coverage": {
            "categories_with_unit_allowlist": sorted(CATEGORY_UNIT_ALLOWLIST),
            "note": "A category absent from the allow-list is not screened on the "
                    "unit check at all. That is reported here rather than passing "
                    "silently — an unscreened category is not a clean one.",
        },
        "not_screened": {
            "period_over_period_step_change": (
                "NOT IMPLEMENTED. The GHG Protocol Corporate Standard ch.7 rule that "
                "'changes of over 10 percent from year to year may warrant further "
                "investigation' needs a stable series identity, and ActivityRecord "
                "carries none — no metering point, site, asset or account. Two "
                "distinct sites are indistinguishable rows (the shipped demo data "
                "separates HQ from workshop electricity by description string alone), "
                "so any series key INFERRED from the available columns would merge "
                "them and report their sum as one trend. The resolution is a "
                "preparer-DECLARED series key on the activity — nullable, never "
                "written back by the engine, following the same doctrine as `scope` "
                "and `ghgp_category`, where NULL means 'not enrolled in "
                "period-over-period screening' and says so by name. That is a schema "
                "change and its own piece of work; inferring the series here would "
                "produce exactly the confidently-wrong number this platform exists "
                "to avoid."),
            "intensity_benchmarks": (
                "NOT IMPLEMENTED. Ratio-to-exposure checks (kWh per m2, litres per "
                "vehicle) need a recorded exposure basis — floor area, fleet size, "
                "headcount — which the model does not carry. Applying a published "
                "benchmark without its area basis, servicing type and climate zone "
                "would compare incomparable things."),
        },
    }


def completeness(db: Session, run) -> dict:
    """Screening blockers and warnings for one run, in the house gate shape.

    A run frozen before screening existed returns the LEGACY branch and is never
    retroactively blocked — the same anti-cliff rule the residual-mix and Scope 3
    temporal gates use. A NULL version means "this predates the requirement", and
    it is never back-filled.
    """
    from ..models import RunScreeningStatement
    version = getattr(run, "screening_version", None)
    if version is None:
        return {
            "assessable": True, "legacy": True, "blockers": [],
            "warnings": ["this run predates pre-calculation screening: no exception "
                         "register was frozen onto it, so nothing here states whether "
                         "its activity data was screened"],
            "statement": None,
        }

    st = db.query(RunScreeningStatement).filter(
        RunScreeningStatement.run_id == run.id).first()
    if st is None:
        return {"assessable": False, "legacy": False,
                "blockers": ["the run carries a screening version but no frozen "
                             "screening statement — the freeze did not complete"],
                "warnings": [], "statement": None}

    blockers, warnings = [], []
    if st.open_blocking > 0:
        blockers.append(
            f"{st.open_blocking} blocking finding(s) were open when this run was "
            f"computed (non-physical quantity, or a missing unit). Those rows cannot "
            f"produce a defensible figure until corrected or explicitly dispositioned")
    if st.exceeds_materiality:
        blockers.append(
            f"accumulated UNCORRECTED misstatement of "
            f"{st.accumulated_uncorrected_effect_kg:.1f} kgCO2e exceeds the "
            f"{st.materiality_pct}% materiality threshold "
            f"({st.materiality_kg:.1f} kgCO2e) — ISAE 3410 para 51(b) has the "
            f"practitioner revise the plan at this point")
    if st.uncorrected_unquantifiable > 0:
        warnings.append(
            f"{st.uncorrected_unquantifiable} uncorrected finding(s) have an effect "
            f"that could not be quantified and are therefore NOT in the accumulated "
            f"total — the true uncorrected effect is at least the stated figure")
    if st.findings_open > 0 and st.open_blocking == 0:
        warnings.append(
            f"{st.findings_open} finding(s) were still open and undispositioned when "
            f"this run was computed")

    return {"assessable": True, "legacy": False, "blockers": blockers,
            "warnings": warnings,
            "statement": {
                "screening_version": st.screening_version,
                "screened_at": st.screened_at,
                "findings_total": st.findings_total,
                "findings_open": st.findings_open,
                "findings_corrected": st.findings_corrected,
                "findings_accepted": st.findings_accepted,
                "open_blocking": st.open_blocking,
                "accumulated_uncorrected_effect_kg": st.accumulated_uncorrected_effect_kg,
                "uncorrected_unquantifiable": st.uncorrected_unquantifiable,
                "materiality_kg": st.materiality_kg,
                "materiality_pct": st.materiality_pct,
                "exceeds_materiality": st.exceeds_materiality,
                "frozen_at": st.frozen_at,
            }}


def freeze_onto_run(db: Session, run, organisation_id: int, now: str) -> None:
    """Freeze the screening state onto an immutable run.

    Advisory by design: this NEVER refuses to produce a run. The engine's own
    contract is that every activity lands in a visible bucket and nothing is
    silently dropped, and a gate that produced no run at all would be the first
    mechanism in the platform to leave no evidence artifact behind — the opposite
    trade from "failing soft is only defensible when something is counting". The
    blockers are reported at DISCLOSURE time by completeness(), where a reader can
    see both the figure and the reason to doubt it.
    """
    from ..models import RunScreeningStatement
    s = summary(db, organisation_id)
    led = s["misstatement_ledger"]
    latest = db.query(ActivityFinding).filter(
        ActivityFinding.organisation_id == organisation_id).order_by(
        ActivityFinding.detected_at.desc()).first()
    db.add(RunScreeningStatement(
        run_id=run.id,
        screening_version=SCREENING_VERSION,
        screened_at=(latest.detected_at if latest else None),
        findings_total=s["findings_total"],
        findings_open=s["by_status"].get("open", 0),
        findings_corrected=s["by_status"].get("corrected", 0),
        findings_accepted=s["by_status"].get("accepted", 0),
        open_blocking=s["open_blocking"],
        accumulated_uncorrected_effect_kg=led["accumulated_uncorrected_effect_kg"],
        uncorrected_unquantifiable=led["uncorrected_unquantifiable"],
        materiality_kg=led["materiality_kg"],
        materiality_pct=led["materiality_pct"],
        exceeds_materiality=bool(led["exceeds_materiality"]),
        frozen_at=now))
    run.screening_version = SCREENING_VERSION


def dispose(db: Session, organisation_id: int, finding_id: int, *,
            status: str, reason_code: str, note: str,
            now: Optional[str] = None) -> dict:
    """Record a disposition against a finding.

    PCAOB SAPA 11: "Verifying that a review was signed off provides little or no
    evidence by itself about the control's effectiveness." So a bare
    acknowledgement is refused — a disposition must carry a reason code from the
    closed vocabulary AND a substantive note describing what was investigated and
    concluded. A note of "ok" is not audit evidence.
    """
    from .calc import _utcnow_iso
    now = now or _utcnow_iso()

    if status not in ("corrected", "accepted"):
        return {"disposed": False,
                "reason": "status must be 'corrected' or 'accepted'; a finding is "
                          "never deleted and never silently closed"}
    if reason_code not in DISPOSITION_REASON_CODES:
        return {"disposed": False,
                "reason": f"reason_code must be one of "
                          f"{list(DISPOSITION_REASON_CODES)} — a closed vocabulary "
                          f"keeps accepted findings queryable instead of decaying "
                          f"into free text"}
    if not note or len(note.strip()) < 15:
        return {"disposed": False,
                "reason": "a disposition needs a substantive note describing what was "
                          "investigated and concluded (at least 15 characters). PCAOB "
                          "SAPA 11: a sign-off alone is not evidence of a control"}

    row = db.query(ActivityFinding).filter(
        ActivityFinding.id == finding_id,
        ActivityFinding.organisation_id == organisation_id).first()
    if row is None:
        return {"disposed": False, "reason": "finding not found for this organisation"}
    if row.status == "superseded":
        return {"disposed": False,
                "reason": "this finding is superseded: the defect is no longer present "
                          "in the activity data, so there is nothing to dispose of"}

    row.status = status
    row.disposition_reason_code = reason_code
    row.disposition_note = note.strip()
    row.dispositioned_at = now
    db.commit()
    return {"disposed": True, "finding_id": row.id, "status": row.status,
            "reason_code": row.disposition_reason_code,
            "dispositioned_at": row.dispositioned_at}


def finding_view(row: ActivityFinding) -> dict:
    return {
        "id": row.id, "finding_key": row.finding_key,
        "check_code": row.check_code, "severity": row.severity, "status": row.status,
        "activity_id": row.activity_id,
        "related_activity_ids": json.loads(row.related_activity_ids or "[]"),
        "expectation": row.expectation,
        "threshold": row.threshold,
        "observed": row.observed,
        "estimated_effect_kg": row.estimated_effect_kg,
        "effect_quantifiable": row.effect_quantifiable,
        "disposition": {
            "reason_code": row.disposition_reason_code,
            "note": row.disposition_note,
            "at": row.dispositioned_at,
        },
        "screening_version": row.screening_version,
        "detected_at": row.detected_at,
    }
