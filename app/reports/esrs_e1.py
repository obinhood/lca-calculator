"""CSRD ESRS E1 (Climate change) disclosure renderer — quantitative datapoints.

Renders one immutable CalculationRun into the E1 datapoints this platform can
substantiate:
  * E1-6 — gross Scope 1, 2 (dual: location- AND market-based), 3 and total
    GHG emissions (tCO2e), biogenic CO2 reported separately, and GHG intensity
    per net revenue.
  * E1-5 — energy consumption (MWh) by carrier, with the contractually-covered
    (REC/PPA) electricity split derived from the run's volume-matched
    market-instrument allocations.
  * E1-7 — removals and credits: stated explicitly as none recorded (the
    platform has no removals ledger yet), never silently omitted.

Disclosure requirements the platform does NOT produce (narrative/target
modules: E1-1 transition plan, E1-2/3 policies and actions, E1-4 targets,
E1-8 internal carbon pricing, E1-9 financial effects) are listed under
``not_covered`` — an honest scope statement, not an implied completeness.

Same fail-closed doctrine as the SECR/SB253 renderers: pre-submission gates
explain exactly why a payload is not disclosure-ready.
"""
import json
from typing import Optional

from sqlalchemy.orm import Session

from ..models import ActivityRecord, CalculationRun, EmissionLineItem, EmissionFactor
from .summary import summary
from .secr import _energy_kwh

NOT_COVERED = [
    "E1-1 transition plan for climate change mitigation",
    "E1-2 policies related to climate change",
    "E1-3 actions and resources",
    "E1-4 targets related to climate change",
    "E1-8 internal carbon pricing",
    "E1-9 anticipated financial effects",
]


def _renewable_contractual_mwh(db: Session, run: CalculationRun) -> float:
    """MWh covered by renewable contractual instruments (REC/PPA) in this run,
    read from the FROZEN market-line allocations."""
    rows = db.query(EmissionLineItem.details).filter(
        EmissionLineItem.run_id == run.id, EmissionLineItem.method == "market").all()
    kwh = 0.0
    for (details,) in rows:
        d = json.loads(details or "{}")
        for alloc in d.get("allocations", []):
            if alloc.get("instrument_type") in ("rec", "ppa"):
                kwh += alloc.get("kwh_covered", 0.0) or 0.0
    return kwh / 1000.0


