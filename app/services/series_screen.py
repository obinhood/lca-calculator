"""Period-over-period screening over DECLARED series.

The screening register (services/screening.py) had to decline the GHG Protocol
Corporate Standard chapter 7 rule that "changes of over 10 percent from year to
year may warrant further investigation", because ActivityRecord carried no series
identity and any key INFERRED from category, subcategory, geography and entity
would merge two physically distinct sites. This closes that gap the only honest
way: the series is DECLARED by the preparer and never written back by the engine.

NULL series_key means "not enrolled", which is the default on every existing row.
So no detector can fire on historical data — the blast radius is zero by
construction rather than by threshold tuning — and the unenrolled share of the
inventory is REPORTED by name rather than passing as clean.

THE STATISTICS, AND WHY NOT THE OBVIOUS ONES

Comparison is on QUANTITY per series per unit, never on emissions: a factor
revision must not read as a data anomaly.

A classical z-score cannot do this job at all. Shiffler (1988) bounds
max|z| = (n-1)/sqrt(n), so with twelve series nothing can exceed 3.175 and with
ten or fewer nothing can exceed 3 — a three-sigma rule is blind, not conservative.
Tukey fences on raw skewed data exceed at roughly 7.6% against 0.7% at the normal
(Hubert & Vandervieren 2008). Both fail on the same thing: they test LEVELS.

So the test is on the log ratio of like-for-like periods:

    d_s = ln(q_now[s] / q_base[s])
    T   = clamp(3.0 * 1.4826 * b_n * MAD(d), ln(1.30), ln(2.50))
    flag when |d_s - median(d)| > T

with a hard backstop at 3x or 1/3 irrespective of T, so a volatile portfolio
cannot widen its own band far enough to certify a gross error away. b_n is the
Croux-Rousseeuw finite-sample correction; without it 1.4826*MAD understates sigma
by about 7% at n=12 and over-flags by roughly the same margin.

Below four comparable series the adaptive term does not run at all. Calibrating a
dispersion estimate from three points is not a conservative approximation, it is a
fabrication, and the payload says `band_basis: "insufficient_series"` instead.

A LEVEL SHIFT IS NOT AN ERROR. A site that opens, a factory that closes, a line
that is commissioned — these move a series permanently and legitimately. ISAE 3410
A101 names exactly this case: trends must be read "for consistency with other
circumstances such as the acquisition or disposal of facilities".

`compare()` screens ONE pair of periods and cannot see a persistent shift — a caller
holding only two periods has no way to distinguish a step from a spike. The
reclassification is `classify_level_shifts()`, which takes an ordered history of
`compare()` payloads and re-files a series whose deviation persists in the same
direction as `level_shift`: informational, routed to base-year recalculation rather
than filed as a data defect. It is available to a caller that accumulates comparisons;
no endpoint assembles that history yet, so the ISAE 3410 A101 treatment is offered
here, not applied on your behalf.
"""
import math
import statistics
from collections import defaultdict
from typing import Optional

from sqlalchemy.orm import Session

from ..models import ActivityRecord, CalculationRun, ReportingPeriod

SERIES_SCREEN_VERSION = "ser-v1"

# Croux-Rousseeuw / Akinshin finite-sample correction for the MAD scale estimate.
# Omitting it understates sigma by ~7% at n=12 and over-flags by about as much.
_BN = {2: 1.865, 3: 1.514, 4: 1.361, 5: 1.206, 6: 1.190, 7: 1.134, 8: 1.126,
       9: 1.091, 10: 1.096, 11: 1.070, 12: 1.076, 13: 1.058, 14: 1.062,
       15: 1.049, 16: 1.053, 17: 1.044, 18: 1.047, 19: 1.039, 20: 1.042}

K = 3.0                      # not 3.5 (loses power on a doubled bill) nor 2.5
BAND_FLOOR = math.log(1.30)  # weather and occupancy move a heating series this far
BAND_CAP = math.log(2.50)
HARD_BACKSTOP = 3.0          # flagged irrespective of the adaptive band
MIN_SERIES_FOR_BAND = 4      # below this, only the backstop applies
LEVEL_SHIFT_MIN_RUNS = 3     # consecutive same-direction flags to reclassify

