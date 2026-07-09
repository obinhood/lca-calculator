import json
import hashlib
from datetime import datetime, timezone
from typing import Optional, List
from sqlalchemy.orm import Session
from ..models import (
    ActivityRecord, EmissionFactor, CalculationRun, EmissionLineItem, ReportingPeriod,
)
from .units import convert, UnitConversionError, QuantityError

SCOPE_RULES = {
    "electricity":"2",
    "gas":"1",
    "diesel":"1",
    "flight":"3",
    "train":"3",
    "car":"3",
    "waste":"3",
    "spend":"3"
}

class ReportingPeriodError(ValueError):
    """Invalid reporting period for a calculation (wrong org, frozen, or missing)."""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def activities_fingerprint(acts: List[ActivityRecord]) -> str:
    """Stable hash of the activity set (id/factor/quantity/unit).

    Changes if any activity is added, removed, re-mapped, or edited — even when the
    activity count is unchanged — so a run computed against this set can be detected
    as stale by content, not just by count.
    """
    parts = sorted(f"{a.id}:{a.factor_id}:{a.quantity}:{a.unit}" for a in acts)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

def compute_activity_co2e(quantity: Optional[float], unit: str, factor: EmissionFactor) -> float:
    """kg CO2e for one activity.

    The quantity is converted from the activity's unit into the factor's unit
    BEFORE multiplying. Incompatible units raise ``UnitConversionError`` and a
    None/non-finite/non-numeric quantity raises ``QuantityError`` (a subclass) —
    a wrong-by-orders-of-magnitude number is worse than a rejected row (Gap 1).
    """
    if factor is None:
        raise ValueError("no emission factor supplied")
    qty_in_factor_unit = convert(quantity, unit, factor.unit)
    return qty_in_factor_unit * factor.value

def compute_co2e(db: Session, organisation_id: int, gwp_set: str = "AR6",
                 reporting_period_id: Optional[int] = None) -> CalculationRun:
    """Create a NEW immutable calculation run for one organisation and return it.

    Prior runs are never mutated or deleted (Gap 5), and the calculation is scoped
    to a single organisation's activities (Gap 6 / multi-tenancy). Every activity
    lands in exactly one bucket — mapped | unmapped | unit_errors | data_errors |
    gwp_mismatch — recorded on the run's frozen coverage snapshot; excluded
    activities are surfaced, never silently dropped (Gap 4).

    If ``reporting_period_id`` is supplied it must belong to this organisation and
    not be frozen, and only activities dated within the period window are included.
    """
    period = None
    if reporting_period_id is not None:
        period = db.get(ReportingPeriod, reporting_period_id)
        if period is None or period.organisation_id != organisation_id:
            raise ReportingPeriodError("reporting period not found for this organisation")
        if period.frozen:
            raise ReportingPeriodError("reporting period is frozen; cannot create a new run")

    q = db.query(ActivityRecord).filter(ActivityRecord.organisation_id == organisation_id)
    if period is not None:
        # ISO date strings sort lexicographically, so string comparison is a valid range filter.
        if period.start_date:
            q = q.filter(ActivityRecord.date >= period.start_date)
        if period.end_date:
            q = q.filter(ActivityRecord.date <= period.end_date)
    acts = q.all()

    run = CalculationRun(
        organisation_id=organisation_id,
        reporting_period_id=reporting_period_id,
        created_at=_utcnow_iso(),
        gwp_set=gwp_set,
        status="pending",
        total_activities=len(acts),
        mapped=0, unmapped=0, unit_errors=0, data_errors=0, gwp_mismatch=0,
        total_co2e=0.0,
        activities_fingerprint=activities_fingerprint(acts),
    )
    errors = []
    try:
        db.add(run)
        db.flush()  # assign run.id for the line-item FK

        line_items = []
        total = 0.0
        for a in acts:
            if not a.factor:
                run.unmapped += 1
                continue
            if a.factor.gwp_set and gwp_set and a.factor.gwp_set != gwp_set:
                run.gwp_mismatch += 1
                errors.append({"activity_id": a.id,
                               "error": f"factor GWP set {a.factor.gwp_set} != requested {gwp_set}"})
                continue
            if a.quantity is not None and a.quantity < 0:
                run.data_errors += 1
                errors.append({"activity_id": a.id, "error": "negative quantity"})
                continue
            try:
                co2e = compute_activity_co2e(a.quantity, a.unit, a.factor)
            except QuantityError as exc:          # None / non-finite / non-numeric
                run.data_errors += 1
                errors.append({"activity_id": a.id, "error": str(exc)})
                continue
            except UnitConversionError as exc:    # incompatible / ambiguous / malformed
                run.unit_errors += 1
                errors.append({"activity_id": a.id, "error": str(exc)})
                continue

            scope = a.scope or SCOPE_RULES.get((a.category or "").lower(), "3")
            a.scope = scope
            line_items.append(EmissionLineItem(
                run_id=run.id, activity_id=a.id, scope=scope, method="location", co2e=co2e,
                details=json.dumps({
                    "factor_id": a.factor_id,
                    "gwp_set": a.factor.gwp_set,
                    "activity_unit": a.unit,
                    "factor_unit": a.factor.unit,
                    "quantity": a.quantity,
                    "factor_value": a.factor.value,
                }),
            ))
            total += co2e
            run.mapped += 1

        db.add_all(line_items)
        run.total_co2e = total
        run.notes = json.dumps(errors)
        run.status = "complete"
        db.commit()
    except Exception:
        db.rollback()
        raise

    db.refresh(run)
    return run
