"""ISO 14064-2 — quantification of GHG emission REDUCTIONS and removal enhancements at
the PROJECT level.

This is a different animal from every inventory renderer on the platform. An inventory
(ISO 14064-1 / the GHG Protocol) answers "what did this organisation emit?". A 14064-2
project answers "how much did this *intervention* change emissions relative to what would
have happened without it?" — a delta between a PROJECT scenario and a counterfactual
BASELINE scenario, minus leakage.

The engine can compute the delta exactly: a project scenario is one immutable
CalculationRun, the baseline scenario is another, and the reduction is their difference —
the same exact-delta-between-two-immutable-runs mechanic GRI 305-5 already uses, with the
same GWP-vintage and residual-mix comparability guards (a "reduction" that spans a
methodology change is an artefact, not abatement).

What the engine CANNOT do — and so fails closed on rather than fabricating — is the part
that makes a 14064-2 assertion credible:

  * The BASELINE is a justified counterfactual, not a prior year. Absent a documented
    justification for why the baseline scenario is what would have happened without the
    project, the whole reduction is unsubstantiated. Required, not optional.
  * LEAKAGE — emission increases OUTSIDE the project boundary caused by the project — must
    be assessed and subtracted. Silently assuming zero would overstate every reduction, so
    an unquantified leakage is a blocker (0 is allowed ONLY as the result of a documented
    assessment, which is the caller's assertion to make).
  * CONSERVATIVENESS: where uncertain, 14064-2 requires estimates that do not overstate the
    reduction. The engine surfaces the principle and the inputs; it cannot audit the data.

And the firewall that matters most: a project reduction is a SEPARATE account from the
organisation's corporate inventory. Adding it to (or netting it against) the corporate
inventory double-counts. That warning is on every payload, ready or not.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CalculationRun
from .summary import summary
from ..services.residual_mix import residual_mix_comparable
from ..services.comparability import (period_comparable, period_payload,
                                      cross_run_gates)
from ..services.boundary import boundary_comparable

# Things a credible 14064-2 assertion requires that a calculation engine does not produce.
# Listed so their absence is explicit, never implied complete.
_NOT_PRODUCED = [
    "identification of the GHG sources, sinks and reservoirs (SSRs) relevant to the "
    "baseline and project scenarios, and the criteria for their inclusion/exclusion",
    "the baseline-scenario procedure itself (performance-standard or project-specific) — "
    "the engine takes the baseline as a supplied calculation run and its justification as "
    "supplied text; it does not derive or defend the counterfactual",
    "an uncertainty assessment for the quantified reduction",
    "a monitoring plan and monitored data management (14064-2 Clause 5.11+)",
    "distinction of emission REDUCTIONS from removal ENHANCEMENTS as separate line items — "
    "the delta here is on the fossil CO2e total; biogenic removals are tracked separately "
    "by the engine and are not folded into this figure",
    "independent validation of the project plan or verification of the assertion "
    "(ISO 14064-3) — this is the quantitative core a validator/verifier would check, not a "
    "validated or verified statement",
]


def iso_14064_2_report(db: Session, organisation_id: int,
                       baseline_run_id: Optional[int] = None,
                       project_run_id: Optional[int] = None,
                       leakage_tco2e: Optional[float] = None,
                       baseline_justification: Optional[str] = None,
                       project_name: Optional[str] = None) -> dict:
    """Quantify a project GHG reduction as (baseline - project - leakage) across two
    immutable runs of the SAME organisation.

    All of `baseline_run_id`, `project_run_id`, a documented `baseline_justification`, and a
    quantified `leakage_tco2e` are required for a substantiated assertion; each absence is a
    blocker, not a silent default.
    """
    blockers = []

    if baseline_run_id is None or project_run_id is None:
        return {
            "framework": "ISO 14064-2 (GHG project quantification)",
            "disclosure_ready": False,
            "blockers": ["both baseline_run_id and project_run_id are required — a "
                         "14064-2 reduction is the difference between a project scenario "
                         "run and a baseline scenario run"],
        }

    # A project cannot be its own baseline: the delta would be a trivial zero and read as a
    # (nonsensical) fully-substantiated no-op reduction.
    if baseline_run_id == project_run_id:
        blockers.append(
            "baseline_run_id and project_run_id are the same run — a project scenario "
            "cannot be its own baseline (the reduction would be a trivial zero)")

    # Both runs must exist AND belong to this organisation (id filter closes IDOR: another
    # tenant's run resolves to None, reported as 'not found').
    base = db.query(CalculationRun).filter(
        CalculationRun.id == baseline_run_id,
        CalculationRun.organisation_id == organisation_id).first()
    proj = db.query(CalculationRun).filter(
        CalculationRun.id == project_run_id,
        CalculationRun.organisation_id == organisation_id).first()
    if base is None:
        blockers.append("baseline_run_id not found for this organisation")
    if proj is None:
        blockers.append("project_run_id not found for this organisation")
    if base is None or proj is None:
        return {"framework": "ISO 14064-2 (GHG project quantification)",
                "disclosure_ready": False, "blockers": blockers}

    # Each scenario run must be a SOUND computation of its own scenario. The gate here is
    # PARTIAL — did the run silently drop activities it could not compute (unmapped factor,
    # bad unit)? — which is an IMMUTABLE property of the frozen run and understates that
    # scenario's total on its side of the delta.
    #
    # Deliberately NOT gated: staleness-vs-current-data. A 14064-2 baseline is a FROZEN
    # counterfactual and the project scenario is equally a frozen snapshot; "this run no
    # longer matches today's activities — recompute it" is the wrong instruction for a
    # scenario that is supposed to stay fixed. Immutability of the run is the guarantee,
    # not currency. Staleness (if any) is surfaced as an informational note, not a blocker.
    scenario_notes = {}
    for label, run in (("baseline", base), ("project", proj)):
        s = summary(db, organisation_id=organisation_id, run_id=run.id)
        if s.get("partial"):
            blockers.append(f"{label} run is PARTIAL — it silently excluded activities and "
                            f"understates that scenario's total: {s.get('partial_reasons')}")
        # An EMPTY run (zero activities) is the one incompleteness `partial` cannot see:
        # partial is mapped < total_activities, i.e. 0 < 0 = False, so a run computed over
        # NO activities looks complete with a 0 kg total. On the project side that 0 would
        # be dressed up as a 100%-of-baseline reduction; on the baseline side it makes the
        # project look catastrophically worse. Gate it explicitly on the FROZEN run count
        # (never against current data — that would be the staleness gate this renderer
        # deliberately omits). For any run with total_activities > 0, `partial` already
        # subsumes coverage < 100%, so this closes the single remaining gap.
        if (run.total_activities or 0) == 0:
            blockers.append(f"{label} run computed over ZERO activities — an empty scenario "
                            f"total (0 kg) is not a substantiated 14064-2 scenario; it would "
                            f"turn a data gap into a fabricated reduction")
        cov = s.get("coverage") or {}
        if cov.get("stale"):
            scenario_notes[label] = ("run no longer matches the org's current activities — "
                                     "expected for a frozen 14064-2 scenario; noted, not a blocker")


    # Baseline justification — the crux of 14064-2. Without a documented counterfactual the
    # reduction is unsubstantiated no matter how exact the arithmetic.
    if not (baseline_justification or "").strip():
        blockers.append(
            "no baseline_justification — 14064-2 requires the baseline scenario to be "
            "documented as what would have occurred in the ABSENCE of the project; the "
            "reduction is unsubstantiated without it")

    # Leakage — must be assessed, never silently assumed zero (that would overstate every
    # reduction). A finite, non-negative figure is required; 0 is a legitimate ASSERTED
    # result of an assessment, a negative 'leakage' is not (it would inflate the reduction).
    leakage = None
    if leakage_tco2e is None:
        blockers.append(
            "leakage_tco2e not quantified — 14064-2 requires a leakage assessment (emission "
            "increases outside the project boundary caused by the project); pass 0 only if a "
            "documented assessment found none")
    else:
        try:
            leakage = float(leakage_tco2e)
        except (TypeError, ValueError):
            leakage = float("nan")
        if not (leakage == leakage) or leakage in (float("inf"), float("-inf")):  # NaN/inf
            blockers.append("leakage_tco2e must be a finite number")
            leakage = None
        elif leakage < 0:
            blockers.append(
                "leakage_tco2e is negative — leakage can only ADD emissions outside the "
                "boundary (it reduces the net benefit); a negative value would inflate the "
                "reduction")
            leakage = None   # invalid -> null the net figures (as for NaN/inf); NEVER
            #                  subtract a negative and surface an INFLATED net reduction

    # Comparability gates. Subtracting two whole-organisation runs only measures a
    # PROJECT effect if the runs are otherwise alike: a 12-month baseline minus a 3-month
    # project run reports the missing nine months as abatement, and a change of GWP set or
    # organisational boundary shows up the same way. Each is a blocker, not a note —
    # a non-comparable delta is not a smaller reduction, it is not a reduction at all.
    blockers.extend(cross_run_gates(
        db, base, proj, label_a="baseline", label_b="project",
        quantity="the project reduction", measures="a project effect"))

    # The exact delta. Positive = the project emitted LESS than the baseline = a reduction.
    gross_loc = (base.total_co2e - proj.total_co2e) / 1000.0
    gross_mkt = (base.total_co2e_market - proj.total_co2e_market) / 1000.0
    net_loc = (gross_loc - leakage) if leakage is not None else None
    net_mkt = (gross_mkt - leakage) if leakage is not None else None

    # A net INCREASE is a legitimate quantification result — the project didn't reduce. It
    # is NOT a disclosure defect (so not a blocker), but it must never be dressed up as a
    # reduction. Reduction status is PER BASIS: the location- and market-based nets can
    # disagree in sign (e.g. a baseline carrying contractual instruments the project drops
    # is a location reduction but a market-based increase). A single location-only flag next
    # to both net figures would label that market increase as abatement, so each basis
    # carries its own flag and the note says they can diverge.
    is_reduction_location_based = (net_loc is not None and net_loc > 0)
    is_reduction_market_based = (net_mkt is not None and net_mkt > 0)

    return {
        "framework": "ISO 14064-2 (GHG project quantification)",
        "period_comparability": period_payload(
            db, base, proj, key_a="baseline", key_b="project",
            note="Baseline and project periods must be within this tolerance of each "
                 "other; a longer baseline would otherwise report the extra elapsed "
                 "time as abatement."),
        "disclosure_ready": not blockers,
        "blockers": blockers,
        "assertion_status": "unverified_quantification",
        "assertion_note": (
            "The quantitative core a validator/verifier would check under ISO 14064-3, NOT "
            "a validated project plan or verified assertion. Reduction = baseline scenario "
            "- project scenario - leakage, from two immutable calculation runs."),
        "project": {
            "name": (project_name or "").strip() or None,
            "baseline_run_id": base.id,
            "project_run_id": proj.id,
            "gwp_set": proj.gwp_set,
            "baseline_justification": (baseline_justification or "").strip() or None,
            "scenario_notes": scenario_notes or None,
        },
        "quantification_tco2e": {
            "baseline_scenario_location_based": round(base.total_co2e / 1000.0, 6),
            "project_scenario_location_based": round(proj.total_co2e / 1000.0, 6),
            "baseline_scenario_market_based": round(base.total_co2e_market / 1000.0, 6),
            "project_scenario_market_based": round(proj.total_co2e_market / 1000.0, 6),
            "gross_reduction_location_based": round(gross_loc, 6),
            "gross_reduction_market_based": round(gross_mkt, 6),
            "leakage": (round(leakage, 6) if leakage is not None else None),
            "net_reduction_location_based": (round(net_loc, 6) if net_loc is not None else None),
            "net_reduction_market_based": (round(net_mkt, 6) if net_mkt is not None else None),
            "is_reduction_location_based": is_reduction_location_based,
            "is_reduction_market_based": is_reduction_market_based,
            "note": ("baseline - project - leakage; POSITIVE = a reduction, NEGATIVE = the "
                     "project scenario emitted MORE than the baseline (an increase, not "
                     "abatement). Leakage is subtracted from the gross benefit. Reduction "
                     "status is PER BASIS: the location- and market-based nets can diverge "
                     "in sign (a location reduction can coincide with a market-based "
                     "increase, e.g. when a baseline held contractual instruments the "
                     "project does not) — read each is_reduction_* flag against its own net."),
        },
        "conservativeness_note": (
            "ISO 14064-2 requires that, where assumptions or data are uncertain, values be "
            "chosen so as NOT to overstate the reduction (conservative baseline high, "
            "project low, leakage complete). The engine subtracts the supplied leakage and "
            "surfaces the inputs; it does not and cannot audit the conservativeness of the "
            "underlying scenario data."),
        "double_counting_warning": (
            "This is a PROJECT-level reduction (ISO 14064-2), a SEPARATE account from the "
            "organisation's corporate GHG inventory (ISO 14064-1 / GHG Protocol). Do NOT add "
            "it to, or net it against, the corporate inventory total — the project's effect "
            "is already reflected in the organisation's own emissions over time, and "
            "counting both double-counts. A reduction claimed here and also sold or "
            "transferred as a credit would be double-counted again."),
        "requirements_not_produced": _NOT_PRODUCED,
        "methodology": (
            f"ISO 14064-2 project quantification: exact difference between immutable "
            f"baseline run #{base.id} and project run #{proj.id}, less supplied leakage; "
            f"{proj.gwp_set} GWP-100. Fossil CO2e total on each side; biogenic removals "
            f"tracked separately and not folded in. Reductions across a GWP vintage or a "
            f"residual-mix methodology change are blocked as non-comparable."),
    }
