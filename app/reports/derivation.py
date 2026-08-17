"""Worked calculations: how each reported figure was actually arrived at.

A disclosure that states a number an auditor cannot reperform is asking to be trusted.
This module lets a report SHOW its arithmetic — the inputs, the operation, the
intermediate subtotals — in a form that travels through JSON, CSV and the PDF alike.

THE RULE THAT MAKES THIS WORTH ANYTHING: a derivation must reproduce the figure the
report actually states, and when it does not, the payload SAYS SO.

A worked calculation that looks plausible but does not add up to the disclosed number is
worse than no calculation at all: it manufactures confidence in a figure whose provenance
has silently diverged from its presentation. So every derivation is checked against the
stated value at build time, and a mismatch is surfaced as `reconciles: false` with the
discrepancy — never rounded away, never quietly dropped.

Not every figure is derived. Some are INPUTS — a factor value, a supplied denominator, a
figure a preparer typed in. Those are recorded with `stated()` rather than given invented
arithmetic. Fabricating a derivation for an input would be the same overclaim in a
different costume.
"""
import math
from typing import Any, Optional

# Operations a derivation can express. Deliberately small: anything a reader cannot
# follow in their head is not a worked calculation, it is a second black box.
SUM = "sum"
DIFFERENCE = "difference"
PRODUCT = "product"
QUOTIENT = "quotient"
STATED = "stated"            # an input, not a derivation — no arithmetic to show
# Two or more measurements of the SAME underlying quantity that a standard requires to be
# disclosed side by side and that must NEVER be combined — Scope 2 location vs market
# being the canonical case. Modelling this as a sum would double-count the electricity;
# modelling it as a plain input would hide the second measurement entirely.
ALTERNATIVES = "alternatives"
# A figure whose arithmetic is REFUSED. The platform's own gates sometimes establish that
# a sum must not be taken — Scope 3 Category 15 declared through BOTH activity lines and
# a PCAF portfolio being the canonical case, where adding them double-counts the same
# investee. Showing the addition anyway, reconciled and ticked, is strictly worse than
# showing no working: it certifies as reperformed arithmetic a number the platform has
# already established is wrong.
BLOCKED = "blocked"

# Reconciliation tolerance. Relative, because these figures span kilograms of methane to
# megatonnes of CO2e, with an absolute floor so a near-zero figure does not fail on
# floating-point dust.
_REL_TOL = 1e-9
_ABS_TOL = 1e-9


def _fmt(v: Optional[float]) -> str:
    """Readable without being lossy.

    Thousands separators for ordinary magnitudes; scientific notation is used — not
    avoided — for the very small and very large, because rendering 3.5e-07 as "0" would
    make a worked calculation show terms that do not produce its own result.
    """
    if v is None:
        return "—"
    if not math.isfinite(v):
        return str(v)
    if v == int(v) and abs(v) < 1e15:
        return f"{int(v):,}"
    if v != 0 and (abs(v) < 1e-4 or abs(v) >= 1e15):
        return f"{v:.6e}"
    return f"{v:,.6g}"


