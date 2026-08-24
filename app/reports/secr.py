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

from . import derivation as D
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import ActivityRecord, CalculationRun, EmissionLineItem
from ..services.units import convert, UnitConversionError
from ..services.boundary import boundary_completeness
from ..services.residual_mix import scope2_residual_mix_completeness
from ..services.comparability import denominator_period_comparable
from .summary import summary, run_factor_sources
from ..services.frozen import parse_detail

# Energy content used to express transport fuel as kWh for the SECR energy
# figure. DEMO constant (net CV, DEFRA-style); replace with the licensed
# DEFRA value for real use — deliberately named and surfaced in the report.
DIESEL_KWH_PER_LITRE_DEMO = 10.0

# Carriers that count toward the SECR "energy use" figure and how to get kWh.
_ENERGY_CARRIERS = ("electricity", "gas", "diesel")
# Units that denote an energy-bearing quantity. Used only to DETECT carriers the kWh
# figure omits — never to convert them, which would need an audited calorific value.
# Matched case-INSENSITIVELY on a normalised token, because the units that actually appear
# in a SECR filing are the ones an exact-case list misses: m3 for gas by volume, tonne and
# kg for solid fuel, and lowercase kwh/l from a spreadsheet export.
_ENERGY_UNITS = {
    "kwh", "mwh", "gwh", "wh", "gj", "mj", "kj", "therm", "therms", "btu", "mmbtu",
    "l", "litre", "litres", "liter", "liters", "gal", "gallon", "gallons",
    "m3", "m^3", "scf", "ccf", "mcf",
    "kg", "t", "tonne", "tonnes", "ton", "tons",
}


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
    # ONE pass over the run's location lines, partitioned by their FROZEN carrier: into
    # the kWh total, or into the `carriers_omitted` caveat below. The carrier filter runs
    # in Python because the category it filters on is the LINE's, not the live
    # ActivityRecord's — a post-run category or unit edit used to change a FILED SECR /
    # ESOS / ESRS E1-5 energy figure, and to move a carrier in or out of the completeness
    # caveat, without the immutable run changing at all.
    # Columns rather than the ORM entity, since every location line is now read and the
    # live values serve only as the pre-freeze fallback.
    q = db.query(ActivityRecord.id, ActivityRecord.category, ActivityRecord.unit,
                 ActivityRecord.quantity, EmissionLineItem.details).join(
        EmissionLineItem, EmissionLineItem.activity_id == ActivityRecord.id)\
        .filter(EmissionLineItem.run_id == run.id,
                EmissionLineItem.method == "location")
    if scopes is not None:
        q = q.filter(EmissionLineItem.scope.in_(scopes))
    rows = q.all()
    out = {c: 0.0 for c in _ENERGY_CARRIERS}
    notes = []
    weighted_any = False
    # Any energy-bearing activity OUTSIDE the three-carrier allowlist had its emissions
    # counted in Scope 1/2 while its energy silently vanished from the kWh figure. The
    # omission is now measured and disclosed; an undisclosed one is what made a partial
    # energy total read as complete.
    # Restricted to the SAME scopes as the figure it annotates — the `scopes` filter
    # above. Without this it reported a Scope 3 line as an omission from an
    # own-operations total it was never part of, and asserted its emissions sat in
    # Scope 1/2, which was simply false, in a filed CSRD disclosure.
    omitted: dict = {}
    for _aid, _live_cat, _live_unit, _live_qty, details in rows:
        _d = parse_detail(details)
        # `in`, not a truthiness test: a run frozen before the category/unit freeze has
        # no better source than the live activity, but a run that froze a NULL category
        # must stay distinguishable from one that froze no category at all.
        cat = _d["activity_category"] if "activity_category" in _d else _live_cat
        unit = _d["activity_unit"] if "activity_unit" in _d else _live_unit
        if cat not in _ENERGY_CARRIERS:
            # A NULL carrier is in NEITHER bucket: it is not one of the reported carriers,
            # and naming it as an omitted one would assert a carrier nobody recorded.
            # (The SQL `~in_()` this replaced dropped it the same way, via NULL logic.)
            if cat is not None and (unit or "").strip().lower().replace(" ", "") in _ENERGY_UNITS:
                omitted[cat] = omitted.get(cat, 0) + 1
            continue
        share = 1.0
        if consolidated:
            share = (_d.get("consolidation") or {}).get("share_factor", 1.0)
            if share != 1.0:
                weighted_any = True
        # The FROZEN quantity, which carries any temporal proration. Reading the live
        # ActivityRecord.quantity put the energy figure on the GROSS basis beside emissions
        # that were prorated — a wrong implied intensity, and 2000 kWh reported across two
        # periods for a 1000 kWh invoice. Falls back for runs frozen before proration
        # existed, whose details carry no `quantity` key, so those stay byte-identical.
        _qty = _d["quantity"] if "quantity" in _d else _live_qty
        try:
            if cat == "diesel":
                litres = convert(_qty, unit, "L")
                out["diesel"] += litres * DIESEL_KWH_PER_LITRE_DEMO * share
                notes.append(f"diesel converted at DEMO constant "
                             f"{DIESEL_KWH_PER_LITRE_DEMO} kWh/L")
            else:
                out[cat] += convert(_qty, unit, "kWh") * share
        except UnitConversionError as exc:
            notes.append(f"activity {_aid} excluded from energy figure: {exc}")
    if omitted:
        out["carriers_omitted"] = omitted
        _where = ("the Scope 1/2 figures" if scopes is None or set(scopes) <= {"1", "2"}
                  else f"the Scope {'/'.join(sorted(scopes))} figures")
        notes.append(
            f"energy-denominated activities outside the reported carriers "
            f"({', '.join(sorted(omitted))}) are NOT in this kWh total, though their "
            f"emissions ARE in {_where} — the energy figure is therefore incomplete "
            f"relative to the emissions beside it")
    out["total_kwh"] = sum(v for k, v in out.items() if k in _ENERGY_CARRIERS)
    out["carriers_reported"] = list(_ENERGY_CARRIERS)
    out["basis"] = "consolidated_entity_share" if consolidated else "gross_physical_energy"
    if consolidated and weighted_any:
        notes.append("energy weighted by the GHGP Ch.3 entity share, on the same basis "
                     "as the emissions reported beside it")
    out["notes"] = sorted(set(notes))
    return out


