"""California SB 253 (Climate Corporate Data Accountability Act) renderer.

Renders one immutable CalculationRun into the CARB disclosure datapoints for a
covered entity (> $1B revenue doing business in California):
  * Scope 1 and Scope 2 GHG emissions (tCO2e), Scope 2 dual-reported per the
    GHG Protocol (CARB filings follow GHG Protocol standards).
  * Scope 3 (phase-in: first required the year after Scope 1/2 reporting
    begins; included here with its phase-in status labelled).
  * Assurance metadata — SB 253 requires LIMITED assurance for Scope 1/2 from
    the first filings, escalating to REASONABLE assurance in 2030.

Fail-closed disclosure, same doctrine as the SECR renderer: the payload always
states whether it is filing-ready and exactly why not.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CalculationRun
from .summary import summary, run_factor_sources
from ..services.residual_mix import scope2_residual_mix_completeness

_ASSURANCE_LEVELS = ("none", "limited", "reasonable")


def sb253_report(db: Session, organisation_id: int, run_id: Optional[int] = None,
                 assurance_level: str = "none",
                 assurance_provider: Optional[str] = None) -> dict:
    """SB 253 disclosure payload for one run (latest for the org by default)."""
    s = summary(db, organisation_id=organisation_id, run_id=run_id)
    run_info = s.get("run")
    if run_info is None:
        return {"framework": "California SB 253 (CCDAA)", "filing_ready": False,
                "blockers": ["no calculation run exists — upload activities and run a calculation"]}
    run = db.get(CalculationRun, run_info["id"])

    by_scope = {row["scope"]: row["co2e"] for row in s["by_scope"]}
    scope1_kg = by_scope.get("1", 0.0)
    scope2_loc_kg = s["scope2"]["location_based"]
    scope2_mkt_kg = s["scope2"]["market_based"]
    scope3_kg = by_scope.get("3", 0.0)

    blockers = []
    cov = s["coverage"]
    blockers.extend(scope2_residual_mix_completeness(db, run).get("blockers", []))
    if s.get("partial"):
        blockers.append(f"run is PARTIAL — excluded activities: {s['partial_reasons']}")
    if cov["stale"]:
        blockers.append("run is STALE relative to current activity data — recompute first")
    if cov["coverage_pct"] < 100.0:
        blockers.append(f"coverage is {cov['coverage_pct']}% (count-based) — "
                        f"resolve unmapped/errored activities or document exclusions")
    if assurance_level not in _ASSURANCE_LEVELS:
        blockers.append(f"assurance_level must be one of {_ASSURANCE_LEVELS}")
    elif assurance_level == "none":
        blockers.append("SB 253 requires at least LIMITED third-party assurance for "
                        "Scope 1 and 2 from the first filing cycle — engage an "
                        "assurance provider and set assurance_level")

    # Frozen lineage — never via the live activity->factor mapping.
    ef_sources = run_factor_sources(db, run)

    dq = s.get("data_quality") or {}
    methodology = (
        f"Prepared in conformance with the GHG Protocol Corporate Standard and Scope 2 "
        f"Guidance, as required by California Health & Safety Code 38532 (SB 253). "
        f"Emission factors: {', '.join(ef_sources) or 'none'}. "
        f"GWP set {run.gwp_set} (IPCC 100-year). Scope 2 dual-reported (location- and "
        f"market-based, volume-matched instruments). Immutable calculation run "
        f"#{run.id} of {run.created_at}; every figure traceable to source records and "
        f"pinned factor versions. Coverage {cov['coverage_pct']}% "
        f"({cov['coverage_basis']}); emissions-weighted data-quality score "
        f"{dq.get('emissions_weighted_score') if dq.get('has_data') else 'n/a'} "
        f"(1 best..5 worst); primary-data share "
        f"{s['method_split']['primary_data_share_pct']}%."
    )

    return {
        "framework": "California SB 253 (CCDAA)",
        "filing_ready": not blockers,
        "blockers": blockers,
        "run": run_info,
        "reporting_period_id": run.reporting_period_id,
        "emissions_tco2e": {
            "scope1": round(scope1_kg / 1000.0, 6),
            "scope2_location_based": round(scope2_loc_kg / 1000.0, 6),
            "scope2_market_based": round(scope2_mkt_kg / 1000.0, 6),
            "scope3": round(scope3_kg / 1000.0, 6),
            "scope3_phase_in_note": "Scope 3 reporting begins the year after the "
                                    "first Scope 1/2 filing; no assurance required "
                                    "for Scope 3 before the 2030 review.",
            "biogenic_co2_separate": round((run.total_biogenic_co2e or 0.0) / 1000.0, 6),
            "total_location_based": round(run.total_co2e / 1000.0, 6),
            "total_market_based": round(run.total_co2e_market / 1000.0, 6),
        },
        "assurance": {
            "level": assurance_level,
            "provider": assurance_provider,
            "requirement": "limited for Scope 1/2 from first filings; "
                           "reasonable from 2030",
        },
        "scope2_market_disclosure": s["scope2"],
        "method_split": s["method_split"],
        "data_quality": dq,
        "methodology_statement": methodology,
        "coverage": cov,
        "exclusions": s["exclusions"],
    }
