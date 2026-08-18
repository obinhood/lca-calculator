"""SBTi target report: pathway, minimum-ambition check, trajectory vs actuals."""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import EmissionsTarget, CalculationRun
from ..services.sbti import (
    financed_comparable, financed_included,
    run_scoped_emissions_kg, linear_pathway, assess_ambition,
)
from ..services.boundary import base_year_recalculation
from ..services.comparability import (
    period_comparable, period_payload, period_info, year_within_period,
)


def sbti_report(db: Session, organisation_id: int, target_id: int,
                current_run_id: Optional[int] = None,
                current_year: Optional[int] = None) -> dict:
    """Pathway, minimum-ambition check, and trajectory vs actuals for one target.

    Both years in play are ASSERTIONS about when a run's emissions occurred —
    `target.base_year` about the base run, `current_year` about the current run — and both
    move the pathway allowance without moving the emissions. Each is tied to its run's
    reporting period here, and the two runs must cover comparable period lengths: a
    12-month base against a 3-month current run reads three quarters of the year as
    progress.
    """
    target = db.query(EmissionsTarget).filter(
        EmissionsTarget.id == target_id,
        EmissionsTarget.organisation_id == organisation_id).first()
    if target is None:
        return {"framework": "SBTi target", "ok": False,
                "blockers": ["target not found for this organisation"]}
    base_run = db.query(CalculationRun).filter(
        CalculationRun.id == target.base_run_id,
        CalculationRun.organisation_id == organisation_id).first()
    if base_run is None:
        return {"framework": "SBTi target", "ok": False,
                "blockers": ["base run not found for this organisation"]}

    blockers = []
    # The base year anchors the whole pathway: `base_t` is the base RUN's total, and every
    # allowance is interpolated from (base_year, base_t) to (target_year, target_t). A
    # base_year that does not match the base run silently shifts the entire line — a 2020
    # base_year on a run covering 2024 grants four extra years of allowance against
    # emissions that were never measured in 2020 — and nothing else in the report can see
    # it, because both numbers are individually plausible.
    if (_by := year_within_period(db, base_run, target.base_year,
                                  year_name="target base_year", run_label="base")):
        blockers.append(_by)

    base_kg = run_scoped_emissions_kg(db, base_run.id, target.scope_coverage)
    base_t = base_kg / 1000.0
    target_t = base_t * (1.0 - target.target_reduction_pct)
    ambition = assess_ambition(target.target_reduction_pct, target.base_year,
                               target.target_year, target.ambition,
                               target_type=target.target_type)

    trajectory = None
    current = None
    period_comparability = None
    if current_run_id is not None:
        current = db.query(CalculationRun).filter(
            CalculationRun.id == current_run_id,
            CalculationRun.organisation_id == organisation_id).first()
        if current is None:
            blockers.append("current run not found for this organisation")
        elif current_year is None:
            blockers.append("current_year required to place the run on the pathway")
        elif current_year < target.base_year:
            # Silently clamping would manufacture "on track" from an out-of-range
            # (or typo'd) year — block instead.
            blockers.append(f"current_year {current_year} is before the base year "
                            f"{target.base_year} — cannot place on the pathway")
        elif current.gwp_set != base_run.gwp_set:
            blockers.append(f"current run GWP set {current.gwp_set} != base {base_run.gwp_set}"
                            f" — trajectory across GWP vintages is not comparable")
        elif (fin := financed_comparable(db, base_run.id, current.id,
                                         target.scope_coverage)) is not None:
            blockers.append(fin)
        elif (recalc := base_year_recalculation(db, base_run, current)) is not None:
            # GHG Protocol Ch.5: a change of organisational boundary between the base
            # year and now makes the trajectory meaningless — the base year must be
            # recalculated, not compared across (organic growth would be fine).
            blockers.append(recalc)
        elif (_pc := period_comparable(db, base_run, current, label_a="base year",
                                       label_b="current", quantity="the trajectory",
                                       measures="progress against the target")) is not None:
            # The dimension the arithmetic cannot see: `actual_t` and `base_t` are each
            # correct for the span they cover, so a shorter current run reads as progress.
            blockers.append(_pc)
        elif (_cy := year_within_period(db, current, current_year,
                                        year_name="current_year",
                                        run_label="current")) is not None:
            # current_year decides WHERE on the pathway the run is judged. Free-floating,
            # it could place a 2024 run at 2030 and manufacture "on track" out of six
            # years of allowance the organisation has not yet lived through.
            blockers.append(_cy)
        else:
            actual_t = run_scoped_emissions_kg(db, current.id, target.scope_coverage) / 1000.0
            allowed_t = linear_pathway(base_t, target.base_year, target.target_year,
                                       target.target_reduction_pct, current_year)
            trajectory = {
                "current_run_id": current.id,
                "current_year": current_year,
                "current_reporting_period": period_info(db, current),
                "actual_tco2e": round(actual_t, 6),
                "pathway_allowed_tco2e": round(allowed_t, 6),
                "variance_tco2e": round(actual_t - allowed_t, 6),
                "on_track": actual_t <= allowed_t + 1e-9,
                "reduction_vs_base_pct": round(100.0 * (1 - actual_t / base_t), 4)
                                         if base_t else None,
            }

    if current is not None:
        period_comparability = period_payload(
            db, base_run, current, key_a="base_year", key_b="current",
            note="Base-year and current runs must cover comparable period lengths, and "
                 "each year label must fall inside its own run's reporting period. "
                 "Otherwise the trajectory measures elapsed time, or places real "
                 "emissions at a year they do not cover.")
        period_comparability["target_base_year"] = target.base_year
        period_comparability["current_year"] = current_year

    return {
        "framework": "SBTi target",
        "ok": not blockers,
        "blockers": blockers,
        # Both runs' periods, so a base_year or current_year that does not match its run is
        # visible in the payload rather than only detectable by the gates above.
        "period_comparability": period_comparability,
        "target": {
            "id": target.id, "name": target.name, "type": target.target_type,
            "scope_coverage": target.scope_coverage, "ambition": target.ambition,
            "base_year": target.base_year, "target_year": target.target_year,
            "target_reduction_pct": target.target_reduction_pct,
            "sbti_validated": target.sbti_validated,
        },
        "base": {"run_id": base_run.id, "gwp_set": base_run.gwp_set,
                 "consolidation_approach": base_run.consolidation_approach,
                 "base_emissions_tco2e": round(base_t, 6),
                 # The period the base-year figure actually covers. None = the base run is
                 # unscoped, in which case `base_year` is unverifiable (and blocked above).
                 "reporting_period": period_info(db, base_run),
                 # Whether financed emissions are INSIDE the figures above. For a bank
                 # that is most of the inventory, so a reader must not have to infer it.
                 # None = not included (dimension not evaluated, coverage excludes
                 # Scope 3, or Cat 15 double-declared) — never "zero".
                 "financed_emissions_included_tco2e": (
                     None if (_fin_base := financed_included(
                         db, base_run.id, target.scope_coverage)) is None
                     else round(_fin_base / 1000.0, 6)),
                 "financed_emissions_basis": (
                     "PCAF financed emissions (Scope 3 Category 15) are INCLUDED in the "
                     "base year and in every actual, because this target covers Scope 3"
                     if financed_included(db, base_run.id, target.scope_coverage) is not None
                     else "financed emissions are NOT included — the target's coverage "
                          "excludes Scope 3, the PCAF dimension was not evaluated on this "
                          "run, or Category 15 is double-declared")},
        "target_emissions_tco2e": round(target_t, 6),
        "ambition_assessment": ambition,
        "trajectory": trajectory,
    }
