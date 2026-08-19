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
from .summary import summary, run_factor_sources
from .scope3 import category_tco2e
from ..services.ghgp import scope3_completeness
from ..services.boundary import boundary_completeness, boundary_comparable
from ..services.residual_mix import (
    scope2_residual_mix_completeness, residual_mix_comparable,
)
from ..services.comparability import (
    PERIOD_TOLERANCE_PCT, period_comparable, period_payload, period_info,
    denominator_period_comparable,
)
from .secr import _energy_kwh


def gri_report(db: Session, organisation_id: int, run_id: Optional[int] = None,
               base_run_id: Optional[int] = None,
               intensity_denominator: Optional[float] = None,
               intensity_denominator_unit: Optional[str] = None,
               intensity_denominator_period_days: Optional[int] = None) -> dict:
    """GRI 305/302 content index for one immutable run.

    `intensity_denominator_period_days` states the span the 305-4/302-3 denominator
    covers. It is REQUIRED with a denominator: a bare float has no period, and a
    quarter-scoped run divided by an annual denominator yields a ratio 4x too low with
    both inputs individually correct (see `denominator_period_comparable`).
    """
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
    else:
        # A denominator is a quantity PER PERIOD, and 305-4/302-3 divide a period-scoped
        # total by it. Accepting a bare float left the one mismatch that no downstream
        # check can see — a quarter's emissions over an annual denominator is a ratio 4x
        # too low, and both inputs are individually right.
        _dp = denominator_period_comparable(db, run, intensity_denominator_period_days,
                                            ratio_name="305-4/302-3 intensity")
        if _dp:
            blockers.append(_dp)
    # GRI 305-3 discloses Scope 3 by category — screen all 15.
    blockers.extend(scope3_completeness(db, run).get("blockers", []))
    blockers.extend(scope2_residual_mix_completeness(db, run).get("blockers", []))
    blockers.extend(boundary_completeness(db, run).get("blockers", []))

    by_scope = {row["scope"]: row["co2e"] for row in s["by_scope"]}
    scope1_kg = by_scope.get("1", 0.0)
    scope3_kg = by_scope.get("3", 0.0)

    # 305-5: exact reduction vs an immutable base run of the SAME organisation.
    #
    # The subtraction is exact; what makes it a REDUCTION is that the two runs differ only
    # in the abatement. Four dimensions can move the delta on their own — GWP vintage,
    # residual-mix methodology, organisational boundary, and elapsed time — and each is
    # gated below. A delta that spans any of them is not a smaller reduction, it is not a
    # reduction at all, so each is a blocker.
    reductions = None
    period_comparability = None
    if base_run_id is not None:
        base = db.query(CalculationRun).filter(
            CalculationRun.id == base_run_id,
            CalculationRun.organisation_id == organisation_id).first()
        if base is None:
            blockers.append("base_run_id not found for this organisation")
        else:
            period_comparability = period_payload(
                db, base, run, key_a="base", key_b="reporting",
                note="A 305-5 reduction is only abatement if the two runs cover the same "
                     "length of time; a 12-month base minus a 3-month reporting run "
                     "reports the missing nine months as a 75% reduction. Both periods "
                     "are stated so the comparison can be checked, not assumed.")
            reductions = {
                "base_run_id": base.id,
                "base_run_created_at": base.created_at,
                "base_gwp_set": base.gwp_set,
                "base_consolidation_approach": base.consolidation_approach,
                "reporting_consolidation_approach": run.consolidation_approach,
                "base_reporting_period": period_info(db, base),
                "reporting_period": period_info(db, run),
                "reduction_location_based_tco2e":
                    round((base.total_co2e - run.total_co2e) / 1000.0, 6),
                "reduction_market_based_tco2e":
                    round((base.total_co2e_market - run.total_co2e_market) / 1000.0, 6),
                "note": "Exact difference between two immutable calculation runs; "
                        "positive = reduction. Valid only where the two runs share a GWP "
                        "set, a residual-mix methodology, a consolidation approach and "
                        "entity population, and a comparable period length — each gated "
                        "as a blocker above, so read `blockers` before quoting this.",
            }
            # Elapsed time — the dimension the arithmetic is blindest to, since both
            # totals are correct for the span they cover.
            _pc = period_comparable(db, base, run, label_a="base", label_b="reporting",
                                    quantity="the 305-5 reduction", measures="abatement")
            if _pc:
                blockers.append(f"305-5: {_pc}")
            # Consolidation approach and entity population: a divestment reads as a
            # reduction and an acquisition as an increase, with no decarbonisation either
            # way. Same detector SBTi uses to re-base a target (GHGP Ch.5).
            _bc = boundary_comparable(db, base, run, label_a="base", label_b="reporting",
                                      quantity="the 305-5 reduction")
            if _bc:
                blockers.append(f"305-5: {_bc}")
            if base.gwp_set != run.gwp_set:
                blockers.append(f"305-5 base run used {base.gwp_set} but this run used "
                                f"{run.gwp_set} — reductions across GWP vintages are not "
                                f"comparable")
            # Same class of trap as the GWP check above: the market-based total moves when
            # uncovered Scope 2 load starts being priced at the residual mix instead of the
            # grid average. A 'reduction' spanning that change is a methodology artefact,
            # not abatement.
            _rm = residual_mix_comparable(db, base, run)
            if _rm:
                blockers.append(f"305-5: {_rm}")
    energy = _energy_kwh(db, run, scopes=("1", "2"), consolidated=True)
    energy_mwh = {c: round(energy[c] / 1000.0, 6) for c in ("electricity", "gas", "diesel")}
    total_mwh = round(energy["total_kwh"] / 1000.0, 6)

    ef_sources = run_factor_sources(db, run)
    dq = s.get("data_quality") or {}

    # Echoed on BOTH intensity ratios (they share the denominator), so a period mismatch
    # is visible next to the figure rather than only in `blockers`.
    denominator_period = {
        "denominator_period_days": intensity_denominator_period_days,
        "numerator_period": period_info(db, run),
        "tolerance_pct": PERIOD_TOLERANCE_PCT * 100.0,
        "note": "The denominator must cover the same span as the emissions it divides. "
                "A period-scoped run over an annual denominator (a quarter against "
                "full-year revenue) is a ratio out by the ratio of the spans, with both "
                "inputs individually correct — so both spans are stated here.",
    }

    return {
        "framework": "GRI 305 Emissions / GRI 302 Energy",
        "disclosure_ready": not blockers,
        "blockers": blockers,
        # 305-1/2/3 are three per-scope disclosures; an assumed scope decides which of
        # them an activity lands in. None when nothing was assumed.
        "scope_assumptions": s.get("scope_assumptions"),
        "run": run_info,
        "gri_305_1_scope1": {
            "gross_tco2e": round(scope1_kg / 1000.0, 6),
            # The run accumulates ONE biogenic pool across all scopes, so it cannot be
            # attributed to Scope 1. Reporting it on the 305-1 line labelled it as
            # Scope 1 biogenic when it may include Scope 2 or 3 biogenic CO2, and left
            # 305-2 and 305-3 with no biogenic line at all. It is reported once, at the
            # top level, on the basis the run actually computed.
            "biogenic_co2_tco2_separate": None,
            "biogenic_note": "biogenic CO2 is tracked as a single all-scopes pool by this "
                             "run and cannot be split across 305-1/2/3 — see "
                             "biogenic_co2_tco2_all_scopes",
            "gases_included": "per-gas factors: CO2, CH4 (fossil/biogenic), N2O; "
                              "aggregate factors as published",
        },
        # GRI 305-1/2/3 each ask for biogenic CO2 separately; this run computes one
        # undifferentiated pool, so it is disclosed once with its basis stated rather
        # than assigned to a scope it may not belong to.
        "biogenic_co2_tco2_all_scopes": round((run.total_biogenic_co2e or 0.0) / 1000.0, 6),
        "gri_305_2_scope2": {
            "location_based_tco2e": round(s["scope2"]["location_based"] / 1000.0, 6),
            "market_based_tco2e": round(s["scope2"]["market_based"] / 1000.0, 6),
            "biogenic_co2_tco2_separate": None,
        },
        "gri_305_3_scope3": {
            "gross_tco2e": round(scope3_kg / 1000.0, 6),
            "biogenic_co2_tco2_separate": None,
            "by_ghgp_category_tco2e": category_tco2e(s.get("scope3_ghgp") or {}),
            # GRI 305-3 here reports activity-derived Scope 3; financed emissions
            # (Cat 15) are surfaced but not folded in — flagged so the omission is visible.
            "financed_emissions_excluded": run.financed_co2e is not None,
            "financed_emissions_tco2e": (round(run.financed_co2e / 1000.0, 6)
                                         if run.financed_co2e is not None else None),
        },
        "gri_305_4_intensity": ({
            "tco2e_per_unit": round(run.total_co2e / 1000.0 / intensity_denominator, 6),
            "denominator": intensity_denominator,
            "denominator_unit": intensity_denominator_unit or "unit",
            "period_basis": denominator_period,
            "scopes_included": "1+2(location)+3",
        } if denom_ok else None),
        "gri_305_5_reductions": reductions,
        "gri_305_5_period_comparability": period_comparability,
        "gri_302_1_energy": {"by_carrier_mwh": energy_mwh, "total_mwh": total_mwh,
                             "boundary": "own operations (Scope 1/2 line items)"},
        "gri_302_3_energy_intensity": ({
            "mwh_per_unit": round(total_mwh / intensity_denominator, 6),
            "denominator": intensity_denominator,
            "denominator_unit": intensity_denominator_unit or "unit",
            "period_basis": denominator_period,
        } if denom_ok else None),
        "methodology": f"GHG Protocol Corporate Standard; {run.gwp_set} GWP-100; "
                       f"factors: {', '.join(ef_sources) or 'none'}; immutable run "
                       f"#{run.id}; coverage {cov['coverage_pct']}%; DQ "
                       f"{dq.get('emissions_weighted_score') if dq.get('has_data') else 'n/a'}.",
        "coverage": cov,
        "exclusions": s["exclusions"],
    }
