"""SBTi target report: pathway, minimum-ambition check, trajectory vs actuals."""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import EmissionsTarget, CalculationRun
from ..services.sbti import (
    run_scoped_emissions_kg, linear_pathway, assess_ambition,
)


def sbti_report(db: Session, organisation_id: int, target_id: int,
                current_run_id: Optional[int] = None,
                current_year: Optional[int] = None) -> dict:
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
    base_kg = run_scoped_emissions_kg(db, base_run.id, target.scope_coverage)
    base_t = base_kg / 1000.0
    target_t = base_t * (1.0 - target.target_reduction_pct)
    ambition = assess_ambition(target.target_reduction_pct, target.base_year,
                               target.target_year, target.ambition)

    trajectory = None
    if current_run_id is not None:
        current = db.query(CalculationRun).filter(
            CalculationRun.id == current_run_id,
            CalculationRun.organisation_id == organisation_id).first()
        if current is None:
            blockers.append("current run not found for this organisation")
        elif current_year is None:
            blockers.append("current_year required to place the run on the pathway")
        elif current.gwp_set != base_run.gwp_set:
            blockers.append(f"current run GWP set {current.gwp_set} != base {base_run.gwp_set}"
                            f" — trajectory across GWP vintages is not comparable")
        else:
            actual_t = run_scoped_emissions_kg(db, current.id, target.scope_coverage) / 1000.0
            allowed_t = linear_pathway(base_t, target.base_year, target.target_year,
                                       target.target_reduction_pct, current_year)
            trajectory = {
                "current_run_id": current.id,
                "current_year": current_year,
                "actual_tco2e": round(actual_t, 6),
                "pathway_allowed_tco2e": round(allowed_t, 6),
                "variance_tco2e": round(actual_t - allowed_t, 6),
                "on_track": actual_t <= allowed_t + 1e-9,
                "reduction_vs_base_pct": round(100.0 * (1 - actual_t / base_t), 4)
                                         if base_t else None,
            }

    return {
        "framework": "SBTi target",
        "ok": not blockers,
        "blockers": blockers,
        "target": {
            "id": target.id, "name": target.name, "type": target.target_type,
            "scope_coverage": target.scope_coverage, "ambition": target.ambition,
            "base_year": target.base_year, "target_year": target.target_year,
            "target_reduction_pct": target.target_reduction_pct,
            "sbti_validated": target.sbti_validated,
        },
        "base": {"run_id": base_run.id, "gwp_set": base_run.gwp_set,
                 "base_emissions_tco2e": round(base_t, 6)},
        "target_emissions_tco2e": round(target_t, 6),
        "ambition_assessment": ambition,
        "trajectory": trajectory,
    }