class Derivation:
    """One reported figure and the arithmetic behind it.

    Build it, then call `.build(stated)` with the value the report actually publishes.
    The result carries `reconciles`, which is the whole point.
    """

    def __init__(self, figure: str, unit: Optional[str] = None,
                 operation: str = SUM, note: Optional[str] = None,
                 basis: Optional[str] = None, display_dp: Optional[int] = None,
                 independent: bool = False):
        self.figure = figure
        self.unit = unit
        self.operation = operation
        self.note = note
        self.basis = basis            # e.g. "location-based", "AR6 GWP-100"
        # Reports publish rounded figures while calculating at full precision — the
        # correct order, since rounding intermediates then aggregating drifts. Recording
        # the published precision lets reconciliation check the RIGHT thing: that the
        # report rounded the correct underlying number, rather than demanding an exact
        # match a rounded figure can never give.
        self.display_dp = display_dp
        # Whether reconciliation compares two INDEPENDENTLY derived values (e.g. a total
        # frozen at compute time against a live re-aggregation of the same run's lines),
        # or merely re-states the arithmetic the payload already used.
        #
        # This distinction is load-bearing for honesty. A recomputation shows a reader HOW
        # a figure was built, which is the point — but it cannot detect a corrupted input,
        # because both sides of the comparison inherit it. Only an independent check can
        # fail on bad data. Presenting both as "reperformed" would let a green tick vouch
        # for a number nothing actually verified.
        self.independent = independent
        self.terms: list = []

    # --- building -----------------------------------------------------------------
    def term(self, label: str, value: Optional[float], *,
             unit: Optional[str] = None, source: Optional[str] = None,
             count: Optional[int] = None, detail: Optional[dict] = None) -> "Derivation":
        """One input to the operation. `count` is the number of underlying records it
        aggregates, which is what lets a reader tell a zero from an absence."""
        self.terms.append({
            "label": label,
            "value": value,
            "unit": unit or self.unit,
            "source": source,
            "count": count,
            "detail": detail,
        })
        return self

    def terms_from(self, rows: list, *, label_key: str, value_key: str,
                   unit: Optional[str] = None, count_key: Optional[str] = None,
                   source: Optional[str] = None) -> "Derivation":
        """Add one term per row — the common case of a total built from a breakdown."""
        for r in rows:
            self.term(str(r.get(label_key)), r.get(value_key), unit=unit, source=source,
                      count=r.get(count_key) if count_key else None)
        return self

    # --- evaluation ---------------------------------------------------------------
    def _compute(self) -> Optional[float]:
        vals = [t["value"] for t in self.terms]
        if self.operation in (STATED, ALTERNATIVES, BLOCKED):
            # Nothing is computed: an input has no arithmetic, alternatives must not be
            # combined, and a blocked figure's arithmetic is deliberately refused.
            return None
        # An unknown term poisons the whole calculation: a sum that silently skips a
        # None is a different (smaller) number wearing the same label.
        if any(v is None or not math.isfinite(v) for v in vals):
            return None
        if not vals:
            return None
        if self.operation == SUM:
            return math.fsum(vals)                 # fsum: no accumulated rounding drift
        if self.operation == DIFFERENCE:
            out = vals[0]
            for v in vals[1:]:
                out -= v
            return out
        if self.operation == PRODUCT:
            out = 1.0
            for v in vals:
                out *= v
            return out
        if self.operation == QUOTIENT:
            if len(vals) != 2:
                return None
            return None if vals[1] == 0 else vals[0] / vals[1]
        raise ValueError(f"unknown operation {self.operation!r}")

    def expression(self) -> str:
        """The calculation as a line of text, for the PDF and for a human skim."""
        if self.operation == BLOCKED:
            return "calculation refused"
        if self.operation == ALTERNATIVES:
            return "  |  ".join(f"{t['label']}: {_fmt(t['value'])}" for t in self.terms)
        if self.operation == STATED:
            t = self.terms[0] if self.terms else {}
            return f"{_fmt(t.get('value'))}{' ' + (t.get('unit') or '') if t.get('unit') else ''}"
        joiner = {SUM: " + ", DIFFERENCE: " − ", PRODUCT: " × ", QUOTIENT: " ÷ "}[self.operation]
        parts = [_fmt(t["value"]) for t in self.terms]
        if self.operation == SUM and len(parts) > 6:
            # A 40-term sum is not readable as an expression; the term table carries it.
            return f"sum of {len(parts)} terms"
        return joiner.join(parts)

    def build(self, stated: Optional[float] = None) -> dict:
        """Finish the derivation, checking it against the figure actually reported."""
        computed = self._compute()
        # NOT defaulted to `computed`: doing so made the derivation certify its own
        # output, and printed a `reported` value the report does not publish (a run with
        # a NULL total showed "200.0 kgCO2e" as the disclosed figure). An absent stated
        # value is an unknown, and unknown-versus-anything never reconciles.
        if self.operation == BLOCKED:
            # Never "reconciles": there is no working to reconcile, and marking it true
            # would let a blocked figure pass the all_reconcile headline.
            reconciles, difference = False, None
        elif self.operation == ALTERNATIVES:
            # Nothing is combined, but the figure the report LEADS with must be one of
            # the alternatives — otherwise the card shows two numbers beside a third.
            vals = [t["value"] for t in self.terms]
            reconciles = stated is None or any(
                v is not None and _reconcile(v, stated)[0] for v in vals)
            difference = None
        elif self.operation == STATED:
            # An input has no arithmetic, but the value shown must be the value reported.
            first = self.terms[0]["value"] if self.terms else None
            reconciles, difference = _reconcile(first, stated)
        elif self.display_dp is not None and computed is not None and stated is not None:
            # `display_dp` exists so a report that publishes 6dp is not asked for exact
            # equality. But it is an ABSOLUTE decimal test on figures of unbounded
            # magnitude: at 6dp any two values within 5e-7 match, so an intensity of
            # 2e-7 published as 0.0 — a sign flip, or a 1000x error — used to pass.
            # It therefore only applies where the rounding unit is genuinely small
            # relative to the value; otherwise the strict relative check governs.
            unit = 0.5 * (10 ** -self.display_dp)
            scale = max(abs(computed), abs(stated))
            if scale > 0 and unit / scale <= 1e-3:
                reconciles = abs(computed - stated) <= unit
                difference = None if reconciles else computed - stated
            else:
                reconciles, difference = _reconcile(computed, stated)
        else:
            reconciles, difference = _reconcile(computed, stated)
        out = {
            "figure": self.figure,
            "unit": self.unit,
            "operation": self.operation,
            "basis": self.basis,
            "note": self.note,
            "terms": self.terms,
            "expression": self.expression(),
            "computed": computed,
            "reported": stated,
            "display_dp": self.display_dp,
            "independent": self.independent,
            "reconciles": reconciles,
        }
        if self.operation == BLOCKED:
            out["blocked_reason"] = self.note
            out["reconciliation_error"] = (
                f"no working is shown for this figure: {self.note}")
            return out
        if not reconciles:
            out["difference"] = difference
            # Loud, in the payload, in the words a reader needs. This is a defect in the
            # report — not a rounding note — and it must read like one.
            out["reconciliation_error"] = (
                f"the steps shown produce {_fmt(computed)} but the report states "
                f"{_fmt(stated)}"
                + (f" (difference {_fmt(difference)})" if difference is not None else "")
                + " — the stated figure is what the report discloses; this derivation "
                  "does not fully explain it and must not be relied on to reperform it")
        return out


