"""UK SECR (Streamlined Energy & Carbon Reporting) disclosure renderer.

Renders one immutable CalculationRun into the SECR datapoints a large unquoted
UK company / LLP must publish in its directors' report:
  * UK energy use (kWh) — electricity, gas, transport fuel
  * Associated Scope 1 & 2 GHG emissions (tCO2e), Scope 2 dual-reported
  * At least one intensity ratio
  * A methodology statement

Fail-closed disclosure: the report always states whether it is disclosure-ready
and WHY NOT if it isn't (partial run, unmapped activities, stale run) — a
pre-submission validation gate, not a silent pass.
"""
import json
import math
from typing import Optional

from sqlalchemy.orm import Session

from ..models import ActivityRecord, CalculationRun, EmissionLineItem
from ..services.units import convert, UnitConversionError
from ..services.boundary import boundary_completeness
from .summary import summary, run_factor_sources

# Energy content used to express transport fuel as kWh for the SECR energy
# figure. DEMO constant (net CV, DEFRA-style); replace with the licensed
# DEFRA value for real use — deliberately named and surfaced in the report.
DIESEL_KWH_PER_LITRE_DEMO = 10.0

# Carriers that count toward the SECR "energy use" figure and how to get kWh.
_ENERGY_CARRIERS = ("electricity", "gas", "diesel")


def _energy_kwh(db: Session, run: CalculationRun, scopes=None,
                consolidated: bool = False) -> dict:
    """kWh of energy use per carrier for the activities in this run.

    ``scopes`` filters by the line items' FROZEN scope (e.g. ("1", "2") for
    ESRS E1-5 own-operations energy); None means unscoped (SECR's total UK
    energy use is deliberately scope-agnostic).

    ``consolidated`` selects the BASIS, which differs by framework and must never be
    left implicit — energy reported on a different basis from the emissions beside it
    implies a wrong intensity (a 40% JV's 1000 kWh next to its consolidated 0.2 tCO2e
    implies 0.2 kgCO2e/kWh against a 0.5 factor):
      * False (default) = GROSS physical energy — correct for the site-level regimes.
        SECR reports UK energy USE and ESOS audits significant energy CONSUMPTION at
        the sites you operate; that is a physical quantity, not an equity share of one.
      * True = energy weighted by the GHGP Ch.3 entity share, read from the location
        line's FROZEN share_factor (never the live entity — reproduction contract).
        Correct for ESRS E1-5, whose scope follows the consolidation scope.
    """
    q = db.query(ActivityRecord, EmissionLineItem.details).join(
        EmissionLineItem, EmissionLineItem.activity_id == ActivityRecord.id)\
        .filter(EmissionLineItem.run_id == run.id,
                EmissionLineItem.method == "location",
                ActivityRecord.category.in_(_ENERGY_CARRIERS))
    if scopes is not None:
        q = q.filter(EmissionLineItem.scope.in_(scopes))
    rows = q.all()
    out = {c: 0.0 for c in _ENERGY_CARRIERS}
    notes = []
    weighted_any = False
    for a, details in rows:
        share = 1.0
        if consolidated:
            share = ((json.loads(details or "{}").get("consolidation") or {})
                     .get("share_factor", 1.0))
            if share != 1.0:
                weighted_any = True
        try:
            if a.category == "diesel":
                litres = convert(a.quantity, a.unit, "L")
                out["diesel"] += litres * DIESEL_KWH_PER_LITRE_DEMO * share
                notes.append(f"diesel converted at DEMO constant "
                             f"{DIESEL_KWH_PER_LITRE_DEMO} kWh/L")
            else:
                out[a.category] += convert(a.quantity, a.unit, "kWh") * share
        except UnitConversionError as exc:
            notes.append(f"activity {a.id} excluded from energy figure: {exc}")
    out["total_kwh"] = sum(v for k, v in out.items() if k in _ENERGY_CARRIERS)
    out["basis"] = "consolidated_entity_share" if consolidated else "gross_physical_energy"
    if consolidated and weighted_any:
        notes.append("energy weighted by the GHGP Ch.3 entity share, on the same basis "
                     "as the emissions reported beside it")
    out["notes"] = sorted(set(notes))
    return out


