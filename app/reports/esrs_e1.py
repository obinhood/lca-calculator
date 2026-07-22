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
import math
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CalculationRun, EmissionLineItem
from .summary import summary, run_factor_sources
from .secr import _energy_kwh
from ..services.ghgp import scope3_completeness
from ..services.boundary import boundary_completeness
from ..services.removals import removals_completeness
from ..services.residual_mix import scope2_residual_mix_completeness

NOT_COVERED = [
    "E1-1 transition plan for climate change mitigation",
    "E1-2 policies related to climate change",
    "E1-3 actions and resources",
    "E1-4 targets related to climate change",
    "E1-8 internal carbon pricing",
    "E1-9 anticipated financial effects",
]


def _e1_7(db: Session, run: CalculationRun, as_of: Optional[str] = None) -> dict:
    """E1-7 removals & carbon credits, from the retired-credit register applied
    to this run (was hardcoded 'none' before the credits module existed).

    The credits ledger is live, so an ``as_of`` cutoff (retirement_date <= as_of)
    makes a filed disclosure reproducible — re-pulling the same run + as_of
    returns the same figures even after later retirements. Stamped in output.
    """
    from ..models import CarbonCredit
    q = db.query(CarbonCredit).filter(
        CarbonCredit.organisation_id == run.organisation_id,
        CarbonCredit.retired.is_(True),
        CarbonCredit.applied_to_run_id == run.id)
    if as_of is not None:
        q = q.filter(CarbonCredit.retirement_date <= as_of)
    applied = q.all()
    removals = sum(c.quantity_tco2e for c in applied if c.credit_type == "removal")
    credits = sum(c.quantity_tco2e for c in applied)

    # E1-7 ¶56-59 mandates THREE distinct pools: gross emissions != INVENTORY removals
    # (the org's own within-boundary sequestration) != PURCHASED credits (market
    # instruments). Inventory removals come from the frozen removals pool.
    from ..models import RunRemovalLine
    rlines = db.query(RunRemovalLine).filter(RunRemovalLine.run_id == run.id).all()
    own = sum(l.co2e for l in rlines if l.scope == "1" and l.record_kind == "removal") / 1000.0
    vc = sum(l.co2e for l in rlines if l.scope == "3" and l.record_kind == "removal") / 1000.0
    reversals = (run.removals_reversed_co2e or 0.0) / 1000.0
    by_cat = {}
    for l in rlines:
        if l.record_kind == "removal":
            by_cat[l.removal_category] = by_cat.get(l.removal_category, 0.0) + l.co2e / 1000.0
    inventory_removals = ({
        "own_operations_tco2e": round(own, 6),
        "value_chain_tco2e": round(vc, 6),
        "reversals_tco2e": round(reversals, 6),
        "net_removals_tco2e": round(own + vc - reversals, 6),
        "by_category_tco2e": {k: round(v, 6) for k, v in by_cat.items()},
        "as_of": run.removals_as_of,
        "note": "Own within-boundary removals (GHG Protocol Land Sector & Removals). "
                "Reported SEPARATELY — NOT counted toward gross emissions or gross-"
                "reduction targets (E1-7 ¶56-57).",
    } if run.total_removals_co2e is not None else {"evaluated": False})
    return {
        # NEW: inventory removals — the org's own sequestration, distinct from credits.
        "inventory_removals": inventory_removals,
        # Purchased/retired carbon credits (market instruments), distinct from the above.
        "purchased_credits": {
            "removals_retired_tco2e": round(removals, 6),
            "credits_retired_total_tco2e": round(credits, 6),
            "credit_count": len(applied),
            "as_of": as_of,
            "note": "PURCHASED credits retired against this run (ISO 14068). A removal-type "
                    "credit is a market instrument, distinct from an inventory removal above.",
        },
        # Back-compat keys (purchased credits), retained so existing consumers don't break.
        "removals_retired_tco2e": round(removals, 6),
        "credits_retired_total_tco2e": round(credits, 6),
        "credit_count": len(applied),
        "as_of": as_of,
        "note": ("Three separate pools: gross emissions, inventory removals, purchased "
                 "credits — none netted into the gross total (E1-7 ¶56-59)."),
    }


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
                   revenue_currency: str = "EUR",
                   credits_as_of: Optional[str] = None) -> dict:
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
    if (not net_revenue_millions or not math.isfinite(net_revenue_millions)
            or net_revenue_millions <= 0):
        blockers.append("net_revenue_millions required: E1-6 mandates GHG intensity "
                        "per net revenue")
    # ESRS AR 46(i): Scope 3 must be screened across all 15 GHG Protocol categories,
    # each quantified or excluded with a justification. An UNDECLARED or NOT-MEASURED
    # category, or a Scope 3 line with no category, blocks the disclosure — this is
    # what stops "3 of 15 categories" reading as a complete inventory.
    s3gate = scope3_completeness(db, run)
    blockers.extend(s3gate.get("blockers", []))
    blockers.extend(scope2_residual_mix_completeness(db, run).get("blockers", []))
    # GHG Protocol Ch.3: the consolidation boundary determines what share of each
    # entity is in these figures — an unresolved boundary cannot be disclosed.
    blockers.extend(boundary_completeness(db, run).get("blockers", []))
    # GHG Protocol Land Sector & Removals: an unresolved/unpermanent removal blocks.
    blockers.extend(removals_completeness(db, run).get("blockers", []))

    # E1-5: ESRS reports energy in MWh, bounded to own operations (Scope 1/2
    # line items) — unlike SECR's deliberately scope-agnostic UK energy figure.
    # ESRS E1-5's scope follows the consolidation scope, so energy must be on the
    # SAME basis as the E1-6 emissions beside it — otherwise the payload implies
    # a wrong intensity (gross kWh over consolidated tCO2e).
    energy_kwh = _energy_kwh(db, run, scopes=("1", "2"), consolidated=True)
    renewable_mwh = _renewable_contractual_mwh(db, run)
    total_mwh = energy_kwh["total_kwh"] / 1000.0
    energy = {
        "total_mwh": round(total_mwh, 6),
        "by_carrier_mwh": {c: round(energy_kwh[c] / 1000.0, 6)
                           for c in ("electricity", "gas", "diesel")},
        "electricity_renewable_contractual_mwh": round(renewable_mwh, 6),
        "note": ("Scope 1/2 own-operations energy only. Renewable split covers "
                 "contractual instruments (REC/PPA) from the run's volume-matched "
                 "allocations; supplier fuel-mix data beyond instruments is not yet "
                 "captured. " + " ".join(energy_kwh["notes"])),
    }

    # E1-6 Scope 3 by the 15 GHG Protocol categories (ESRS ¶51 / AR 46), from the
    # run's frozen lineage. The value-chain completeness statement (AR 46(i)) lives
    # in scope3_screening below.
    s3inv = s.get("scope3_ghgp") or {}
    scope3_ghgp_categories = {
        k: {"name": v["name"], "tco2e": v["tco2e"], "declared_status": v["declared_status"],
            "primary_data_pct": v["primary_data_pct"],
            "method_description": v["method_description"]}
        for k, v in (s3inv.get("categories") or {}).items()
    } if s3inv.get("assessable") else None

    # ESRS ¶51-52: gross Scope 3 includes every significant category — for a
    # financial institution, Cat 15 (financed emissions) always is. The DISCLOSED
    # totals therefore add financed emissions; run.total_co2e (activity-derived) is
    # never changed. Both figures are emitted and reconciled.
    financed_tco2e = (run.financed_co2e or 0.0) / 1000.0
    scope3_disclosed = scope3_kg / 1000.0 + financed_tco2e
    total_loc_disclosed = run.total_co2e / 1000.0 + financed_tco2e
    total_mkt_disclosed = run.total_co2e_market / 1000.0 + financed_tco2e

    intensity = None
    if net_revenue_millions and math.isfinite(net_revenue_millions) and net_revenue_millions > 0:
        # ¶52's total and E1-6 intensity must agree inside one payload -> intensity
        # is off the DISCLOSED total (financed included).
        intensity = {
            "tco2e_total_location_per_million_revenue":
                round(total_loc_disclosed / net_revenue_millions, 6),
            "tco2e_total_market_per_million_revenue":
                round(total_mkt_disclosed / net_revenue_millions, 6),
            "net_revenue_millions": net_revenue_millions,
            "revenue_currency": revenue_currency,
        }

    # Frozen lineage — never via the live activity->factor mapping.
    ef_sources = run_factor_sources(db, run)
    dq = s.get("data_quality") or {}
    methodology = (
        f"GHG figures prepared per the GHG Protocol Corporate Standard as referenced "
        f"by ESRS E1 (AR {run.gwp_set} GWP-100, applied per gas at calculation time "
        f"where per-gas factors exist). Emission factors: "
        f"{', '.join(ef_sources) or 'none'}. "
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
            "scope3_excl_financed": round(scope3_kg / 1000.0, 6),
            "scope3": round(scope3_disclosed, 6),          # gross, incl. Cat 15 financed
            "scope3_ghgp_categories": scope3_ghgp_categories,
            "total_location_based_excl_financed": round(run.total_co2e / 1000.0, 6),
            "total_location_based": round(total_loc_disclosed, 6),
            "total_market_based": round(total_mkt_disclosed, 6),
            "biogenic_co2_separate": round((run.total_biogenic_co2e or 0.0) / 1000.0, 6),
            # E1-7 ¶56-57: removals reported separately, NOT deducted from gross or
            # gross-reduction targets — the net line is additional information only.
            "inventory_removals_tco2e": (round(run.total_removals_co2e / 1000.0, 6)
                                         if run.total_removals_co2e is not None else None),
            "net_of_removals_tco2e": (
                round((run.total_co2e - (run.total_removals_co2e - (run.removals_reversed_co2e or 0.0)))
                      / 1000.0, 6) if run.total_removals_co2e is not None else None),
            "financed_emissions": ({
                "included_in_total": True,
                "tco2e": round(financed_tco2e, 6),
                "as_of": run.financed_as_of,
                "note": "PCAF Part A (Dec 2022), frozen to immutable run #%d. NOT part of "
                        "run.total_co2e (which is activity-derived; positions are a live "
                        "ledger). Re-pull the run to reproduce." % run.id,
            } if run.financed_co2e is not None else {"included_in_total": False}),
            "ghg_intensity": intensity,
        },
        # AR 46(i): the value-chain completeness statement — which categories are
        # included vs excluded, and why. The 15-row detail is at /reports/scope3_inventory.
        "e1_6_scope3_screening": ({
            "standard": s3inv.get("standard_version"),
            "included": s3inv["completeness"]["by_status"]["included"],
            "not_applicable": s3inv["completeness"]["by_status"]["not_applicable"],
            "not_material": s3inv["completeness"]["by_status"]["not_material"],
            "not_measured": s3inv["completeness"]["by_status"]["not_measured"],
            "undeclared": s3inv["completeness"]["by_status"]["undeclared"],
            "inventory_coverage_pct": s3inv["completeness"]["inventory_coverage_pct"],
            "warnings": s3inv["completeness"]["warnings"],
        } if s3inv.get("assessable") else {
            "assessable": False,
            "note": "run predates the 15-category dimension — recompute"}),
        "e1_5_energy_consumption": energy,
        "e1_7_removals_and_credits": _e1_7(db, run, as_of=credits_as_of),
        "not_covered": NOT_COVERED,
        "method_split": s["method_split"],
        "data_quality": dq,
        "methodology_statement": methodology,
        "coverage": cov,
        "exclusions": s["exclusions"],
    }