_EPS = 1e-12


def _bn(n: int) -> float:
    return _BN.get(n, 1.0)


def _mad(values: list) -> float:
    if not values:
        return 0.0
    med = statistics.median(values)
    return statistics.median([abs(v - med) for v in values])


def _iqr(values: list) -> float:
    if len(values) < 4:
        return 0.0
    s = sorted(values)
    q1 = statistics.median(s[:len(s) // 2])
    q3 = statistics.median(s[(len(s) + 1) // 2:])
    return q3 - q1


def band(deviations: list) -> dict:
    """The adaptive flag threshold T, and an honest account of how it was derived."""
    n = len(deviations)
    if n < MIN_SERIES_FOR_BAND:
        return {"threshold": None, "band_basis": "insufficient_series",
                "n": n, "median": statistics.median(deviations) if deviations else 0.0,
                "note": f"only {n} comparable series; a dispersion estimate from fewer "
                        f"than {MIN_SERIES_FOR_BAND} points is a fabrication, not a "
                        f"conservative approximation. Only the {HARD_BACKSTOP}x "
                        f"backstop applies."}
    med = statistics.median(deviations)
    mad = _mad(deviations)
    basis = "mad"
    scale = 1.4826 * _bn(n) * mad
    if scale <= _EPS:
        # Repeated flat or estimated readings make MAD=0 common.
        iqr = _iqr(deviations)
        scale = iqr / 1.349 if iqr > _EPS else 0.0
        basis = "iqr" if scale > _EPS else "degenerate_dispersion"
    t = K * scale
    t = max(BAND_FLOOR, min(BAND_CAP, t)) if basis != "degenerate_dispersion" else BAND_CAP
    return {"threshold": t, "band_basis": basis, "n": n, "median": med,
            "note": None}


def _series_quantities(db: Session, organisation_id: int,
                       period: ReportingPeriod) -> dict:
    """{(series_key, unit): summed quantity} for one period.

    Units are part of the key deliberately: summing kWh with m3 would be a
    dimensional error, and two rows in different units are not the same series
    even when they carry the same declared key.
    """
    rows = db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == organisation_id,
        ActivityRecord.series_key.isnot(None)).all()
    out = defaultdict(float)
    start, end = period.start_date, period.end_date
    for a in rows:
        if a.quantity is None or not isinstance(a.quantity, (int, float)):
            continue
        if not math.isfinite(a.quantity) or a.quantity <= 0:
            continue
        d = a.date or ""
        if start and d < start:
            continue
        if end and d > end:
            continue
        out[(a.series_key, (a.unit or "").strip())] += float(a.quantity)
    return dict(out)


def enrolment(db: Session, organisation_id: int) -> dict:
    """How much of the inventory is enrolled in period-over-period screening.

    Reported because an unenrolled row is NOT a clean one — it is simply not
    looked at, and a coverage figure that did not say so would read as assurance.
    """
    rows = db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == organisation_id).all()
    total = len(rows)
    enrolled = sum(1 for a in rows if a.series_key)
    return {
        "activities_total": total,
        "activities_enrolled": enrolled,
        "activities_not_enrolled": total - enrolled,
        "enrolled_pct": round(100.0 * enrolled / total, 2) if total else None,
        "distinct_series": len({a.series_key for a in rows if a.series_key}),
        "note": "A row with no declared series_key is NOT screened for "
                "period-over-period change — it is not looked at. The series key is "
                "preparer-declared and never written by the engine: an inferred key "
                "would merge physically distinct sites and report their sum as one "
                "trend.",
    }


def compare(db: Session, organisation_id: int, current_period_id: int,
            baseline_period_id: int) -> dict:
    """Screen a period against a baseline, series by series.

    Refuses rather than guessing: an unresolvable period, a period without dates,
    or no comparable series each produce a stated `status` and no findings. A
    comparison that cannot be calibrated is never reported as a clean one.
    """
    cur = db.query(ReportingPeriod).filter(
        ReportingPeriod.id == current_period_id,
        ReportingPeriod.organisation_id == organisation_id).first()
    base = db.query(ReportingPeriod).filter(
        ReportingPeriod.id == baseline_period_id,
        ReportingPeriod.organisation_id == organisation_id).first()
    if cur is None or base is None:
        return {"available": False, "status": "period_not_found",
                "reason": "current and baseline periods must both belong to this "
                          "organisation"}
    if cur.id == base.id:
        return {"available": False, "status": "same_period",
                "reason": "a period cannot be screened against itself"}
    if not (cur.start_date and cur.end_date and base.start_date and base.end_date):
        return {"available": False, "status": "period_not_dated",
                "reason": "both periods need a start and end date; a comparison "
                          "cannot be scoped without one"}

    # Period LENGTH comparability is a prior question, and the platform already has
    # exactly one detector for it. Do not write a second — services/comparability.py
    # declares the tolerance once so five renderers cannot drift apart.
    from .comparability import PERIOD_TOLERANCE_PCT
    cur_days = _days(cur.start_date, cur.end_date)
    base_days = _days(base.start_date, base.end_date)
    length_comparable = None
    if cur_days and base_days:
        length_comparable = abs(cur_days - base_days) <= base_days * PERIOD_TOLERANCE_PCT

    now_q = _series_quantities(db, organisation_id, cur)
    base_q = _series_quantities(db, organisation_id, base)
    shared = sorted(set(now_q) & set(base_q))

    absent = sorted(set(base_q) - set(now_q))
    appeared = sorted(set(now_q) - set(base_q))

    if not shared:
        return {"available": False, "status": "no_comparable_series",
                "reason": "no declared series has quantities in both periods",
                "enrolment": enrolment(db, organisation_id),
                "series_absent": [{"series_key": k, "unit": u,
                                   "baseline_quantity": round(base_q[(k, u)], 6)}
                                  for k, u in absent],
                "series_new": [{"series_key": k, "unit": u,
                                "current_quantity": round(now_q[(k, u)], 6)}
                               for k, u in appeared]}

    deviations = [math.log(now_q[k] / base_q[k]) for k in shared]
    b = band(deviations)
    med = b["median"]
    t = b["threshold"]

    findings = []
    for key, d in zip(shared, deviations):
        series_key, unit = key
        ratio = now_q[key] / base_q[key]
        backstop = ratio >= HARD_BACKSTOP or ratio <= 1.0 / HARD_BACKSTOP
        adaptive = t is not None and abs(d - med) > t
        if not (backstop or adaptive):
            continue
        findings.append({
            "series_key": series_key, "unit": unit,
            "baseline_quantity": round(base_q[key], 6),
            "current_quantity": round(now_q[key], 6),
            "ratio": round(ratio, 6),
            "log_deviation": round(d, 6),
            "median_log_deviation": round(med, 6),
            "threshold": None if t is None else round(t, 6),
            "triggered_by": "backstop" if backstop and not adaptive
                            else ("both" if backstop and adaptive else "adaptive_band"),
            "direction": "increase" if ratio > 1 else "decrease",
            # The GHG Protocol's own words, so the finding cites its authority.
            "criterion": "GHG Protocol Corporate Standard ch.7: 'changes of over 10 "
                         "percent from year to year may warrant further "
                         "investigation'. Applied here as a robust band on the log "
                         "ratio rather than a flat 10%, because weather and occupancy "
                         "alone move a heating series further than that.",
        })

    return {
        "available": True,
        "status": "screened",
        "version": SERIES_SCREEN_VERSION,
        "current_period": {"id": cur.id, "label": cur.label,
                           "start_date": cur.start_date, "end_date": cur.end_date,
                           "days": cur_days},
        "baseline_period": {"id": base.id, "label": base.label,
                            "start_date": base.start_date, "end_date": base.end_date,
                            "days": base_days},
        "period_length_comparable": length_comparable,
        "period_length_note": (
            None if length_comparable in (True, None) else
            f"the periods differ in length by more than "
            f"{PERIOD_TOLERANCE_PCT:.0%}; a ratio across them mixes a rate change "
            f"with a duration change and the findings below should be read with that "
            f"in mind"),
        "band": {k: (round(v, 6) if isinstance(v, float) else v)
                 for k, v in b.items()},
        "parameters": {
            "k": K, "band_floor_ratio": round(math.exp(BAND_FLOOR), 4),
            "band_cap_ratio": round(math.exp(BAND_CAP), 4),
            "hard_backstop_ratio": HARD_BACKSTOP,
            "min_series_for_band": MIN_SERIES_FOR_BAND,
            "bn_correction": _bn(len(deviations)),
            "note": "Tested on the LOG RATIO of like-for-like periods, never on "
                    "levels: a z-score is bounded at (n-1)/sqrt(n) and cannot exceed "
                    "3 at ten or fewer points (Shiffler 1988), and Tukey fences on "
                    "skewed levels exceed at ~7.6% against 0.7% at the normal.",
        },
        "series_compared": len(shared),
        "findings": findings,
        "series_absent": [{
            "series_key": k, "unit": u,
            "baseline_quantity": round(base_q[(k, u)], 6),
            "note": "present in the baseline period and absent now. A missing bill "
                    "reads as a reduction, which is the most damaging silent error "
                    "in an inventory.",
        } for k, u in absent],
        "series_new": [{
            "series_key": k, "unit": u,
            "current_quantity": round(now_q[(k, u)], 6),
            "note": "no baseline to compare against; reported so a new site is "
                    "visible rather than being screened as though it had always "
                    "existed.",
        } for k, u in appeared],
        "enrolment": enrolment(db, organisation_id),
    }


def _days(start: Optional[str], end: Optional[str]) -> Optional[int]:
    """Inclusive day count between two ISO dates, or None."""
    from datetime import datetime
    try:
        a = datetime.strptime((start or "")[:10], "%Y-%m-%d")
        b = datetime.strptime((end or "")[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return (b - a).days + 1 if b >= a else None


def classify_level_shifts(history: list) -> dict:
    """Reclassify a persistent same-direction deviation as a level shift.

    `history` is a chronological list of per-period comparison payloads for the
    same organisation. A series that flags in the same direction across
    LEVEL_SHIFT_MIN_RUNS consecutive comparisons, with its deviations agreeing
    within half the threshold, is a legitimate step change — a site opening, a
    line commissioned — not a data defect. ISAE 3410 A101 names exactly this:
    trends must be read "for consistency with other circumstances such as the
    acquisition or disposal of facilities".
    """
    runs = defaultdict(list)
    for payload in history:
        if not payload.get("available"):
            continue
        for f in payload.get("findings", []):
            runs[(f["series_key"], f["unit"])].append(f)

    shifts, anomalies = [], []
    for key, flags in runs.items():
        if len(flags) < LEVEL_SHIFT_MIN_RUNS:
            anomalies.extend(flags)
            continue
        directions = {f["direction"] for f in flags}
        devs = [f["log_deviation"] for f in flags]
        thresholds = [f["threshold"] for f in flags if f["threshold"] is not None]
        spread = max(devs) - min(devs)
        tol = 0.5 * (min(thresholds) if thresholds else BAND_CAP)
        if len(directions) == 1 and spread < tol:
            shifts.append({
                "series_key": key[0], "unit": key[1],
                "direction": directions.pop(),
                "consecutive_periods": len(flags),
                "log_deviation_spread": round(spread, 6),
                "check_code": "level_shift",
                "severity": "informational",
                "routing": "base_year_recalculation",
                "note": "the same series moved in the same direction across "
                        f"{len(flags)} consecutive comparisons with deviations "
                        f"agreeing within half the band — a persistent step change, "
                        f"not a data defect. ISAE 3410 A101 requires trends to be read "
                        f"for consistency with circumstances such as the acquisition "
                        f"or disposal of facilities. Route to the GHG Protocol "
                        f"base-year recalculation policy.",
            })
        else:
            anomalies.extend(flags)
    return {"level_shifts": shifts, "anomalies": anomalies,
            "min_consecutive_periods": LEVEL_SHIFT_MIN_RUNS}
