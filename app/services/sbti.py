"""SBTi-style target maths: linear pathways and minimum-ambition assessment.

A near-term 1.5C-aligned target requires a minimum linear annual reduction of
4.2% of base-year emissions (SBTi Corporate Net-Zero Standard); well-below-2C
uses ~2.5%. The pathway is a straight line from the base year to the target
year; trajectory tracking compares an actual run's scoped emissions to the
pathway value for that year.
"""
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from ..models import EmissionLineItem

SBTI_MIN_ANNUAL_RATE = {"1.5C": 0.042, "WB2C": 0.025}


def scopes_from_coverage(coverage: str) -> set:
    """'1+2' -> {'1','2'}; '1+2+3' -> {'1','2','3'}."""
    return {s.strip() for s in (coverage or "").split("+") if s.strip()}


def run_scoped_emissions_kg(db: Session, run_id: int, coverage: str) -> float:
    """Location-based emissions (kg) of a run, restricted to the covered scopes."""
    scopes = scopes_from_coverage(coverage)
    if not scopes:
        return 0.0
    total = db.query(func.sum(EmissionLineItem.co2e)).filter(
        EmissionLineItem.run_id == run_id,
        EmissionLineItem.method == "location",
        EmissionLineItem.scope.in_(scopes)).scalar()
    return total or 0.0


def linear_pathway(base_emissions: float, base_year: int, target_year: int,
                   target_reduction_pct: float, year: int) -> float:
    """Allowed emissions on the linear pathway at ``year``."""
    if year <= base_year:
        return base_emissions
    if year >= target_year:
        return base_emissions * (1.0 - target_reduction_pct)
    frac = (year - base_year) / (target_year - base_year)
    return base_emissions * (1.0 - target_reduction_pct * frac)


def implied_annual_rate(target_reduction_pct: float, base_year: int,
                        target_year: int) -> Optional[float]:
    years = target_year - base_year
    if years <= 0:
        return None
    return target_reduction_pct / years


def assess_ambition(target_reduction_pct: float, base_year: int, target_year: int,
                    ambition: Optional[str]) -> dict:
    rate = implied_annual_rate(target_reduction_pct, base_year, target_year)
    minimum = SBTI_MIN_ANNUAL_RATE.get(ambition or "")
    meets = None
    if rate is not None and minimum is not None:
        meets = rate + 1e-9 >= minimum
    return {
        "ambition": ambition,
        "implied_annual_linear_rate": round(rate, 4) if rate is not None else None,
        "minimum_annual_rate": minimum,
        "meets_minimum": meets,
    }
