"""SBTi V2.0 report over an immutable run.

Reads only what the run froze, like every other renderer, so a filed assessment
does not move when activities are re-mapped.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CalculationRun, RunScope3Declaration
from ..services.frozen import parse_detail
from ..services.comparability import cross_run_gates
from ..services.sbti_v2 import (
    SBTI_V2_VERSION, company_category, significance, target_boundary,
    recalculation_triggers,
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
                   high_income_country: Optional[bool] = None,
                   base_run: Optional[CalculationRun] = None) -> dict:
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
    declared = {c for (c,) in db.query(RunScope3Declaration.category)
                .filter(RunScope3Declaration.run_id == run.id).all()
                if isinstance(c, int)}
    sig = (significance(by_cat, inventoried=declared) if declared
           else significance(by_cat))

    # C8.3 recalculation triggers. README listed these as shipped while the function had
    # ZERO callers — measurable, correct, and unreachable. Supplying a base run makes the
    # comparison, and omitting it OMITS THE KEY rather than reporting a bare False: "we
    # were not asked" is not "no recalculation is required".
    recalculation = None
    if base_run is not None:
        base_cat = _scope3_by_category(db, base_run)
        base_declared = {c for (c,) in db.query(RunScope3Declaration.category)
                         .filter(RunScope3Declaration.run_id == base_run.id).all()
                         if isinstance(c, int)}
        base_sig = (significance(base_cat, inventoried=base_declared) if base_declared
                    else significance(base_cat))
        # The same question every other two-run comparison has to ask. C8.3 asks which
        # categories CROSSED the 5% line; across a boundary change or a GWP vintage the
        # shares moved for reasons that have nothing to do with the company's emissions,
        # so a "crossing" would be an artefact of the comparison.
        blockers = cross_run_gates(db, base_run, run, label_a="base", label_b="current",
                                   quantity="the C8.3 recalculation test",
                                   measures="a change in significance")
        if blockers:
            recalculation = {
                "determinable": False,
                "base_run_id": base_run.id,
                "blockers": blockers,
                "reason": "the base and current runs are not comparable, so a category "
                          "appearing to cross the 5% line cannot be attributed to a "
                          "change in the inventory rather than to the comparison itself",
            }
        else:
            recalculation = recalculation_triggers(base_sig, sig)
            recalculation["determinable"] = True
            recalculation["base_run_id"] = base_run.id
            recalculation["blockers"] = []
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
        "recalculation": recalculation,
        "recalculation_note": (
            "C8.3 triggers are computed only when base_run_id is supplied. A null here "
            "means the comparison was not requested, NOT that no recalculation is due."
        ) if recalculation is None else None,
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
