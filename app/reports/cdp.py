"""CDP Climate questionnaire export.

Maps one immutable CalculationRun onto the CDP climate datapoints using the
long-standing C-question codes (C6 emissions data, C6.10 intensity, C10
verification). CDP renumbered modules in its 2024 integrated questionnaire —
the classic codes remain the lingua franca and each field carries its label,
but VERIFY the mapping against the current questionnaire release before
submission (surfaced in the payload, not hidden).
"""
import math
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CalculationRun
from .summary import summary, run_factor_sources
from .scope3 import category_tco2e
from ..services.ghgp import scope3_completeness


def cdp_export(db: Session, organisation_id: int, run_id: Optional[int] = None,
               intensity_denominator: Optional[float] = None,
               intensity_denominator_unit: Optional[str] = None,
               verification_status: str = "no_third_party_verification") -> dict:
    s = summary(db, organisation_id=organisation_id, run_id=run_id)
    run_info = s.get("run")
    if run_info is None:
        return {"framework": "CDP Climate", "submission_ready": False,
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
        blockers.append("intensity_denominator required (finite, > 0) for C6.10")
    # CDP C6.5 IS the 15-category Scope 3 grid — screen all 15.
    blockers.extend(scope3_completeness(db, run).get("blockers", []))
    _financed_tco2e = (run.financed_co2e or 0.0) / 1000.0

    by_scope = {row["scope"]: row["co2e"] for row in s["by_scope"]}
    ef_sources = run_factor_sources(db, run)
    dq = s.get("data_quality") or {}

    return {
        "framework": "CDP Climate",
        "questionnaire_note": "Classic C-question codes; CDP renumbered modules in "
                              "the 2024 integrated questionnaire — verify mapping "
                              "against the current release before submission.",
        "submission_ready": not blockers,
        "blockers": blockers,
        "run": run_info,
        "answers": {
            "C5.2_base_year_emissions": None,   # set when a base year is designated
            "C6.1_scope1_gross_tco2e": round(by_scope.get("1", 0.0) / 1000.0, 6),
            "C6.3_scope2_location_tco2e": round(s["scope2"]["location_based"] / 1000.0, 6),
            "C6.3_scope2_market_tco2e": round(s["scope2"]["market_based"] / 1000.0, 6),
            "C6.5_scope3_tco2e": round(by_scope.get("3", 0.0) / 1000.0 + _financed_tco2e, 6),
            "C6.5_scope3_excl_financed_tco2e": round(by_scope.get("3", 0.0) / 1000.0, 6),
            "C6.5_scope3_by_ghgp_category_tco2e": category_tco2e(s.get("scope3_ghgp") or {}),
            "C6.5_cat15_financed_tco2e": round(_financed_tco2e, 6) if run.financed_co2e is not None else None,
            "C6.7_biogenic_co2_tco2": round((run.total_biogenic_co2e or 0.0) / 1000.0, 6),
            "C6.10_intensity": ({
                "tco2e_per_unit": round((run.total_co2e / 1000.0 + _financed_tco2e) / intensity_denominator, 6),
                "denominator": intensity_denominator,
                "denominator_unit": intensity_denominator_unit or "unit",
            } if denom_ok else None),
            "C10.1_verification_status": verification_status,
        },
        "methodology": f"GHG Protocol Corporate Standard; {run.gwp_set} GWP-100 per gas "
                       f"at calculation time; factors: {', '.join(ef_sources) or 'none'}; "
                       f"immutable run #{run.id}; coverage {cov['coverage_pct']}%; "
                       f"emissions-weighted DQ "
                       f"{dq.get('emissions_weighted_score') if dq.get('has_data') else 'n/a'}; "
                       f"primary-data share {s['method_split']['primary_data_share_pct']}%.",
        "coverage": cov,
        "exclusions": s["exclusions"],
    }
