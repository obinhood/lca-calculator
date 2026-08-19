"""Cross-run comparability: the machine-checkable half of "these two totals may be
subtracted".

Five renderers subtract one immutable run's total from another's and publish the
difference as a reduction (GRI 305-5), a trend (EcoVadis Actions), a trajectory
variance (SBTi), or a project abatement (ISO 14064-2). The arithmetic is exact. What
makes the difference MEAN abatement is that the two runs are alike in every dimension
except the abatement — same GWP vintage, same residual-mix methodology, same
organisational boundary, and the same length of ELAPSED TIME.

That last dimension is this module. A 12-month base minus a 3-month current run reports
the missing nine months as a 75% reduction, and no amount of care in the arithmetic can
see it: both totals are correct for what they cover. ISO 14064-2 gated it first; the
gate lives here so the tolerance is declared ONCE and every renderer that subtracts two
runs applies the same one, rather than five copies drifting apart.

The related trap is a period-scoped numerator over an unscoped denominator: a quarter's
emissions divided by annual revenue is a ratio 4x too low, and again both inputs are
individually right. `denominator_period_comparable` is that check.

Throughout: a period that cannot be DETERMINED — an unscoped run, a date that is not
ISO — is never treated as a pass. It is a cannot-determine, and for a comparison a
cannot-determine is a blocker. The delta may well be sound; nothing here can show that
it is, and a gate that fails open on a missing input is not a gate.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import ReportingPeriod
from .calc import _parse_iso_date

# How far two period lengths may differ before a delta between them is refused. Stated
# as a constant and echoed in every payload that relies on it, because a silent
# tolerance lets up to this share of the base total read as abatement.
PERIOD_TOLERANCE_PCT = 0.05


def period_days(period) -> Optional[int]:
    """Inclusive day count of a reporting period, or None if either date is unparseable.

    None is a cannot-determine, never a zero: every caller treats it as a blocker
    rather than skipping its check.
    """
    if period is None:
        return None
    a, b = _parse_iso_date(period.start_date or ""), _parse_iso_date(period.end_date or "")
    return (b - a).days + 1 if (a and b) else None


def run_period(db: Session, run) -> Optional[ReportingPeriod]:
    """The reporting period a run is scoped to, or None for an unscoped run."""
    if run is None or not getattr(run, "reporting_period_id", None):
        return None
    return db.get(ReportingPeriod, run.reporting_period_id)


def period_info(db: Session, run) -> Optional[dict]:
    """Disclosable description of a run's period — None when the run is unscoped.

    Published alongside every cross-run figure so a reader can see the two windows the
    difference spans instead of having to trust that they match.
    """
    p = run_period(db, run)
    if p is None:
        return None
    return {"reporting_period_id": p.id, "label": p.label, "start_date": p.start_date,
            "end_date": p.end_date, "days": period_days(p)}


def period_comparable(db: Session, run_a, run_b, *, label_a: str, label_b: str,
                      quantity: str, measures: str,
                      tolerance_pct: float = PERIOD_TOLERANCE_PCT) -> Optional[str]:
    """A reason the two runs do not cover comparable lengths of time, or None.

    `quantity` names what is being computed ("the reduction"), `measures` what it is
    supposed to measure ("abatement") — so the blocker reads as the renderer's own
    sentence rather than a generic one.
    """
    p_a, p_b = run_period(db, run_a), run_period(db, run_b)
    if p_a is None or p_b is None:
        _unscoped = [lbl for lbl, p in ((label_a, p_a), (label_b, p_b)) if p is None]
        return (f"the {label_a} run and the {label_b} run must both be scoped to a "
                f"reporting period ({', '.join(_unscoped)} not scoped) — an unscoped run "
                f"has no period length, so {quantity} cannot be shown to measure "
                f"{measures} rather than a difference in elapsed time")
    d_a, d_b = period_days(p_a), period_days(p_b)
    if d_a is None or d_b is None:
        # An unparseable date is a CANNOT-DETERMINE, not a pass: skipping the check here
        # lets a 12-month base minus a 3-month current run through untouched because one
        # period was written 01/01/2025 instead of 2025-01-01.
        _bad = [p.label for p, d in ((p_a, d_a), (p_b, d_b)) if d is None]
        return (f"reporting period length cannot be determined for {_bad} — the dates are "
                f"not ISO (YYYY-MM-DD), so {quantity} cannot be shown to measure "
                f"{measures} rather than a difference in elapsed time")
    if abs(d_a - d_b) > max(1, round(tolerance_pct * max(d_a, d_b))):
        return (f"{label_a} period is {d_a} days and {label_b} period is {d_b} days — "
                f"beyond the {tolerance_pct:.0%} tolerance, so {quantity} would report the "
                f"difference in elapsed time as abatement. Re-scope both runs to "
                f"comparable periods.")
    return None


def period_payload(db: Session, run_a, run_b, *, key_a: str, key_b: str, note: str,
                   tolerance_pct: float = PERIOD_TOLERANCE_PCT) -> dict:
    """The disclosure block for a period gate: both lengths, the tolerance, and why.

    Published whether or not the gate passed — a reader of a comparable delta needs the
    two windows just as much as a reader of a blocked one.
    """
    return {
        f"{key_a}_days": period_days(run_period(db, run_a)),
        f"{key_b}_days": period_days(run_period(db, run_b)),
        f"{key_a}_period": period_info(db, run_a),
        f"{key_b}_period": period_info(db, run_b),
        "tolerance_pct": tolerance_pct * 100.0,
        "note": note,
    }


def year_within_period(db: Session, run, year: Optional[int], *, year_name: str,
                       run_label: str) -> Optional[str]:
    """A reason `year` cannot be tied to `run`'s reporting period, or None.

    A year label supplied as a free parameter is an assertion about WHEN a run's
    emissions occurred, and it drives where the run lands on a pathway. Nothing else
    checks it: a base year that does not match the base run, or a current year off by
    five, moves the allowance without moving the emissions.
    """
    if year is None:
        return None                                    # a separate gate's business
    p = run_period(db, run)
    if p is None:
        return (f"{year_name} {year} cannot be tied to the {run_label} run — the run is "
                f"not scoped to a reporting period, so nothing establishes that the year "
                f"labelling these emissions is the year they were emitted in")
    a, b = _parse_iso_date(p.start_date or ""), _parse_iso_date(p.end_date or "")
    if a is None or b is None:
        return (f"{year_name} {year} cannot be tied to the {run_label} run — reporting "
                f"period {p.label!r} has non-ISO dates "
                f"({p.start_date!r}..{p.end_date!r}), so the years it covers are unknown")
    if not (a.year <= year <= b.year):
        # A fiscal period spanning two calendar years legitimately answers to either, so
        # the gate is membership of the span, not a derived "predominant" year — which
        # would impose a convention the standards do not.
        return (f"{year_name} {year} is outside the {run_label} run's reporting period "
                f"{p.label!r} ({p.start_date}..{p.end_date}, covering "
                f"{a.year}-{b.year}) — the run's emissions would be placed at a year they "
                f"do not cover")
    return None


def denominator_period_comparable(db: Session, run, denominator_period_days,
                                  *, ratio_name: str,
                                  tolerance_pct: float = PERIOD_TOLERANCE_PCT
                                  ) -> Optional[str]:
    """A reason an intensity denominator cannot be shown to cover the numerator's period.

    An intensity ratio divides a period-scoped emissions total by a caller-supplied
    figure. If that figure covers a different length of time — annual revenue over a
    quarter's emissions — the ratio is out by the ratio of the lengths and both inputs
    are still individually correct, so nothing downstream can catch it.
    """
    if denominator_period_days is None:
        return (f"{ratio_name}: intensity_denominator_period_days is required alongside "
                f"intensity_denominator — a denominator with no period cannot be shown to "
                f"cover the same span as the emissions it divides, and an annual "
                f"denominator over a quarter's emissions yields a ratio 4x too low")
    try:
        d_den = int(denominator_period_days)
    except (TypeError, ValueError):
        d_den = -1
    if d_den <= 0:
        return (f"{ratio_name}: intensity_denominator_period_days must be a positive "
                f"whole number of days, got {denominator_period_days!r}")
    p = run_period(db, run)
    if p is None:
        return (f"{ratio_name}: the run is not scoped to a reporting period, so the "
                f"denominator's {d_den}-day period cannot be checked against the span of "
                f"the emissions in the numerator")
    d_run = period_days(p)
    if d_run is None:
        return (f"{ratio_name}: reporting period {p.label!r} has non-ISO dates "
                f"({p.start_date!r}..{p.end_date!r}), so the numerator's span cannot be "
                f"determined and the denominator's {d_den} days cannot be checked "
                f"against it")
    if abs(d_run - d_den) > max(1, round(tolerance_pct * max(d_run, d_den))):
        return (f"{ratio_name}: the emissions cover {d_run} days ({p.label}) but the "
                f"denominator covers {d_den} days — beyond the {tolerance_pct:.0%} "
                f"tolerance, the ratio is out by a factor of about "
                f"{max(d_run, d_den) / min(d_run, d_den):.2f}. Supply a denominator for "
                f"the same period as the run.")
    return None
