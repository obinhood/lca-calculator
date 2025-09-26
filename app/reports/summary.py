from sqlalchemy.orm import Session
from sqlalchemy import func
from ..models import ActivityRecord, Result

def summary(db: Session):
    # Total
    total = db.query(func.sum(Result.co2e)).scalar() or 0.0

    # By scope
    by_scope = db.query(ActivityRecord.scope, func.sum(Result.co2e))\
        .join(Result, Result.activity_id == ActivityRecord.id)\
        .group_by(ActivityRecord.scope).all()

    # By category
    by_cat = db.query(ActivityRecord.category, func.sum(Result.co2e))\
        .join(Result, Result.activity_id == ActivityRecord.id)\
        .group_by(ActivityRecord.category).all()

    return {
        "total_co2e": total,
        "by_scope": [{"scope": s or "?", "co2e": v or 0.0} for s, v in by_scope],
        "by_category": [{"category": c or "?", "co2e": v or 0.0} for c, v in by_cat],
        "notes": "MVP results. Units assumed matched to factors; refine with pint for conversions."
    }
