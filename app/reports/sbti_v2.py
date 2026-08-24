"""SBTi V2.0 report over an immutable run.

Reads only what the run froze, like every other renderer, so a filed assessment
does not move when activities are re-mapped.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CalculationRun
from ..services.frozen import parse_detail
from ..services.sbti_v2 import (
    SBTI_V2_VERSION, company_category, significance, target_boundary,
    version_applicability,
)


def _scope3_by_category(db: Session, run: CalculationRun) -> dict:
    """tCO2e per GHGP Scope 3 category, from FROZEN line detail."""
    from ..models import EmissionLineItem
    rows = db.query(EmissionLineItem.details, EmissionLineItem.co2e).filter(
        EmissionLineItem.run_id == run.id,
        EmissionLineItem.method == "location",
        EmissionLineItem.scope == "3").all()
    out = {}
    for details, co2e in rows:
        cat = parse_detail(details).get("ghgp_category")
        if isinstance(cat, int):
            out[cat] = out.get(cat, 0.0) + (co2e or 0.0) / 1000.0
    return out


def sbti_v2_report(db: Session, run: CalculationRun, *,
                   turnover_eur: Optional[float] = None,
                   fte: Optional[int] = None,
                   balance_sheet_eur: Optional[float] = None,
                   high_income_country: Optional[bool] = None) -> dict:
    by_cat = _scope3_by_category(db, run)
    # Which categories the run's FROZEN Scope 3 screen actually decided on. Without this,
    # a category with no lines is indistinguishable from one measured at zero: it left the
    # denominator, took a 0.0% share, and dropped out of the C14.1 required boundary — a
    # positive finding manufactured from an absence.
    #
    # ANTI-CLIFF: a run computed before declarations were frozen has no rows here. Passing
    # an empty set would declare all fourteen categories un-inventoried and retroactively
    # block every legacy run, so the stricter rule applies only where a screen was frozen.
    # A NULL sentinel is evidence about the run, not a missing value.
    from ..models import RunScope3Declaration
    declared = {c for (c,) in db.query(RunScope3Declaration.category)
                .filter(RunScope3Declaration.run_id == run.id).all()
                if isinstance(c, int)}
    sig = (significance(by_cat, inventoried=declared) if declared
           else significance(by_cat))
    cat = company_category(turnover_eur=turnover_eur, fte=fte,
                           scope12_tco2e=_scope12_tco2e(db, run),
                           balance_sheet_eur=balance_sheet_eur,
                           high_income_country=high_income_country)
    from ..models import ReportingPeriod
    period_start = None
    if run.reporting_period_id:
        p = db.query(ReportingPeriod).filter(
            ReportingPeriod.id == run.reporting_period_id).first()
        period_start = getattr(p, "start_date", None) if p else None

    return {
        "framework": "SBTi Corporate Net-Zero Standard V2.0",
        "standard_version": SBTI_V2_VERSION,
        "run": {"id": run.id, "created_at": run.created_at},
        "company_category": cat,
        "significance": sig,
        "significance_basis": (
            "Categories the run's frozen Scope 3 screen decided on; one it never "
            "inventoried suspends the answer rather than counting as zero."
            if declared else
            "This run predates frozen Scope 3 declarations, so an un-inventoried "
            "category cannot be told from one measured at zero. The shares below may be "
            "overstated by however much of categories 1-14 was never screened. Recompute "
            "the run to get the stricter test."),
        "target_boundary": target_boundary(sig),
        "version_applicability": version_applicability(period_start),
        "note": "Significance is determined on the PHYSICAL inventory. Market "
                "instruments are accounted separately and never netted into it "
                "(C5.4, C37.4).",
    }


def _scope12_tco2e(db: Session, run: CalculationRun) -> Optional[float]:
    from ..models import EmissionLineItem
    from sqlalchemy import func
    v = db.query(func.sum(EmissionLineItem.co2e)).filter(
        EmissionLineItem.run_id == run.id,
        EmissionLineItem.method == "location",
        EmissionLineItem.scope.in_(("1", "2"))).scalar()
    return None if v is None else v / 1000.0