def _reconcile(computed: Optional[float], stated: Optional[float]) -> tuple:
    """(reconciles, difference). Unknown-vs-known never silently passes."""
    # Unknown vs unknown is NOT agreement: a sum with an unknown term, compared against
    # a figure the report never stated, would otherwise get a green tick on nothing.
    if computed is None or stated is None:
        return False, None
    if not math.isfinite(computed) or not math.isfinite(stated):
        return computed == stated, None
    diff = computed - stated
    tol = max(_ABS_TOL, _REL_TOL * max(abs(computed), abs(stated)))
    return abs(diff) <= tol, (None if abs(diff) <= tol else diff)


# --- convenience constructors ------------------------------------------------------

def blocked(figure: str, reason: str, *, reported: Optional[float] = None,
            unit: Optional[str] = None) -> dict:
    """A figure whose working is deliberately not shown, because the platform's own
    gates have established the arithmetic would be wrong.

    The figure itself may still appear in the payload (removing it would break every
    consumer), but it never gets a tick. `reason` must say WHY, in the words a reader
    needs to judge whether to rely on the number at all.
    """
    d = Derivation(figure, unit=unit, operation=BLOCKED, note=reason)
    return d.build(reported)


def alternatives(figure: str, options: list, *, reported: Optional[float] = None,
                 unit: Optional[str] = None, note: Optional[str] = None) -> dict:
    """Measurements a standard requires side by side and forbids combining.

    `options` is a list of (label, value). `reported` is whichever one the report leads
    with — stating it explicitly stops a reader inferring that the first is authoritative.
    """
    d = Derivation(figure, unit=unit, operation=ALTERNATIVES, note=note)
    for label, value in options:
        d.term(label, value, unit=unit)
    return d.build(reported)


def stated(figure: str, value: Optional[float], *, unit: Optional[str] = None,
           source: str = "supplied input", note: Optional[str] = None) -> dict:
    """A figure that is an INPUT, not a derivation. Recorded honestly as such rather
    than dressed in arithmetic it never went through."""
    d = Derivation(figure, unit=unit, operation=STATED, note=note)
    d.term(figure, value, unit=unit, source=source)
    return d.build(value)


def total_of(figure: str, rows: list, *, label_key: str, value_key: str,
             unit: Optional[str] = None, stated_value: Optional[float] = None,
             count_key: Optional[str] = None, basis: Optional[str] = None,
             note: Optional[str] = None, display_dp: Optional[int] = None, independent: bool = False) -> dict:
    """A total built from a breakdown — the shape most report figures actually have."""
    d = Derivation(figure, unit=unit, operation=SUM, basis=basis, note=note,
                   display_dp=display_dp, independent=independent)
    d.terms_from(rows, label_key=label_key, value_key=value_key, unit=unit,
                 count_key=count_key)
    return d.build(stated_value)


def ratio(figure: str, numerator_label: str, numerator: Optional[float],
          denominator_label: str, denominator: Optional[float], *,
          unit: Optional[str] = None, stated_value: Optional[float] = None,
          note: Optional[str] = None, display_dp: Optional[int] = None, independent: bool = False) -> dict:
    """An intensity or share. The labels carry the boundary, because a ratio whose
    numerator and denominator span different boundaries is the classic silent defect."""
    d = Derivation(figure, unit=unit, operation=QUOTIENT, note=note,
                   display_dp=display_dp, independent=independent)
    d.term(numerator_label, numerator)
    d.term(denominator_label, denominator)
    return d.build(stated_value)