def esrs_e1_report(db: Session, organisation_id: int, run_id: Optional[int] = None,
                   net_revenue_millions: Optional[float] = None,
                   revenue_currency: str = "EUR") -> dict:
    """ESRS E1 quantitative disclosure payload for one run."""
    s = summary(db, organisation_id=organisation_id, run_id=run_id)
    run_info = s.get("run")
    if run_info is None:
        return {"framework": "CSRD ESRS E1", "disclosure_ready": False,
                "blockers": ["no calculation run exists — upload activities and run a calculation"]}
    run = db.get(CalculationRun, run_info["id"])

    by_scope = {row["scope"]: row["co2e"] for row in s["by_scope"]}
    scope1_kg = by_scope.get("1", 0.0)
    scope2_loc_kg = s["scope2"]["location_based"]
    scope2_mkt_kg = s["scope2"]["market_based"]
    scope3_kg = by_scope.get("3", 0.0)

    blockers = []
    cov = s["coverage"]
    if s.get("partial"):
        blockers.append(f"run is PARTIAL — excluded activities: {s['partial_reasons']}")
    if cov["stale"]:
        blockers.append("run is STALE relative to current activity data — recompute first")
    if cov["coverage_pct"] < 100.0:
        blockers.append(f"coverage is {cov['coverage_pct']}% (count-based) — "
                        f"resolve unmapped/errored activities or document exclusions")
    if not net_revenue_millions or net_revenue_millions <= 0:
        blockers.append("net_revenue_millions required: E1-6 mandates GHG intensity "
                        "per net revenue")

    # E1-5: ESRS reports energy in MWh.
    energy_kwh = _energy_kwh(db, run)
    renewable_mwh = _renewable_contractual_mwh(db, run)
    total_mwh = energy_kwh["total_kwh"] / 1000.0
    energy = {
        "total_mwh": round(total_mwh, 6),
        "by_carrier_mwh": {c: round(energy_kwh[c] / 1000.0, 6)
                           for c in ("electricity", "gas", "diesel")},
        "electricity_renewable_contractual_mwh": round(renewable_mwh, 6),
        "note": ("Renewable split covers contractual instruments (REC/PPA) from the "
                 "run's volume-matched allocations; supplier fuel-mix data beyond "
                 "instruments is not yet captured. " + " ".join(energy_kwh["notes"])),
    }

    # E1-6 scope 3 by (platform) category — GHG Protocol 15-category mapping is
    # a labelled limitation until activities carry a ghgp_category.
    scope3_by_cat = {
        row["category"]: round(row["co2e"] / 1000.0, 6)
        for row in s["by_category"]
        if row["category"] not in ("electricity", "gas", "diesel")
    }

    intensity = None
    if net_revenue_millions and net_revenue_millions > 0:
        intensity = {
            "tco2e_total_location_per_million_revenue":
                round(run.total_co2e / 1000.0 / net_revenue_millions, 6),
            "tco2e_total_market_per_million_revenue":
                round(run.total_co2e_market / 1000.0 / net_revenue_millions, 6),
            "net_revenue_millions": net_revenue_millions,
            "revenue_currency": revenue_currency,
        }

    ef_sources = db.query(EmissionFactor.source, EmissionFactor.version)\
        .join(ActivityRecord, ActivityRecord.factor_id == EmissionFactor.id)\
        .join(EmissionLineItem, EmissionLineItem.activity_id == ActivityRecord.id)\
        .filter(EmissionLineItem.run_id == run.id).distinct().all()
    dq = s.get("data_quality") or {}
    methodology = (
        f"GHG figures prepared per the GHG Protocol Corporate Standard as referenced "
        f"by ESRS E1 (AR {run.gwp_set} GWP-100, applied per gas at calculation time "
        f"where per-gas factors exist). Emission factors: "
        f"{', '.join(sorted(f'{src} v{ver}' for src, ver in ef_sources)) or 'none'}. "
        f"Scope 2 dual-reported (location- and market-based, volume-matched "
        f"instruments). Biogenic CO2 reported separately, never netted. Immutable "
        f"calculation run #{run.id} of {run.created_at}. Coverage "
        f"{cov['coverage_pct']}% ({cov['coverage_basis']}); emissions-weighted "
        f"data-quality score "
        f"{dq.get('emissions_weighted_score') if dq.get('has_data') else 'n/a'} "
        f"(1 best..5 worst); primary-data share "
        f"{s['method_split']['primary_data_share_pct']}%."
    )

    return {
        "framework": "CSRD ESRS E1",
        "disclosure_ready": not blockers,
        "blockers": blockers,
        "run": run_info,
        "reporting_period_id": run.reporting_period_id,
        "e1_6_gross_ghg_emissions_tco2e": {
            "scope1": round(scope1_kg / 1000.0, 6),
            "scope2_location_based": round(scope2_loc_kg / 1000.0, 6),
            "scope2_market_based": round(scope2_mkt_kg / 1000.0, 6),
            "scope3": round(scope3_kg / 1000.0, 6),
            "scope3_by_category": scope3_by_cat,
            "scope3_category_note": "Categories are platform activity categories; "
                                    "GHG Protocol 15-category mapping pending.",
            "total_location_based": round(run.total_co2e / 1000.0, 6),
            "total_market_based": round(run.total_co2e_market / 1000.0, 6),
            "biogenic_co2_separate": round((run.total_biogenic_co2e or 0.0) / 1000.0, 6),
            "ghg_intensity": intensity,
        },
        "e1_5_energy_consumption": energy,
        "e1_7_removals_and_credits": {
            "removals_tco2e": 0.0,
            "credits_tco2e": 0.0,
            "note": "No GHG removals or carbon credits are recorded on this platform "
                    "for this run — stated explicitly, not omitted.",
        },
        "not_covered": NOT_COVERED,
        "method_split": s["method_split"],
        "data_quality": dq,
        "methodology_statement": methodology,
        "coverage": cov,
        "exclusions": s["exclusions"],
    }