def secr_report(db: Session, organisation_id: int, run_id: Optional[int] = None,
                intensity_denominator: Optional[float] = None,
                intensity_denominator_unit: Optional[str] = None,
                intensity_denominator_period_days: Optional[int] = None) -> dict:
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
    blockers.extend(scope2_residual_mix_completeness(db, run).get("blockers", []))

    energy = _energy_kwh(db, run)

    intensity = None
    if intensity_denominator and math.isfinite(intensity_denominator) and intensity_denominator > 0:
        # SECR's ratio divides a period-scoped total by a per-period quantity. GRI gated
        # this and SECR did not, so one renderer refused the exact denominator the other
        # published a 4x-wrong ratio from.
        _dp = denominator_period_comparable(db, run, intensity_denominator_period_days,
                                            ratio_name="SECR intensity ratio")
        if _dp:
            blockers.append(_dp)
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

    # Worked calculations for every disclosed figure. Terms carry FULL precision while
    # the payload publishes 6dp, which is the correct order — rounding intermediates then
    # aggregating drifts. `display_dp` makes reconciliation check the right thing: that
    # the report rounded the correct underlying number.
    derivations = [
        D.total_of("Scope 1 + 2 (location-based)",
                   [{"k": "Scope 1", "v": scope1_kg / 1000.0},
                    {"k": "Scope 2 (location-based)", "v": scope2_loc_kg / 1000.0}],
                   label_key="k", value_key="v", unit="tCO2e", display_dp=6,
                   stated_value=round((scope1_kg + scope2_loc_kg) / 1000.0, 6),
                   basis=f"{run.gwp_set} GWP-100",
                   note="SECR's headline is Scope 1 + 2 on the LOCATION basis. The "
                        "market-based figure is disclosed alongside, never added to it."),
        D.alternatives("Scope 2 — dual reported",
                       [("Location-based", round(scope2_loc_kg / 1000.0, 6)),
                        ("Market-based", round(scope2_mkt_kg / 1000.0, 6))],
                       reported=round(scope2_loc_kg / 1000.0, 6), unit="tCO2e",
                       note="GHG Protocol Scope 2 Guidance requires both; they measure "
                            "the same electricity two ways and are never summed."),
        D.total_of("Total (location-based)",
                   [{"k": "Scope 1", "v": scope1_kg / 1000.0},
                    {"k": "Scope 2 (location-based)", "v": scope2_loc_kg / 1000.0},
                    {"k": "Scope 3 (voluntary under SECR)", "v": scope3_kg / 1000.0}],
                   label_key="k", value_key="v", unit="tCO2e", display_dp=6,
                   stated_value=round(run.total_co2e / 1000.0, 6), independent=True,
                   note="Scope 3 is voluntary under SECR but is included in this total "
                        "because the run computed it; the mandatory figure is the "
                        "Scope 1 + 2 line above."),
    ]
    if intensity:
        derivations.append(D.ratio(
            "Intensity ratio",
            f"Scope 1 + 2 location-based (tCO2e)",
            (scope1_kg + scope2_loc_kg) / 1000.0,
            f"Denominator ({intensity_denominator_unit or 'unit'})", intensity_denominator,
            unit=f"tCO2e per {intensity_denominator_unit or 'unit'}", display_dp=6,
            stated_value=intensity["tco2e_scope1_and_2_location"],
            note="The denominator is supplied by the preparer. SECR does not prescribe "
                 "one, so confirm it covers the SAME entities and period as the "
                 "emissions numerator — a mismatched boundary is the usual defect here."))
    if energy and energy.get("total_kwh") is not None:
        rows = [{"k": c, "v": energy[c]} for c in _ENERGY_CARRIERS if c in energy]
        if rows:
            _om = energy.get("carriers_omitted") or {}
            _note = ("Gross physical energy at operated sites — not converted to, or "
                     "derived from, the emissions figures above.")
            if _om:
                # The arithmetic adds up, but the figure is INCOMPLETE. Saying only that
                # it reconciles would let a tick read as completeness.
                _note += (f" INCOMPLETE: {', '.join(sorted(_om))} carry energy that this "
                          f"total does not include, while their emissions are in the "
                          f"figures above.")
            derivations.append(D.total_of(
                "Energy use", rows, label_key="k", value_key="v", unit="kWh",
                stated_value=energy["total_kwh"], basis=energy.get("basis"), note=_note))

    return {
        "framework": "UK SECR",
        "derivations": D.summarise(derivations),
        "disclosure_ready": not blockers,
        "blockers": blockers,
        # SECR's mandatory headline is Scope 1 + 2; Scope 3 is voluntary. An activity
        # ASSUMED into Scope 3 is therefore assumed OUT of the mandatory figure, which is
        # exactly the caveat a reader needs beside it. None when nothing was assumed.
        "scope_assumptions": s.get("scope_assumptions"),
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
