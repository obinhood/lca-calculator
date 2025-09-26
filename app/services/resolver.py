from sqlalchemy.orm import Session
from rapidfuzz import process, fuzz
from typing import Optional, Tuple
from ..models import EmissionFactor, ActivityRecord

# Deterministic priority: exact category+subcategory+geo -> category+geo -> category
def resolve_factor(db: Session, category: str, subcategory: str, geo: str, gwp_set: str="AR6", year: Optional[int]=None) -> Optional[EmissionFactor]:
    q = db.query(EmissionFactor).filter(EmissionFactor.gwp_set == gwp_set)
    if year:
        q = q.filter(EmissionFactor.year == year)
    # Exact combo
    ef = q.filter(EmissionFactor.category == category,
                  EmissionFactor.subcategory == (subcategory or ""),
                  EmissionFactor.geography == (geo or "Global")).first()
    if ef: return ef
    # Category + Geo
    ef = q.filter(EmissionFactor.category == category,
                  EmissionFactor.geography == (geo or "Global")).first()
    if ef: return ef
    # Category only
    ef = q.filter(EmissionFactor.category == category).first()
    return ef

def suggest_subcategory(db: Session, category: str, text: str) -> Optional[str]:
    subs = [r[0] for r in db.query(EmissionFactor.subcategory).filter(EmissionFactor.category==category).distinct().all()]
    if not subs: return None
    match = process.extractOne(text or "", subs, scorer=fuzz.WRatio)
    if match and match[1] >= 75:
        return match[0]
    return None
