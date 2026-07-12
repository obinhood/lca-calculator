"""GRI 305 (Emissions) + GRI 302 (Energy) content-index renderer.

Maps one immutable CalculationRun onto the GRI disclosure numbers:
  305-1 gross Scope 1 (with biogenic CO2 separately, as 305-1 requires)
  305-2 gross Scope 2 (location- AND market-based)
  305-3 gross Scope 3 (with category detail)
  305-4 GHG intensity (caller-supplied denominator)
  305-5 emissions reductions — computed as the EXACT delta between this run
        and a caller-chosen BASE run (both immutable, both traceable), not a
        self-reported number.
  302-1 energy consumption within the organisation (Scope 1/2-bounded MWh)
  302-3 energy intensity.

Same fail-closed gates; GRI-specific: 305-5 requires a base_run_id.
"""
import math
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CalculationRun
from .summary import summary, run_factor_sources, scope3_by_category
from .secr import _energy_kwh


def gri_report(db: Session, organisation_id: int, run_id: Optional[int] = None,
               base_run_id: Optional[int] = None,
               intensity_denominator: Optional[float] = None,
               intensity_denominator_unit: Optional[str] = None) -> dict:
    s = summary(db, organisation_id=organisation_id, run_id=run_id)
    run_info = s.get("run")
    if run_info is None:
        return {"framework": "GRI 305/302", "disclosure_ready": False,
                "blockers": ["no calculation run exists — upload activities and run a calculation"]}
    run = db.get(CalculationRun, run_info["id"])

    blockers = []
    cov = s["coverage"]
    if s.get("partial"):
        blockers.append(f"run is PARTIAL — excluded activities: {s['partial_reasons']}")
    if cov["stale"]:
        blockers.append("run is STALE relative to current activity data — recompute first")
    if cov["coverage_pct"] < 100.0:
        blockers.append(f"coverage is {cov['coverage_pct']}% (count-based) — "
                        f"resolve unmapped/errored activities or document exclusions")

    denom_ok = (intensity_denominator is not None
                and math.isfinite(intensity_denominator) and intensity_denominator > 0)
    if not denom_ok:
        blockers.append("intensity_denominator required (finite, > 0) for 305-4/302-3")

    by_scope = {row["scope"]: row["co2e"] for row in s["by_scope"]}
    scope1_kg = by_scope.get("1", 0.0)
    scope3_kg = by_scope.get("3", 0.0)

    # 305-5: exact reduction vs an immutable base run of the SAME organisation.
    reductions = None
    if base_run_id is not None:
        base = db.query(CalculationRun).filter(
            CalculationRun.id == base_run_id,
            CalculationRun.organisation_id == organisation_id).first()
        if base is None:
            blockers.append("base_run_id not found for this organisation")
        else:
            reductions = {
                "base_run_id": base.id,
                "base_run_created_at": base.created_at,
                "base_gwp_set": base.gwp_set,
                "reduction_location_based_tco2e":
                    round((base.total_co2e - run.total_co2e) / 1000.0, 6),
                "reduction_market_based_tco2e":
                    round((base.total_co2e_market - run.total_co2e_market) / 1000.0, 6),
                "note": "Exact difference between two immutable calculation runs; "
                        "positive = reduction. GWP sets must match for a valid claim.",
            }
            if base.gwp_set != run.gwp_set:
                blockers.append(f"305-5 base run used {base.gwp_set} but this run used "
                                f"{run.gwp_set} — reductions across GWP vintages are not "
                                f"comparable")
    energy = _energy_kwh(db, run, scopes=("1", "2"))
    energy_mwh = {c: round(energy[c] / 1000.0, 6) for c in ("electricity", "gas", "diesel")}
    total_mwh = round(energy["total_kwh"] / 1000.0, 6)

    ef_sources = run_factor_sources(db, run)
    dq = s.get("data_quality") or {}

    return {
        "framework": "GRI 305 Emissions / GRI 302 Energy",
        "disclosure_ready": not blockers,
        "blockers": blockers,
        "run": run_info,
        "gri_305_1_scope1": {
            "gross_tco2e": round(scope1_kg / 1000.0, 6),
            "biogenic_co2_tco2_separate": round((run.total_biogenic_co2e or 0.0) / 1000.0, 6),
            "gases_included": "per-gas factors: CO2, CH4 (fossil/biogenic), N2O; "
                              "aggregate factors as published",
        },
        "gri_305_2_scope2": {
            "location_based_tco2e": round(s["scope2"]["location_based"] / 1000.0, 6),
            "market_based_tco2e": round(s["scope2"]["market_based"] / 1000.0, 6),
        },
        "gri_305_3_scope3": {
            "gross_tco2e": round(scope3_kg / 1000.0, 6),
            "by_category_tco2e": {c: round(v / 1000.0, 6)
                                  for c, v in scope3_by_category(db, run).items()},
        },
        "gri_305_4_intensity": ({
            "tco2e_per_unit": round(run.total_co2e / 1000.0 / intensity_denominator, 6),
            "denominator": intensity_denominator,
            "denominator_unit": intensity_denominator_unit or "unit",
            "scopes_included": "1+2(location)+3",
        } if denom_ok else None),
        "gri_305_5_reductions": reductions,
        "gri_302_1_energy": {"by_carrier_mwh": energy_mwh, "total_mwh": total_mwh,
                             "boundary": "own operations (Scope 1/2 line items)"},
        "gri_302_3_energy_intensity": ({
            "mwh_per_unit": round(total_mwh / intensity_denominator, 6),
            "denominator": intensity_denominator,
            "denominator_unit": intensity_denominator_unit or "unit",
        } if denom_ok else None),
        "methodology": f"GHG Protocol Corporate Standard; {run.gwp_set} GWP-100; "
                       f"factors: {', '.join(ef_sources) or 'none'}; immutable run "
                       f"#{run.id}; coverage {cov['coverage_pct']}%; DQ "
                       f"{dq.get('emissions_weighted_score') if dq.get('has_data') else 'n/a'}.",
        "coverage": cov,
        "exclusions": s["exclusions"],
    }
