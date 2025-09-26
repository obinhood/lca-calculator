import json
from sqlalchemy.orm import Session
from ..models import ActivityRecord, EmissionFactor, Result

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

def compute_activity_co2e(quantity: float, unit: str, factor: EmissionFactor) -> float:
    # MVP assumes units match factor units (e.g., kWh with per kWh factor).
    # Extend with pint for robust conversions.
    return (quantity or 0.0) * (factor.value if factor else 0.0)

def compute_co2e(db: Session, gwp_set: str="AR6") -> None:
    # Clear old results (MVP simplicity)
    db.query(Result).delete()
    db.commit()

    acts = db.query(ActivityRecord).all()
    for a in acts:
        if not a.factor:
            continue
        co2e = compute_activity_co2e(a.quantity, a.unit, a.factor)
        a.scope = a.scope or SCOPE_RULES.get((a.category or "").lower(), "3")
        res = Result(activity_id=a.id, co2e=co2e, details=json.dumps({
            "factor_id": a.factor_id, "gwp_set": a.factor.gwp_set, "unit": a.unit, "quantity": a.quantity
        }))
        db.add(res)
    db.commit()
