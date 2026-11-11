"""SBTi V2.0 report over an immutable run.

Reads only what the run froze, like every other renderer, so a filed assessment
does not move when activities are re-mapped.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CalculationRun, RunScope3Declaration
from ..services.frozen import parse_detail
from ..services.comparability import cross_run_gates
from ..services.calc import scope_declaration_completeness
from ..services.sbti_v2 import (
    SBTI_V2_VERSION, HIGH_INCOME_SCOPE12_TCO2E,
    company_category, significance, target_boundary,
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
    # THE SCOPE SPLIT IS NOT AN INPUT TO SIGNIFICANCE — IT DEFINES THE DENOMINATOR.
    # `_scope3_by_category` buckets every line the run froze as scope "3", and an activity
    # category outside SCOPE_RULES is defaulted to Scope 3 and frozen `assumed_scope3`. So
    # on a run whose categories all miss that dict the ENTIRE inventory — purchased steam
    # (really Scope 2) and a refrigerant leak (really Scope 1) included — enters the
    # categories-1-14 denominator as Scope 3 category emissions. Measured on the matched
    # fixture: the denominator reports 43.096 tCO2e with category 1 at a 100% share, where
    # the same sources, quantities and factor values with RECOGNISED categories have no
    # Scope 3 at all. Those are emissions provably not in Scope 3 setting the 5% test.
    #
    # REFUSED, NOT CAVEATED, and the file's own note is the reason why rather than against:
    # significance is determined on the PHYSICAL inventory, so it rests on that inventory's
    # scope split exactly as the mandatory Scope 1 + 2 figure does. A guessed split makes
    # the answer unknown, not merely uncertain, and `target_boundary` turns the 5% set into
    # a conformance verdict — a genuinely significant category screened out as immaterial
    # states the wrong required boundary.
    #
    # The refusal reuses the shape `significance()` already returns when a category was
    # never inventoried, so `target_boundary()` derives its own refusal from it through
    # machinery that exists instead of a second partial copy of the rule. `denominator_tco2e`
    # is None, not 0.0: unknown is a different fact from measured-at-zero, which is what the
    # declared-scope run with no value chain legitimately reports.
    _scope_gate = scope_declaration_completeness(db, run)
    if _scope_gate["blockers"]:
        sig = {
            "determinable": False,
            "reason": "the scope split this run froze is not a declaration — "
                      + "; ".join(_scope_gate["blockers"])
                      + ". Every line assumed into Scope 3 enters the categories 1-14 "
                        "denominator, so the 5% significance test and the C14.1 target "
                        "boundary derived from it would be computed against a denominator "
                        "inflated by emissions that are not Scope 3 at all.",
            "denominator_tco2e": None,
            "significant": [], "shares": {},
            "categories_not_inventoried": [],
            "scope_declaration_blockers": _scope_gate["blockers"],
            "assumed_scope3_by_category": _scope_gate["assumed_scope3_by_category"],
            "lines_unassessable": _scope_gate["lines_unassessable"],
        }
    else:
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
        # `cross_run_gates` answers "can these two runs be subtracted?" and knows nothing
        # about scope_source — a single run's split being guessed is a different question
        # from the two runs being comparable, so the scope gate is asked here per run
        # rather than folded into it (that would make every two-run renderer refuse for a
        # one-run reason).
        #
        # Asked at all because WITHOUT it this block fails open on the refusal above:
        # `recalculation_triggers` runs its crossing test only when BOTH significance
        # results are determinable, so a refused `sig` silently yields zero triggers and
        # this block would then stamp `determinable: True, recalculation_required: False`
        # over it — "no recalculation is due" manufactured out of a refusal.
        for _label, _r in (("base", base_run), ("current", run)):
            blockers.extend(
                f"{_label} run: {b}" for b in
                scope_declaration_completeness(db, _r).get("blockers", []))
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
    # company_category consults the INVENTORY in exactly one place: the A.1 high-income
    # test "scope 1+2 >= 10,000 tCO2e", and only where no turnover/FTE threshold has
    # already settled Category A. `_scope12_tco2e` sums scope IN ("1","2"), which on a run
    # whose split was guessed is a sum over ZERO rows — SQL SUM returns NULL — so the
    # figure arrives as None and that test cannot fire. Verified by execution: a
    # high-income company with a real Scope 1 + 2 of 12,000 tCO2e is Category A; with the
    # same emissions assumed into Scope 3 it falls through to Category B.
    #
    # That is exactly the harm this function's own docstring says it refuses — "defaulting
    # to Category B would let a large company past the Category A criteria that only apply
    # to it". It refuses when NOTHING is supplied; here turnover and FTE ARE supplied, so
    # it answers confidently over the one figure that went missing.
    cat = company_category(turnover_eur=turnover_eur, fte=fte,
                           scope12_tco2e=_scope12_tco2e(db, run),
                           balance_sheet_eur=balance_sheet_eur,
                           high_income_country=high_income_country)
    # All three conjuncts, because only their conjunction rests on the guess. Without
    # `high_income_country` the Scope 1 + 2 figure is never read at all (execution-verified:
    # the category is B either way), and a Category A already established by turnover or
    # FTE is reached before that branch and so is unaffected. A "B" under a high-income
    # flag is the one answer that could have been an A but for the absent figure — gating
    # the others would refuse answers the guess did not touch.
    if _scope_gate["blockers"] and high_income_country and cat.get("category") == "B":
        cat = {
            "determinable": False, "category": None,
            "reason": "Category B here rests on a Scope 1 + 2 figure the run never "
                      "produced: " + "; ".join(_scope_gate["blockers"])
                      + ". In a high-income country the A.1 test 'scope 1+2 >= "
                        f"{HIGH_INCOME_SCOPE12_TCO2E:,} tCO2e' is what separates A from B "
                        "at this size, and every tonne that would answer it was assumed "
                        "into Scope 3, so B cannot be distinguished from an A whose "
                        "emissions were misfiled",
            "scope_declaration_blockers": _scope_gate["blockers"],
        }
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
        # The gate's own finding, named, so a reader sees WHICH categories were assumed
        # rather than only that something was refused. Always present: on a clean run it
        # is the positive statement that the split was declared, which is not the same
        # fact as a refusal being absent.
        "scope_declaration": _scope_gate,
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
