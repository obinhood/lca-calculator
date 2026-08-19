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
from .frozen import parse_detail

SBTI_MIN_ANNUAL_RATE = {"1.5C": 0.042, "WB2C": 0.025}
# SBTi net-zero / long-term: minimum ~90% absolute reduction (residual offset).
NET_ZERO_MIN_REDUCTION = 0.90
VALID_SCOPES = {"1", "2", "3"}


def scopes_from_coverage(coverage: str) -> set:
    """'1+2' -> {'1','2'}; '1+2+3' -> {'1','2','3'}. Raises on unknown tokens."""
    tokens = {s.strip() for s in (coverage or "").split("+") if s.strip()}
    bad = tokens - VALID_SCOPES
    if bad:
        raise ValueError(f"invalid scope(s) in coverage {coverage!r}: {sorted(bad)}")
    return tokens


def run_scoped_emissions_kg(db: Session, run_id: int, coverage: str) -> float:
    """Location-based emissions (kg) of a run, restricted to the covered scopes.

    Includes PCAF financed emissions when the coverage takes in Scope 3: they are Scope 3
    Category 15, but they live in RunFinancedLine rather than EmissionLineItem, so a plain
    line-item sum dropped them. For a financial institution that is most of the inventory,
    and omitting it from BOTH the base year and the actuals made a target look on track
    against a fraction of what it covers.
    """
    scopes = scopes_from_coverage(coverage)
    if not scopes:
        return 0.0
    total = db.query(func.sum(EmissionLineItem.co2e)).filter(
        EmissionLineItem.run_id == run_id,
        EmissionLineItem.method == "location",
        EmissionLineItem.scope.in_(scopes)).scalar() or 0.0
    if "3" in scopes:
        total += _financed_component(db, run_id) or 0.0
    return total


def _financed_component(db: Session, run_id: int) -> Optional[float]:
    """Financed emissions to include for a Scope-3-covering target, or None.

    None means "the PCAF dimension was not evaluated on this run" — distinct from 0.0,
    which means "evaluated, and nil". Flattening the two with `or 0.0` let a base year
    WITH financed emissions be compared against actuals WITHOUT them, manufacturing a
    71% reduction from an organisation whose own operations had not changed at all.

    Also returns None when Category 15 is declared through BOTH activity lines and a PCAF
    portfolio: adding them double-counts the same investee, which is the sum `summary.py`
    and `cdp.py` refuse to publish.
    """
    from ..models import CalculationRun
    run = db.get(CalculationRun, run_id)
    if run is None or run.financed_co2e is None:
        return None
    if _cat15_double_declared(db, run_id):
        return None
    return run.financed_co2e


def _cat15_double_declared(db: Session, run_id: int) -> bool:
    import json
    from ..models import EmissionLineItem
    rows = db.query(EmissionLineItem.details).filter(
        EmissionLineItem.run_id == run_id,
        EmissionLineItem.method == "location",
        EmissionLineItem.scope == "3",
        EmissionLineItem.co2e > 0).all()
    for (details,) in rows:
        try:
            if parse_detail(details).get("ghgp_category") == 15:
                return True
        except ValueError:
            continue
    return False


def financed_comparable(db: Session, base_run_id: int, current_run_id: int,
                        coverage: str) -> Optional[str]:
    """A blocker string when the two runs' financed dimensions are not comparable.

    A target's base year and its actuals must both include financed emissions or both
    exclude them. Mixing the two moves the trajectory by the whole portfolio.
    """
    if "3" not in scopes_from_coverage(coverage):
        return None
    b, c = _financed_component(db, base_run_id), _financed_component(db, current_run_id)
    if (b is None) == (c is None):
        return None
    have, lack = ("base year", "current run") if c is None else ("current run", "base year")
    return (f"the {have} includes PCAF financed emissions (Scope 3 Category 15) and the "
            f"{lack} does not — this target covers Scope 3, so comparing them would "
            f"report the whole portfolio as a change in the organisation's own emissions. "
            f"Recompute both runs with the financed dimension evaluated.")


def financed_included(db: Session, run_id: int, coverage: str) -> Optional[float]:
    """The financed component actually included, for disclosure. None when the coverage
    excludes Scope 3, the dimension was not evaluated, or Cat 15 is double-declared —
    each of which is "not included", and none of which is "zero"."""
    if "3" not in scopes_from_coverage(coverage):
        return None
    return _financed_component(db, run_id)


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
                    ambition: Optional[str], target_type: str = "near_term") -> dict:
    """Assess a target against the SBTi criterion for its TYPE.

    Near-term targets use the linear annual-reduction floor (4.2% for 1.5C,
    2.5% for WB2C). Long-term/net-zero targets are judged against the ~90%
    absolute-reduction requirement, NOT the annual floor (a long horizon
    legitimately dilutes the yearly rate below 4.2%).
    """
    rate = implied_annual_rate(target_reduction_pct, base_year, target_year)
    out = {
        "target_type": target_type,
        "ambition": ambition,
        "implied_annual_linear_rate": round(rate, 4) if rate is not None else None,
    }
    if target_type in ("long_term", "net_zero"):
        out["criterion"] = f">= {int(NET_ZERO_MIN_REDUCTION * 100)}% absolute reduction (net-zero)"
        out["minimum_reduction_pct"] = NET_ZERO_MIN_REDUCTION
        out["meets_minimum"] = target_reduction_pct + 1e-9 >= NET_ZERO_MIN_REDUCTION
    else:
        minimum = SBTI_MIN_ANNUAL_RATE.get(ambition or "")
        out["criterion"] = (f">= {minimum:.1%}/yr linear (near-term {ambition})"
                            if minimum else "near-term ambition not recognised")
        out["minimum_annual_rate"] = minimum
        out["meets_minimum"] = (rate + 1e-9 >= minimum) if (rate is not None
                                                            and minimum is not None) else None
    return out
