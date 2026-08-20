"""EcoVadis Environment-theme readiness (evidence pack + gap list).

EcoVadis is a RATINGS scheme, not a carbon-accounting standard. It assesses four
themes — Environment, Labour & Human Rights, Ethics, Sustainable Procurement —
and scores each on a management-system model: POLICIES (commitments) ->
ACTIONS (measures taken) -> RESULTS (reported KPIs) -> REPORTING & VERIFICATION
(disclosure, certification, third-party assurance).

This renderer assembles, from the org's immutable inventory, the evidence an
EcoVadis assessor asks for on the CARBON/ENERGY portion of the Environment
theme, and names the gaps that would weaken it.

Deliberately NOT produced (fail-closed on honesty, not just on numbers):
  * a score or medal — only EcoVadis issues those; nothing here predicts one;
  * the Labour & Human Rights, Ethics, and Sustainable Procurement themes —
    the platform holds no data model for them;
  * the non-carbon parts of Environment (water, waste, biodiversity beyond the
    TNFD module, product end-of-life).
Policy/certification/report-publication inputs are SELF-ATTESTED by the caller
and labelled as such — the platform cannot verify them.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CalculationRun, EmissionsTarget, MarketInstrument, AssuranceEngagement
from ..services.boundary import boundary_completeness, boundary_comparable
from ..services.residual_mix import residual_mix_comparable
from ..services.comparability import period_comparable, period_payload
from .summary import summary
from .secr import _energy_kwh

# Contractual instruments that evidence low-carbon electricity procurement.
_CONTRACTUAL = ("rec", "ppa", "supplier_specific")


def _own_run(db: Session, organisation_id: int, run_id: int) -> Optional[CalculationRun]:
    """Org-scoped run lookup — a baseline run id from another tenant resolves to None."""
    return db.query(CalculationRun).filter(
        CalculationRun.id == run_id,
        CalculationRun.organisation_id == organisation_id).first()


def _status(evidence: list, gaps: list) -> str:
    if not evidence:
        return "missing"
    return "evidenced" if not gaps else "partial"


def ecovadis_readiness(db: Session, organisation_id: int, run_id: Optional[int] = None,
                       baseline_run_id: Optional[int] = None,
                       intensity_denominator: Optional[float] = None,
                       denominator_unit: str = "revenue",
                       has_environmental_policy: bool = False,
                       iso_14001_certified: bool = False,
                       published_sustainability_report: bool = False) -> dict:
    s = summary(db, organisation_id=organisation_id, run_id=run_id)
    run_info = s.get("run")
    if run_info is None:
        return {
            "framework": "EcoVadis (Environment theme — carbon & energy readiness)",
            "assessment_ready": False,
            "blockers": ["no calculation run exists — EcoVadis Results require a "
                         "reported GHG inventory"],
        }
    run = db.get(CalculationRun, run_info["id"])

    # Fail-closed gates, consistent with the disclosure renderers: an incomplete or
    # stale inventory must not be handed to an assessor as evidence.
    blockers = []
    if s.get("partial"):
        blockers.append(f"run is PARTIAL — {s['partial_reasons']}")
    if s["coverage"]["stale"]:
        blockers.append("run is STALE — recompute before submitting evidence")
    # EcoVadis evidence rests on consolidated emissions — an unresolved boundary
    # means the figures in the evidence pack are not disclosable.
    blockers.extend(boundary_completeness(db, run).get("blockers", []))

    by_scope = {r["scope"]: r["co2e"] for r in s["by_scope"]}
    scope1 = by_scope.get("1", 0.0)
    scope3 = by_scope.get("3", 0.0)
    s2 = s.get("scope2") or {}
    scope2_loc = s2.get("location_based", 0.0)
    scope2_mkt = s2.get("market_based", 0.0)
    # Own-operations energy on the CONSOLIDATED basis — the same call GRI 302-1 and ESRS
    # E1-5 make. The defaults (all scopes, gross physical energy) put a different kWh
    # figure beside the same run's consolidated emissions than the GRI 305/302 report an
    # assessor cross-checks this pack against, and implied a wrong intensity against them.
    energy = _energy_kwh(db, run, scopes=("1", "2"), consolidated=True)

    # --- RESULTS: the reported KPIs ------------------------------------------
    results_evidence, results_gaps = [], []
    results_evidence.append(f"GHG inventory reported: Scope 1 {scope1 / 1000:.3f} tCO2e, "
                            f"Scope 2 {scope2_loc / 1000:.3f} tCO2e (location) / "
                            f"{scope2_mkt / 1000:.3f} tCO2e (market), "
                            f"Scope 3 {scope3 / 1000:.3f} tCO2e")
    if energy["total_kwh"] > 0:
        results_evidence.append(
            f"Energy consumption reported: {energy['total_kwh']:.0f} kWh "
            f"(own operations, Scope 1/2; {energy['basis']})")
    else:
        results_gaps.append("no energy-carrier consumption reported (kWh) — EcoVadis "
                            "Results expect an energy KPI alongside emissions")
    # An assessor must not read a partial kWh figure as the whole energy footprint.
    if energy.get("carriers_omitted"):
        results_gaps.append(
            f"energy KPI is INCOMPLETE: {', '.join(sorted(energy['carriers_omitted']))} "
            f"carry energy that the kWh total excludes, while their emissions are in the "
            f"Scope 1/2 figures reported beside it")
    if scope3 <= 0:
        results_gaps.append("no Scope 3 emissions reported — value-chain emissions are "
                            "expected for a strong Environment score")
    intensity = None
    if intensity_denominator and intensity_denominator > 0:
        intensity = round((scope1 + scope2_loc) / 1000.0 / intensity_denominator, 6)
        results_evidence.append(f"Intensity: {intensity} tCO2e (Scope 1+2 location) "
                                f"per {denominator_unit}")
    else:
        results_gaps.append("no intensity metric — supply an intensity_denominator "
                            "(e.g. revenue, FTE) for a normalised KPI")
    cov = s["coverage"]["coverage_pct"]
    if cov < 100:
        results_gaps.append(f"inventory coverage is {cov}% of activities (count-based)")

    # --- ACTIONS: measures actually taken -------------------------------------
    actions_evidence, actions_gaps = [], []
    instruments = db.query(MarketInstrument).filter(
        MarketInstrument.organisation_id == organisation_id).all()
    renewable = [i for i in instruments
                 if i.instrument_type in _CONTRACTUAL and (i.kg_co2e_per_kwh or 0.0) == 0.0]
    procurement_saving_t = max(0.0, (scope2_loc - scope2_mkt)) / 1000.0
    if renewable:
        actions_evidence.append(
            f"{len(renewable)} zero-carbon electricity contract(s) "
            f"({', '.join(sorted({i.instrument_type for i in renewable}))}); "
            f"{s2.get('kwh_contractual', 0.0):.0f} kWh contractually covered in this run")
    if procurement_saving_t > 0:
        actions_evidence.append(
            f"Contractual procurement reduces market-based Scope 2 by "
            f"{procurement_saving_t:.3f} tCO2e vs location-based")
    if not renewable:
        actions_gaps.append("no zero-carbon electricity procurement (REC/PPA/supplier-specific) "
                            "recorded — a primary Environment 'Actions' evidence item")

    # Measured reduction vs a baseline run (the strongest Actions evidence) — and the
    # single figure in this pack most likely to be quoted back as an achievement. It is
    # the same two-immutable-runs subtraction GRI 305-5 makes, so it carries the same
    # comparability gates: a "reduction" that spans a GWP vintage, a residual-mix
    # methodology change, or a shorter reporting period is an artefact of the method, and
    # submitting it as evidence of action is a misrepresentation, not a weak KPI. Each is
    # a blocker on the pack, not a gap in a pillar.
    trend = None
    trend_comparability = None
    if baseline_run_id is not None:
        base = _own_run(db, organisation_id, baseline_run_id)
        if base is None:
            blockers.append("baseline_run_id not found for this organisation")
        elif (base.total_co2e or 0.0) <= 0:
            actions_gaps.append("baseline run has no emissions to compare against")
        else:
            if base.gwp_set != run.gwp_set:
                blockers.append(
                    f"trend_vs_baseline: the baseline run used {base.gwp_set} GWP but this "
                    f"run used {run.gwp_set} — a trend across GWP vintages is a change of "
                    f"metric, not a measured reduction; recompute both on one vintage")
            if (_rm := residual_mix_comparable(db, base, run)):
                blockers.append(f"trend_vs_baseline: {_rm}")
            if (_pc := period_comparable(db, base, run, label_a="baseline",
                                         label_b="current", quantity="the trend",
                                         measures="a measured reduction")):
                blockers.append(f"trend_vs_baseline: {_pc}")
            # The comment above claims "the same comparability gates" as GRI 305-5. GRI
            # applies FOUR; this block applied three. The missing one is the boundary:
            # a divestment between the two runs is the single most flattering thing that
            # can happen to an Actions-pillar trend, and it is not action.
            if (_bc := boundary_comparable(db, base, run, label_a="baseline",
                                           label_b="current", quantity="the trend")):
                blockers.append(f"trend_vs_baseline: {_bc}")
            trend_comparability = period_payload(
                db, base, run, key_a="baseline", key_b="current",
                note="A trend is only evidence of action if the two runs cover the same "
                     "length of time; a 12-month baseline against a 3-month current run "
                     "shows a 75% 'reduction' that is entirely elapsed time.")
            delta = (run.total_co2e or 0.0) - base.total_co2e
            pct = 100.0 * delta / base.total_co2e
            trend = {
                "baseline_run_id": base.id,
                "baseline_tco2e": round(base.total_co2e / 1000.0, 6),
                "current_tco2e": round((run.total_co2e or 0.0) / 1000.0, 6),
                "change_tco2e": round(delta / 1000.0, 6),
                "change_pct": round(pct, 2),
                "direction": "reduction" if delta < 0 else ("increase" if delta > 0 else "flat"),
                "baseline_gwp_set": base.gwp_set,
                "current_gwp_set": run.gwp_set,
                "comparability_note": "Valid only where the two runs share a GWP set, a "
                                      "residual-mix methodology, an organisational "
                                      "boundary and a comparable period length — each "
                                      "gated as a blocker; read `blockers` before "
                                      "quoting this as evidence.",
            }
            if delta < 0:
                actions_evidence.append(
                    f"Measured reduction of {abs(pct):.1f}% vs baseline run #{base.id}")
            else:
                actions_gaps.append(
                    f"emissions are {pct:.1f}% vs baseline run #{base.id} (no reduction "
                    "demonstrated) — EcoVadis Actions look for a measured downward trend")
    else:
        actions_gaps.append("no baseline run supplied — pass baseline_run_id to evidence a "
                            "measured reduction trend")

    # --- POLICIES: quantified commitments -------------------------------------
    policies_evidence, policies_gaps = [], []
    targets = db.query(EmissionsTarget).filter(
        EmissionsTarget.organisation_id == organisation_id).order_by(EmissionsTarget.id).all()
    for t in targets:
        policies_evidence.append(
            f"{t.target_type} target '{t.name}': {round(t.target_reduction_pct * 100, 1)}% "
            f"reduction on scopes {t.scope_coverage} by {t.target_year} vs {t.base_year}"
            + (f" (ambition {t.ambition})" if t.ambition else "")
            + (" — SBTi VALIDATED" if t.sbti_validated else " — not SBTi-validated"))
    if not targets:
        policies_gaps.append("no quantified emissions-reduction target set — EcoVadis expects "
                             "a public, time-bound, quantified commitment")
    elif not any(t.sbti_validated for t in targets):
        policies_gaps.append("no SBTi-validated target — external validation strengthens the "
                             "Policies pillar")
    if has_environmental_policy:
        policies_evidence.append("Formal environmental/climate policy in place (SELF-ATTESTED)")
    else:
        policies_gaps.append("no formal environmental/climate policy attested")

    # --- REPORTING & VERIFICATION ---------------------------------------------
    rv_evidence, rv_gaps = [], []
    eng = db.query(AssuranceEngagement).filter(
        AssuranceEngagement.organisation_id == organisation_id,
        AssuranceEngagement.run_id == run.id).order_by(AssuranceEngagement.id.desc()).first()
    if eng and eng.status == "concluded":
        rv_evidence.append(
            f"Third-party assurance over this run: {eng.standard} ({eng.level} assurance), "
            f"opinion {eng.opinion or 'n/a'}"
            + (f", assuror {eng.assuror_name}" if eng.assuror_name else ""))
    elif eng:
        rv_gaps.append(f"assurance engagement exists but is {eng.status} (not concluded)")
    else:
        rv_gaps.append("no third-party assurance over this run — verified data materially "
                       "strengthens the Environment score")
    if iso_14001_certified:
        rv_evidence.append("ISO 14001 environmental management system certified (SELF-ATTESTED)")
    else:
        rv_gaps.append("no ISO 14001 certification attested")
    if published_sustainability_report:
        rv_evidence.append("Sustainability report published (SELF-ATTESTED)")
    else:
        rv_gaps.append("no published sustainability report attested")
    rv_evidence.append("Disclosure reports available from this inventory: CDP export, "
                       "CSRD ESRS E1, ISSB IFRS S2, GRI 305, UK SECR")

    pillars = {
        "policies": {"status": _status(policies_evidence, policies_gaps),
                     "evidence": policies_evidence, "gaps": policies_gaps},
        "actions": {"status": _status(actions_evidence, actions_gaps),
                    "evidence": actions_evidence, "gaps": actions_gaps},
        "results": {"status": _status(results_evidence, results_gaps),
                    "evidence": results_evidence, "gaps": results_gaps},
        "reporting_and_verification": {"status": _status(rv_evidence, rv_gaps),
                                       "evidence": rv_evidence, "gaps": rv_gaps},
    }

    return {
        "framework": "EcoVadis (Environment theme — carbon & energy readiness)",
        "assessment_ready": not blockers,
        "blockers": blockers,
        # The KPIs below are reported per scope; an assumed scope is a caveat on the
        # evidence, and evidence submitted without it overstates what was verified.
        # None when nothing was assumed.
        "scope_assumptions": s.get("scope_assumptions"),
        "run": run_info,
        "pillars": pillars,
        "kpis": {
            "scope1_tco2e": round(scope1 / 1000.0, 6),
            "scope2_location_tco2e": round(scope2_loc / 1000.0, 6),
            "scope2_market_tco2e": round(scope2_mkt / 1000.0, 6),
            "scope3_tco2e": round(scope3 / 1000.0, 6),
            "total_tco2e_location": round((run.total_co2e or 0.0) / 1000.0, 6),
            "energy_kwh": round(energy["total_kwh"], 3),
            # The caveats travel WITH the number: which carriers it covers, which
            # energy-bearing ones it omits, its basis, and the diesel calorific-value
            # assumption all live in these notes.
            "energy_boundary": "own operations (Scope 1/2 line items)",
            "energy_basis": energy["basis"],
            "energy_carriers_reported": energy["carriers_reported"],
            "energy_carriers_omitted": energy.get("carriers_omitted"),
            "energy_complete": not energy.get("carriers_omitted"),
            "energy_notes": energy["notes"],
            "intensity_tco2e_per_denominator": intensity,
            "denominator_unit": denominator_unit,
            "coverage_pct": cov,
            "data_quality_score": s["data_quality"]["emissions_weighted_score"],
        },
        "trend_vs_baseline": trend,
        "trend_period_comparability": trend_comparability,
        "all_gaps": sorted({g for p in pillars.values() for g in p["gaps"]}),
        "not_assessed": [
            "Labour & Human Rights theme (no data model)",
            "Ethics theme (no data model)",
            "Sustainable Procurement theme (no data model)",
            "Non-carbon Environment topics (water, waste, product end-of-life)",
            "The EcoVadis score/medal itself — only EcoVadis issues it; nothing here predicts one",
        ],
        "note": "Evidence pack for the carbon/energy portion of the EcoVadis Environment "
                "theme, assembled from the org's immutable inventory. Policy, ISO 14001 and "
                "report-publication inputs are SELF-ATTESTED and unverifiable by the platform.",
    }
