"""ISSB IFRS S2 (Climate-related Disclosures) renderer + jurisdiction profiles.

IFRS S2 is the global baseline that UK SRS, Japan (SSBJ), Singapore (SGX),
Hong Kong (HKEX) and other adopting jurisdictions anchor on — one renderer,
thin per-jurisdiction profiles (regulator, phase-in, reliefs) on top.

Quantitative datapoints this platform substantiates (S2 paras 29-33):
  * Gross Scope 1, Scope 2 (location-based required; market-based/contractual
    instrument information disclosed alongside), Scope 3 with category detail.
  * Measured per the GHG Protocol Corporate Standard, per-gas GWP applied at
    calculation time. S2 expects the LATEST IPCC GWP values (AR6): a run
    computed under AR5 is gated, not silently passed.
  * Biogenic CO2 separately; methodology, coverage and data-quality context.
Disclosures the platform does not produce (governance, strategy, scenario
analysis, industry/SASB metrics, internal carbon price, remuneration links,
capex alignment) are listed under ``not_covered`` — honest scope, not implied
completeness. Same fail-closed gate doctrine as every other renderer.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CalculationRun
from .summary import summary, run_factor_sources
from .scope3 import category_tco2e
from ..services.ghgp import scope3_completeness

JURISDICTION_PROFILES = {
    "ISSB": {
        "name": "IFRS S2 (global baseline)",
        "regulator": "ISSB / local adopter",
        "basis": "IFRS S2 as issued",
        "phase_in": "as adopted per jurisdiction",
    },
    "UK_SRS": {
        "name": "UK Sustainability Reporting Standards (UK SRS S2)",
        "regulator": "UK FCA / DBT endorsement",
        "basis": "IFRS S2 with UK endorsement amendments",
        "phase_in": "listed companies phased from FY2026 (first reports 2027); "
                    "interacts with existing SECR duties",
    },
    "JP_SSBJ": {
        "name": "SSBJ Standards (Japan)",
        "regulator": "JFSA / SSBJ",
        "basis": "IFRS S1/S2-aligned SSBJ standards",
        "phase_in": "TSE Prime market phased mandatory reporting from FY2027 "
                    "(largest issuers first)",
    },
    "SG_SGX": {
        "name": "SGX climate reporting (Singapore)",
        "regulator": "SGX RegCo / ACRA",
        "basis": "IFRS S2-based ISSB-aligned climate disclosures",
        "phase_in": "listed issuers from FY2025; large non-listed companies to follow",
    },
    "HK_HKEX": {
        "name": "HKEX climate disclosures (Hong Kong)",
        "regulator": "HKEX / SFC",
        "basis": "HKEX ESG Code Part D, IFRS S2-aligned",
        "phase_in": "phased from FY2025; full alignment for LargeCap by FY2026",
    },
}

NOT_COVERED = [
    "governance and strategy narratives (S2 paras 5-12)",
    "climate resilience / scenario analysis (S2 para 22)",
    "industry-based (SASB-derived) metrics",
    "internal carbon price disclosure",
    "climate targets (pending the target-setting module)",
    "remuneration linkage; capex/financing alignment",
    "financed emissions (financial-sector Scope 3 category 15)",
]


def issb_s2_report(db: Session, organisation_id: int, run_id: Optional[int] = None,
                   jurisdiction: str = "ISSB") -> dict:
    s = summary(db, organisation_id=organisation_id, run_id=run_id)
    run_info = s.get("run")
    if run_info is None:
        return {"framework": "ISSB IFRS S2", "disclosure_ready": False,
                "blockers": ["no calculation run exists — upload activities and run a calculation"]}
    run = db.get(CalculationRun, run_info["id"])

    profile = JURISDICTION_PROFILES.get(jurisdiction)
    blockers = []
    if profile is None:
        blockers.append(f"unknown jurisdiction profile {jurisdiction!r}; "
                        f"one of {sorted(JURISDICTION_PROFILES)}")
        profile = JURISDICTION_PROFILES["ISSB"]

    by_scope = {row["scope"]: row["co2e"] for row in s["by_scope"]}
    cov = s["coverage"]
    if s.get("partial"):
        blockers.append(f"run is PARTIAL — excluded activities: {s['partial_reasons']}")
    if cov["stale"]:
        blockers.append("run is STALE relative to current activity data — recompute first")
    if cov["coverage_pct"] < 100.0:
        blockers.append(f"coverage is {cov['coverage_pct']}% (count-based) — "
                        f"resolve unmapped/errored activities or document exclusions")
    # S2 para B23: latest IPCC GWP values unless the jurisdiction requires otherwise.
    if run.gwp_set != "AR6":
        blockers.append(f"IFRS S2 expects the latest IPCC GWP values (AR6); this run "
                        f"used {run.gwp_set} — recompute or document the jurisdictional "
                        f"requirement permitting it")

    # IFRS S2 ¶29(a)(vi): disclose the Scope 3 categories included. Screen all 15.
    s3gate = scope3_completeness(db, run)
    blockers.extend(s3gate.get("blockers", []))

    # Cat 15 financed emissions (frozen) roll into the disclosed Scope 3 / totals.
    _financed_tco2e = (run.financed_co2e or 0.0) / 1000.0
    _cat15 = (((s.get("scope3_ghgp") or {}).get("categories") or {}).get("15") or {})
    _cat15_financed = _cat15.get("financed_emissions")
    if _cat15_financed is not None:
        # ¶B58-B63: financed emissions must be disclosed WITH the gross exposure and
        # the % of it they cover — a financed figure without its exposure denominator
        # is not interpretable. Now that gross exposure is capturable, its absence
        # blocks rather than being silently omitted.
        if _cat15_financed.get("gross_exposure_total") is None:
            blockers.append("IFRS S2 Cat 15 (¶B58-B63): financed emissions disclosed without "
                            "gross exposure — set gross_exposure_total on the Cat 15 Scope 3 "
                            "declaration so the % of exposure covered can be reported")

    ef_sources = run_factor_sources(db, run)
    dq = s.get("data_quality") or {}
    s3inv = s.get("scope3_ghgp") or {}
    scope3_cats = category_tco2e(s3inv)
    scope3_categories_included = (
        s3inv["completeness"]["by_status"]["included"] if s3inv.get("assessable") else None)

    methodology = (
        f"GHG emissions measured in accordance with the GHG Protocol Corporate "
        f"Standard as required by IFRS S2, {run.gwp_set} GWP-100 applied per gas at "
        f"calculation time where per-gas factors exist. Emission factors: "
        f"{', '.join(ef_sources) or 'none'}. Scope 2 disclosed location-based with "
        f"market-based/contractual-instrument information alongside (S2 para 29(a)(v)). "
        f"Biogenic CO2 reported separately. Immutable calculation run #{run.id} of "
        f"{run.created_at}; every figure traceable to source records and pinned factor "
        f"versions. Coverage {cov['coverage_pct']}% ({cov['coverage_basis']}); "
        f"emissions-weighted data-quality score "
        f"{dq.get('emissions_weighted_score') if dq.get('has_data') else 'n/a'} "
        f"(1 best..5 worst); primary-data share "
        f"{s['method_split']['primary_data_share_pct']}%."
    )

    return {
        "framework": "ISSB IFRS S2",
        "jurisdiction_profile": {"key": jurisdiction, **profile},
        "disclosure_ready": not blockers,
        "blockers": blockers,
        "run": run_info,
        "reporting_period_id": run.reporting_period_id,
        "ghg_emissions_tco2e": {
            "scope1_gross": round(by_scope.get("1", 0.0) / 1000.0, 6),
            "scope2_location_based_gross": round(s["scope2"]["location_based"] / 1000.0, 6),
            "scope2_market_based_information": round(s["scope2"]["market_based"] / 1000.0, 6),
            "scope2_contractual_instruments": {
                "kwh_contractual": s["scope2"]["kwh_contractual"],
                "kwh_grid_fallback": s["scope2"]["kwh_grid_fallback"],
            },
            "scope3_gross_excl_financed": round(by_scope.get("3", 0.0) / 1000.0, 6),
            "scope3_gross": round(by_scope.get("3", 0.0) / 1000.0 + _financed_tco2e, 6),
            "scope3_by_ghgp_category_tco2e": scope3_cats,
            "scope3_categories_included": scope3_categories_included,
            # IFRS S2 ¶29(a)(vi) + ¶B58-B63 + Dec-2025 ¶29A: financed emissions are a
            # MANDATORY Cat 15 subtotal for financial institutions.
            "scope3_cat15_financed": _cat15_financed,
            "total_location_based": round(run.total_co2e / 1000.0 + _financed_tco2e, 6),
            "total_market_based": round(run.total_co2e_market / 1000.0 + _financed_tco2e, 6),
            "biogenic_co2_separate": round((run.total_biogenic_co2e or 0.0) / 1000.0, 6),
            "gwp_source": f"IPCC {run.gwp_set} GWP-100",
        },
        "not_covered": NOT_COVERED,
        "method_split": s["method_split"],
        "data_quality": dq,
        "methodology_statement": methodology,
        "coverage": cov,
        "exclusions": s["exclusions"],
    }