def product(figure: str, factors: list, *, unit: Optional[str] = None,
            stated_value: Optional[float] = None, note: Optional[str] = None, display_dp: Optional[int] = None, independent: bool = False) -> dict:
    """`factors` is a list of (label, value) or (label, value, unit)."""
    d = Derivation(figure, unit=unit, operation=PRODUCT, note=note,
                   display_dp=display_dp, independent=independent)
    for f in factors:
        d.term(f[0], f[1], unit=f[2] if len(f) > 2 else None)
    return d.build(stated_value)


def difference(figure: str, terms: list, *, unit: Optional[str] = None,
               stated_value: Optional[float] = None, note: Optional[str] = None, display_dp: Optional[int] = None, independent: bool = False) -> dict:
    """`terms` is a list of (label, value); the first is the minuend."""
    d = Derivation(figure, unit=unit, operation=DIFFERENCE, note=note,
                   display_dp=display_dp, independent=independent)
    for label, value in terms:
        d.term(label, value, unit=unit)
    return d.build(stated_value)


# --- collection --------------------------------------------------------------------

def summarise(derivations: list) -> dict:
    """Roll a report's derivations into a block the payload can carry whole.

    `all_reconcile` is the honest headline: if any worked calculation fails to reproduce
    its own figure, the report says so at the top rather than burying it in one row.
    """
    rows = [d for d in derivations if d]
    refused = [d for d in rows if d.get("operation") == BLOCKED]
    # A refused calculation is a KNOWN-bad figure the platform declined to work; an
    # unreconciled one is a surprise the report cannot explain. Listing the refusals in
    # both made the UI count them twice.
    broken = [d for d in rows
              if not d.get("reconciles") and d.get("operation") != BLOCKED]
    return {
        "figures": rows,
        "count": len(rows),
        # Counts only the figures that are actually reperformed arithmetic — inputs and
        # alternative-measurement pairs are neither, and counting them inflated the
        # "N figures reperformed" badge.
        "computed_count": sum(1 for d in rows
                              if d.get("operation") not in (STATED, ALTERNATIVES, BLOCKED)),
        "all_reconcile": not broken and not refused,
        "unreconciled": [d["figure"] for d in broken],
        "calculations_refused": [d["figure"] for d in refused],
        "independently_verified": [d["figure"] for d in rows if d.get("independent")],
        "note": ("Every figure below is shown with the inputs and the operation that "
                 "produced it, so a reader can follow how it was built. Figures marked "
                 "'stated' are inputs rather than calculations. Note the difference "
                 "between the two kinds of check: an INDEPENDENTLY VERIFIED figure is "
                 "compared against a separately derived value, so it fails on bad data; "
                 "the rest restate the arithmetic the report itself used, which shows the "
                 "method but cannot detect a corrupted input, because both sides inherit "
                 "it."),
        **({"warning": (
            (f"{len(refused)} figure(s) have NO working shown because the platform "
             f"established the calculation would be wrong: "
             f"{', '.join(d['figure'] for d in refused)}. "
             if refused else "")
            + (f"{len(broken)} figure(s) could not be reconciled to their own worked "
               f"calculation: {', '.join(d['figure'] for d in broken)}. The reported "
               f"values stand; the derivations shown for them are incomplete."
               if broken else "")
        ).strip()} if (broken or refused) else {}),
    }


def to_lines(block: dict) -> list:
    """Flatten to (indent, text) pairs for the PDF and plain-text renderers."""
    out: list = []
    for d in block.get("figures", []):
        unit = f" {d['unit']}" if d.get("unit") else ""
        out.append((0, f"{d['figure']} = {_fmt(d['reported'])}{unit}"))
        if d.get("basis"):
            out.append((1, f"basis: {d['basis']}"))
        if d["operation"] == BLOCKED:
            out.append((1, f"!! no working shown — {d.get('blocked_reason')}"))
        elif d["operation"] == ALTERNATIVES:
            for t in d["terms"]:
                out.append((1, f"{t['label']}: {_fmt(t['value'])}"))
            out.append((1, "reported side by side — these are alternative measurements "
                           "of the same quantity and are never added"))
        elif d["operation"] == STATED:
            src = (d["terms"][0].get("source") if d["terms"] else None) or "input"
            out.append((1, f"({src} — not a calculation)"))
        else:
            for t in d["terms"]:
                cnt = f"  [{t['count']} record(s)]" if t.get("count") is not None else ""
                out.append((1, f"{t['label']}: {_fmt(t['value'])}{cnt}"))
            out.append((1, f"= {d['expression']}"))
        # A blocked figure has already printed its reason; note and reconciliation_error
        # carry the same text, so printing all three would say it three times.
        if d.get("note") and d["operation"] != BLOCKED:
            out.append((1, d["note"]))
        if not d.get("reconciles") and d["operation"] != BLOCKED:
            out.append((1, f"!! {d['reconciliation_error']}"))
    return out