def secr_report(db: Session, organisation_id: int, run_id: Optional[int] = None,
                intensity_denominator: Optional[float] = None,
                intensity_denominator_unit: Optional[str] = None) -> dict:
    """SECR disclosure payload for one run (latest for the org by default)."""
    s = summary(db, organisation_id=organisation_id, run_id=run_id)
    run_info = s.get("run")
    if run_info is None:
        return {"disclosure_ready": False,
                "blockers": ["no calculation run exists — upload activities and run a calculation"]}
    run = db.get(CalculationRun, run_info["id"])

    by_scope = {row["scope"]: row["co2e"] for row in s["by_scope"]}
    scope1_kg = by_scope.get("1", 0.0)
    scope2_loc_kg = s["scope2"]["location_based"]
    scope2_mkt_kg = s["scope2"]["market_based"]
    scope3_kg = by_scope.get("3", 0.0)

    # Pre-submission validation gates (fail-closed disclosure).
    blockers = []
    cov = s["coverage"]
    if s.get("partial"):
        blockers.append(f"run is PARTIAL — excluded activities: {s['partial_reasons']}")
    if cov["stale"]:
        blockers.append("run is STALE relative to current activity data — recompute first")
    if cov["coverage_pct"] < 100.0:
        blockers.append(f"coverage is {cov['coverage_pct']}% (count-based) — "
                        f"resolve unmapped/errored activities or document exclusions")

    # SECR's emissions are consolidated under the GHGP Ch.3 boundary, so an
    # unresolved boundary blocks. Its ENERGY figure stays gross physical energy
    # (UK energy use at operated sites), which is labelled via energy["basis"].
    blockers.extend(boundary_completeness(db, run).get("blockers", []))

    energy = _energy_kwh(db, run)

    intensity = None
    if intensity_denominator and math.isfinite(intensity_denominator) and intensity_denominator > 0:
        intensity = {
            "tco2e_scope1_and_2_location": round((scope1_kg + scope2_loc_kg) / 1000.0
                                                 / intensity_denominator, 6),
            "denominator": intensity_denominator,
            "denominator_unit": intensity_denominator_unit or "unit",
        }
    else:
        blockers.append("no intensity ratio denominator supplied "
                        "(SECR requires at least one intensity ratio)")

    # Frozen lineage: NEVER via the live activity->factor mapping (a post-run
    # re-map must not rewrite an immutable run's methodology statement).
    ef_sources = run_factor_sources(db, run)

    methodology = (
        f"Prepared in accordance with the GHG Protocol Corporate Standard using the "
        f"operational approach reflected in the underlying activity data. Emission factors: "
        f"{', '.join(ef_sources) or 'none'}. "
        f"GWP set {run.gwp_set} (IPCC 100-year), applied per gas at calculation time where "
        f"per-gas factors are available. Scope 2 dual-reported (location- and market-based, "
        f"GHG Protocol Scope 2 Guidance, volume-matched instruments). "
        f"Immutable calculation run #{run.id} of {run.created_at}; every figure is traceable "
        f"to source records and pinned factor versions. "
        f"Coverage: {cov['coverage_pct']}% of activity records ({cov['coverage_basis']}). "
        f"Emissions-weighted data-quality score "
        f"{run.data_quality_score if (run.total_co2e or 0) > 0 else 'n/a'} "
        f"(1 best..5 worst, ecoinvent pedigree); primary-data share "
        f"{s['method_split']['primary_data_share_pct']}%."
    )

    return {
        "framework": "UK SECR",
        "disclosure_ready": not blockers,
        "blockers": blockers,
        "run": run_info,
        "reporting_period_id": run.reporting_period_id,
        "emissions_tco2e": {
            "scope1": round(scope1_kg / 1000.0, 6),
            "scope2_location_based": round(scope2_loc_kg / 1000.0, 6),
            "scope2_market_based": round(scope2_mkt_kg / 1000.0, 6),
            "scope1_and_2_location": round((scope1_kg + scope2_loc_kg) / 1000.0, 6),
            "scope3_voluntary": round(scope3_kg / 1000.0, 6),
            "total_location_based": round(run.total_co2e / 1000.0, 6),
            "total_market_based": round(run.total_co2e_market / 1000.0, 6),
            # SECR has no financed-emissions duty; if the org holds financed positions
            # the omission is flagged (visible), never silent.
            "financed_emissions_excluded": run.financed_co2e is not None,
            # Reported separately across ALL renderers (ISO 14067) — omission
            # here would be a silent cross-framework inconsistency.
            "biogenic_co2_separate": round((run.total_biogenic_co2e or 0.0) / 1000.0, 6),
        },
        "energy_use_kwh": energy,
        "intensity_ratio": intensity,
        "methodology_statement": methodology,
        "coverage": cov,
        "exclusions": s["exclusions"],
    }
