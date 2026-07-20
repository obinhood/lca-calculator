import json
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import func
from ..models import (
    ActivityRecord, CalculationRun, EmissionLineItem, EmissionFactor, ReportingPeriod,
)
from ..services.calc import (
    activities_fingerprint, activities_in_scope, FINGERPRINT_VERSION,
)


def _resolve_run(db: Session, organisation_id: Optional[int], run_id: Optional[int]):
    """Resolve a run, ALWAYS scoped by organisation.

    A supplied ``run_id`` is filtered by ``organisation_id`` too, so a caller can
    never read another tenant's run by guessing an id (IDOR). A run belonging to a
    different org simply resolves to ``None`` (reported as "no run").
    """
    q = db.query(CalculationRun)
    if organisation_id is not None:
        q = q.filter(CalculationRun.organisation_id == organisation_id)
    if run_id is not None:
        return q.filter(CalculationRun.id == run_id).first()
    return q.order_by(CalculationRun.id.desc()).first()


def summary(db: Session, organisation_id: Optional[int] = None, run_id: Optional[int] = None):
    """Summary of a single immutable calculation run (latest for the org by default)."""
    run = _resolve_run(db, organisation_id, run_id)
    if run is None:
        return {
            "run": None,
            "total_co2e": 0.0,
            "by_scope": [],
            "by_category": [],
            "coverage": None,
            "notes": "No calculation run yet. Upload activities and POST /calculate/run.",
        }

    li = EmissionLineItem
    # Aggregations use the location-based line items only; market-based Scope 2
    # is a parallel view of the same activities, not additional emissions.
    by_scope = db.query(li.scope, func.sum(li.co2e))\
        .filter(li.run_id == run.id, li.method == "location").group_by(li.scope).all()
    by_cat = db.query(ActivityRecord.category, func.sum(li.co2e))\
        .join(li, li.activity_id == ActivityRecord.id)\
        .filter(li.run_id == run.id, li.method == "location")\
        .group_by(ActivityRecord.category).all()

    # GHG Protocol Scope 3 method split: how much of the total rests on which
    # calculation method (supplier_specific/hybrid = primary-leaning data;
    # spend_based = lowest tier). Assurers and the Scope 3 revision ask for this.
    # Read from the FROZEN per-line detail (method_type captured at compute time),
    # NOT the live activity->factor mapping — a re-map after the run must not
    # relabel this immutable run's method mix.
    method_split = {}
    for details, line_co2e in db.query(li.details, li.co2e)\
            .filter(li.run_id == run.id, li.method == "location").all():
        d = json.loads(details or "{}")
        m = d.get("method_type") or "average_data"
        method_split[m] = method_split.get(m, 0.0) + (line_co2e or 0.0)
    total_methods = sum(method_split.values())
    primary_kg = method_split.get("supplier_specific", 0.0) + method_split.get("hybrid", 0.0)

    scope2_location = next((v for s, v in by_scope if s == "2"), 0.0) or 0.0
    scope2_market = db.query(func.sum(li.co2e))\
        .filter(li.run_id == run.id, li.method == "market").scalar() or 0.0

    # Aggregate market-basis disclosure (Scope 2 Guidance): how much consumption
    # is contractually covered vs falling back to the grid average.
    market_lines = db.query(li.details)\
        .filter(li.run_id == run.id, li.method == "market").all()
    bases = {}
    kwh_contractual = 0.0
    kwh_grid_fallback = 0.0
    kwh_market_unverified = 0.0     # covered by an instrument whose market couldn't be checked
    skipped_market = set()          # instruments excluded by a declared market mismatch
    for (details,) in market_lines:
        d = json.loads(details or "{}")
        bases[d.get("method_basis", "?")] = bases.get(d.get("method_basis", "?"), 0) + 1
        kwh_contractual += d.get("kwh_contractual", 0.0) or 0.0
        kwh_grid_fallback += d.get("kwh_grid_fallback", 0.0) or 0.0
        kwh_market_unverified += d.get("kwh_market_unverified", 0.0) or 0.0
        skipped_market.update(d.get("instruments_skipped_market", []) or [])

    # Surface activities whose scope was ASSUMED (unrecognised category -> Scope 3)
    # from the frozen line lineage, so a silent mis-scoping of purchased energy
    # (Scope 2) or a fugitive source (Scope 1) is visible to the report consumer.
    scope_assumed = {}
    for details, cat in db.query(li.details, ActivityRecord.category)\
            .join(ActivityRecord, li.activity_id == ActivityRecord.id)\
            .filter(li.run_id == run.id, li.method == "location").all():
        if (json.loads(details or "{}")).get("scope_source") == "assumed_scope3":
            scope_assumed[cat or "?"] = scope_assumed.get(cat or "?", 0) + 1

    return {
        "run": {
            "id": run.id,
            "created_at": run.created_at,
            "gwp_set": run.gwp_set,
            "organisation_id": run.organisation_id,
            "reporting_period_id": run.reporting_period_id,
            "status": run.status,
        },
        "scope_assumptions": ({
            "assumed_scope3_by_category": scope_assumed,
            "note": "These categories were unrecognised and defaulted to Scope 3 — "
                    "verify none are purchased energy (Scope 2) or direct/fugitive "
                    "(Scope 1) before relying on the scope split.",
        } if scope_assumed else None),
        "total_co2e": run.total_co2e,                     # location-based (headline, activity-derived)
        "total_co2e_market": run.total_co2e_market,       # dual reporting counterpart
        # DISCLOSED total incl. Scope 3 Cat 15 financed emissions (PCAF), when
        # evaluated. total_co2e itself is never changed (positions are a live ledger).
        "financed_co2e": run.financed_co2e,
        "total_co2e_incl_financed_kg": ((run.total_co2e or 0.0) + run.financed_co2e
                                        if run.financed_co2e is not None else None),
        # ISO 14067: biogenic CO2 reported separately, never netted into the above.
        "biogenic_co2e_separate": run.total_biogenic_co2e or 0.0,
        # GHG Protocol Land Sector & Removals: the org's own removals, reported
        # SEPARATELY. total_co2e (gross) stays the headline; net is derived, never
        # stored. None when the dimension was not evaluated (distinct from 0.0).
        "removals_co2e_separate": run.total_removals_co2e,
        "removals_reversed_co2e": run.removals_reversed_co2e,
        "net_removals_co2e": ((run.total_removals_co2e - (run.removals_reversed_co2e or 0.0))
                              if run.total_removals_co2e is not None else None),
        "net_co2e_after_removals_kg": (
            (run.total_co2e or 0.0) - (run.total_removals_co2e - (run.removals_reversed_co2e or 0.0))
            if run.total_removals_co2e is not None else None),
        "by_scope": [{"scope": s or "?", "co2e": v or 0.0} for s, v in by_scope],
        "by_category": [{"category": c or "?", "co2e": v or 0.0} for c, v in by_cat],
        # GHG Protocol Scope 2 Guidance: dual reporting, both bases side by side.
        "scope2": {
            "location_based": scope2_location,
            "market_based": scope2_market,
            "market_line_items": len(market_lines),
            "market_bases": bases,
            "kwh_contractual": kwh_contractual,
            "kwh_grid_fallback": kwh_grid_fallback,
            # Contractual kWh applied without a verified market match (instrument or
            # consumption had no declared market) — a Scope 2 Guidance quality caveat.
            "kwh_market_unverified": kwh_market_unverified,
            "instruments_excluded_by_market": sorted(skipped_market),
        },
        "method_split": {
            "co2e_by_method": method_split,
            "primary_data_share_pct": round(100.0 * primary_kg / total_methods, 2)
                                      if total_methods else 0.0,
            "spend_based_share_pct": round(100.0 * method_split.get("spend_based", 0.0)
                                           / total_methods, 2) if total_methods else 0.0,
        },
        # GHG Protocol Ch.3 organisational boundary, from the run's FROZEN snapshot.
        "consolidation": _consolidation(db, run),
        "data_quality": _data_quality(db, run, li),
        # GHG Protocol Scope 3 by the 15 categories, from frozen lineage.
        "scope3_ghgp": _scope3_ghgp(db, run),
        # A partial run cannot honestly answer the question asked of it — flag it
        # at the TOP level, not only inside the nested coverage block.
        "partial": (run.mapped or 0) < (run.total_activities or 0),
        "partial_reasons": {
            k: v for k, v in {
                "unmapped": run.unmapped, "unit_errors": run.unit_errors,
                "data_errors": run.data_errors, "gwp_mismatch": run.gwp_mismatch,
            }.items() if v
        },
        "coverage": coverage(db, run),
        # Per-activity exclusion reasons captured at compute time (assurer lineage).
        "exclusions": json.loads(run.notes or "[]"),
        "notes": "Quantities are unit-converted to factor units; incompatible units are "
                 "rejected (not guessed). Scope 2 is dual-reported (location + market).",
    }


def _consolidation(db: Session, run: CalculationRun) -> dict:
    """The run's frozen GHGP Ch.3 boundary + the S2 29(a)(iv) disaggregation.

    Built ONCE here so every renderer reads the same numbers. Reads only frozen state.
    """
    from ..models import RunEntityBoundary
    from ..services.boundary import boundary_completeness
    if not run.boundary_version:
        return {"assessable": False,
                "note": "This run predates the GHGP organisational-boundary dimension — "
                        "recompute. It is deliberately NOT rendered as a clean "
                        "'operational_control, 100%' claim it never made."}
    rows = db.query(RunEntityBoundary).filter(
        RunEntityBoundary.run_id == run.id).order_by(RunEntityBoundary.id).all()
    g = boundary_completeness(db, run)
    # IFRS S2 29(a)(iv): Scope 1 and Scope 2 (location-based) disaggregated between the
    # consolidated accounting group and other investees — a FINANCIAL-statement split,
    # not the GHGP category. The per-scope figures are only trustworthy when the run
    # FROZE them; a run predating this dimension has NULL scope columns, so we fall back
    # to the all-scope figure and flag scope_split_available=False rather than report a
    # silent 0 for Scope 1/2 (fail-closed-on-disclosure).
    scope_split = bool(rows) and all(
        r.scope1_consolidated_co2e is not None for r in rows)
    disagg = {}
    for r in rows:
        b = disagg.setdefault(r.group_class, {
            "consolidated_co2e_kg": 0.0, "scope1_co2e_kg": 0.0,
            "scope2_location_co2e_kg": 0.0, "entities": []})
        b["consolidated_co2e_kg"] += r.consolidated_co2e
        b["scope1_co2e_kg"] += (r.scope1_consolidated_co2e or 0.0)
        b["scope2_location_co2e_kg"] += (r.scope2_consolidated_co2e or 0.0)
        b["entities"].append(r.entity_name)

    def _disagg_bucket(v):
        out = {"consolidated_all_scopes_co2e_kg": round(v["consolidated_co2e_kg"], 6),
               "entities": sorted(v["entities"])}
        if scope_split:
            out["scope1_co2e_kg"] = round(v["scope1_co2e_kg"], 6)
            out["scope2_location_co2e_kg"] = round(v["scope2_location_co2e_kg"], 6)
            out["scope1_2_co2e_kg"] = round(
                v["scope1_co2e_kg"] + v["scope2_location_co2e_kg"], 6)
        return out
    return {
        "assessable": True,
        "approach": run.consolidation_approach,
        "boundary_version": run.boundary_version,
        "reason_for_choice": run.consolidation_reason,
        # Gross emissions the boundary EXCLUDED. Never in total_co2e, and never added
        # to the disclosed total either — it is a different measure, not a missing
        # addend (adding an equity-excluded associate's gross back is exactly the
        # double count Scope 3 Cat 15 exists to avoid).
        "excluded_by_boundary_kg": run.total_co2e_non_consolidated,
        "entities": [{
            "entity_key": r.entity_key, "name": r.entity_name,
            "accounting_category": r.accounting_category,
            "share_factor": r.share_factor, "share_basis": r.share_basis,
            "resolved": r.resolved, "group_class": r.group_class,
            "gross_co2e_kg": round(r.gross_co2e, 6),
            "consolidated_co2e_kg": round(r.consolidated_co2e, 6),
            "line_count": r.line_count,
        } for r in rows],
        "disaggregation_by_accounting_group": {
            k: _disagg_bucket(v) for k, v in disagg.items()},
        # IFRS S2 ¶29(a)(iv) is a Scope 1 / Scope 2 split; True only when the run froze
        # the per-scope figures. Legacy runs expose all-scope totals with this False.
        "disaggregation_scope_split_available": scope_split,
        "disaggregation_basis": (
            "scope1_and_scope2_location_ifrs_s2_29a_iv" if scope_split
            else "all_scopes_only_run_predates_per_scope_freeze"),
        "blockers": g.get("blockers", []),
        "warnings": g.get("warnings", []),
        "note": "Each entity's emissions enter the inventory at its share under the "
                "declared approach. gross -> share -> consolidated is frozen per entity; "
                "the excluded residual is measured, not re-routed (declare the Scope 3 "
                "category for excluded operations).",
    }


def _inventory_coverage(db: Session, run: CalculationRun) -> dict:
    """Coverage of the VALUE CHAIN (the 15 GHGP Scope 3 categories) — orthogonal to
    coverage_pct, which is coverage of the activity rows the user uploaded."""
    from ..services.ghgp import scope3_completeness
    g = scope3_completeness(db, run)
    if not g.get("assessable"):
        return {"basis": "ghgp_scope3_15_categories", "assessable": False,
                "note": "Legacy run — recompute to assess value-chain completeness."}
    st = g["by_status"]
    return {
        "basis": "ghgp_scope3_15_categories",
        "standard_version": run.ghgp_standard_version,
        "assessable": True,
        "categories_total": 15,
        "categories_included": st["included"],
        "categories_not_applicable": st["not_applicable"],
        "categories_not_material": st["not_material"],
        "categories_not_measured": st["not_measured"],
        "categories_undeclared": st["undeclared"],
        "categories_accounted_for": g["categories_accounted_for"],
        "inventory_coverage_pct": g["inventory_coverage_pct"],
        "unassigned_scope3_sources": g["unassigned_sources"],
        "note": "Coverage of the 15 GHG Protocol Scope 3 categories. Orthogonal to "
                "coverage_pct: a firm uploading only electricity/gas/flights has 100% "
                "mapping coverage and ~7% inventory coverage.",
    }


def _scope3_ghgp(db: Session, run: CalculationRun) -> dict:
    # Imported lazily: scope3.py reads _resolve_run from this module.
    from .scope3 import scope3_by_ghgp_category
    return scope3_by_ghgp_category(db, run)


def _data_quality(db: Session, run: CalculationRun, li):
    """Portfolio data-quality: emissions-weighted score, rating mix, and an
    approximate emissions-weighted 95% uncertainty band (pedigree lognormal).

    Read from frozen per-line detail so a re-map cannot relabel the run's DQ.
    The band is a weighted mean of per-line CI multipliers — an approximation,
    not full lognormal propagation — and is labelled as such.
    """
    rows = db.query(li.details, li.co2e)\
        .filter(li.run_id == run.id, li.method == "location").all()
    total = 0.0
    by_rating = {"high": 0.0, "medium": 0.0, "low": 0.0}
    lo_w = hi_w = 0.0
    for details, co2e in rows:
        dq = (json.loads(details or "{}")).get("data_quality")
        if not dq or not co2e:
            continue
        total += co2e
        by_rating[dq.get("rating", "medium")] = by_rating.get(dq.get("rating", "medium"), 0.0) + co2e
        lo_w += co2e * dq.get("ci95_low_mult", 1.0)
        hi_w += co2e * dq.get("ci95_high_mult", 1.0)
    # No emitting lines -> no score. None (not 0.0) so nothing reads as
    # "better than the best possible 1.0" on the 1..5 scale.
    has_data = total > 0
    return {
        "has_data": has_data,
        "emissions_weighted_score": run.data_quality_score if has_data else None,
        "scale": "1 best .. 5 worst (ecoinvent pedigree)",
        "co2e_by_rating": {k: round(v, 4) for k, v in by_rating.items()},
        "approx_ci95_low": round(lo_w, 4) if has_data else None,
        "approx_ci95_high": round(hi_w, 4) if has_data else None,
        "uncertainty_note": "Approximate emissions-weighted 95% band (pedigree "
                            "lognormal), assuming FULLY CORRELATED line errors: "
                            "the relative band does not narrow as the portfolio "
                            "grows (conservative vs independent-error Monte Carlo).",
    }


def run_factor_sources(db: Session, run: CalculationRun) -> list:
    """Factor sources/versions used by a run, from FROZEN line lineage.

    Joining through the live ``ActivityRecord.factor_id`` would let a post-run
    re-map (or un-map) silently rewrite an immutable run's methodology
    statement — the factor ids must come from the line details captured at
    compute time.
    """
    ids = set()
    for (details,) in db.query(EmissionLineItem.details)\
            .filter(EmissionLineItem.run_id == run.id,
                    EmissionLineItem.method == "location").all():
        fid = json.loads(details or "{}").get("factor_id")
        if fid:
            ids.add(fid)
    if not ids:
        return []
    rows = db.query(EmissionFactor.source, EmissionFactor.version)\
        .filter(EmissionFactor.id.in_(ids)).distinct().all()
    return sorted(f"{src} v{ver}" for src, ver in rows)


def scope3_by_category(db: Session, run: CalculationRun) -> dict:
    """Scope 3 kg CO2e by activity category, filtered by the line items' FROZEN
    scope — never by category-name heuristics (a preset scope or a new
    non-carrier Scope-1 category would silently misattribute otherwise)."""
    li = EmissionLineItem
    rows = db.query(ActivityRecord.category, func.sum(li.co2e))\
        .join(li, li.activity_id == ActivityRecord.id)\
        .filter(li.run_id == run.id, li.method == "location", li.scope == "3")\
        .group_by(ActivityRecord.category).all()
    return {(c or "?"): (v or 0.0) for c, v in rows}


def coverage(db: Session, run: CalculationRun):
    """Completeness of a run's total, read from the run's FROZEN snapshot.

    Because the counters were fixed at compute time, this can never
    self-contradict later re-mapping (the failure mode a live-derived metric had).
    Staleness — new activities added to the org since the run — is surfaced
    explicitly instead. ``coverage_pct`` is COUNT-based, not emissions-weighted
    (that lands in Phase 2b); the largest unmapped activities are surfaced so a
    few big gaps can't hide behind a high count-based percentage (Gap 4).
    """
    n_total = run.total_activities or 0
    n_calc = run.mapped or 0
    uncovered = n_total - n_calc

    # Current org state, for diagnostics + staleness.
    n_unmapped_now = db.query(func.count(ActivityRecord.id))\
        .filter(ActivityRecord.organisation_id == run.organisation_id,
                ActivityRecord.factor_id.is_(None)).scalar() or 0
    # Compare like with like: a PERIOD-scoped run's fingerprint was taken over the
    # in-period activities, so it must be re-checked against the same filtered set.
    # (Comparing against the org's whole activity list made every period run — the
    # normal annual-inventory case — perpetually STALE.)
    period = (db.get(ReportingPeriod, run.reporting_period_id)
              if run.reporting_period_id else None)
    acts_now = activities_in_scope(db, run.organisation_id, period)
    n_activities_now = len(acts_now)

    # Content fingerprint (not just count): catches re-mapping / edits at equal count.
    # A run stamped under an older fingerprint scheme can't be compared, so report
    # "not assessable" rather than falsely STALE.
    stored_fp = run.activities_fingerprint or ""
    staleness_assessable = stored_fp.startswith(f"{FINGERPRINT_VERSION}:")
    stale = (activities_fingerprint(acts_now) != stored_fp) if staleness_assessable else False

    # Factor drift: the run froze each line's factor value, so an IN-PLACE edit to a
    # factor (which should never happen — supersede instead) means the run no longer
    # reproduces from the current catalog. Detect it rather than silently diverge.
    factor_drift = []
    frozen = {}
    for (details,) in db.query(EmissionLineItem.details)\
            .filter(EmissionLineItem.run_id == run.id,
                    EmissionLineItem.method == "location").all():
        d = json.loads(details or "{}")
        fid, fval = d.get("factor_id"), d.get("factor_value")
        if fid is not None and fval is not None:
            frozen[fid] = fval
    if frozen:
        for f in db.query(EmissionFactor).filter(EmissionFactor.id.in_(frozen.keys())).all():
            if f.value is not None and f.value != frozen[f.id]:
                factor_drift.append(
                    f"factor {f.id} ({f.source} v{f.version}) value changed in place since "
                    f"this run ({frozen[f.id]} -> {f.value}) — the run's figures no longer "
                    f"reproduce from the current catalog; supersede factors, never edit them")

    unmapped_by_cat = db.query(ActivityRecord.category, func.count(ActivityRecord.id))\
        .filter(ActivityRecord.organisation_id == run.organisation_id,
                ActivityRecord.factor_id.is_(None))\
        .group_by(ActivityRecord.category).all()

    largest_unmapped = db.query(
        ActivityRecord.category, ActivityRecord.quantity, ActivityRecord.unit)\
        .filter(ActivityRecord.organisation_id == run.organisation_id,
                ActivityRecord.factor_id.is_(None), ActivityRecord.quantity.isnot(None))\
        .order_by(ActivityRecord.quantity.desc()).limit(5).all()

    warnings = []
    if uncovered:
        warnings.append(f"{uncovered} activities EXCLUDED from total_co2e (footprint understated).")
    if stale:
        warnings.append(f"Run is STALE: the activity set changed since this run "
                        f"(now {n_activities_now} activities vs {n_total} at run time, "
                        f"or an activity was re-mapped/edited) — re-run /calculate/run.")
    if not staleness_assessable:
        warnings.append("Run predates the current fingerprint scheme — staleness cannot be "
                        "assessed; recompute to enable reproducibility checking.")
    warnings.extend(factor_drift)

    return {
        "activities_total": n_total,
        "activities_calculated": n_calc,
        "activities_uncovered": uncovered,
        "staleness_assessable": staleness_assessable,
        "factor_drift": factor_drift,
        "period_scoped": run.reporting_period_id is not None,
        "unit_errors": run.unit_errors,
        "data_errors": run.data_errors,
        "gwp_mismatch": run.gwp_mismatch,
        "activities_unmapped_now": n_unmapped_now,
        "stale": stale,
        "coverage_pct": round(100.0 * n_calc / n_total, 2) if n_total else 0.0,
        "coverage_basis": "activity_count",
        # Naming the LIMIT of this number is the point: it is coverage of the rows the
        # user uploaded, NOT coverage of the value chain. A firm uploading only
        # electricity/gas/flights has 100% mapping coverage and ~7% inventory coverage.
        "coverage_scope": "uploaded_activities_only — NOT value-chain completeness; "
                          "see inventory_coverage",
        "coverage_caveat": "Count-based, NOT emissions-weighted; see largest_unmapped. "
                           "Emissions-weighted coverage is planned (analytics phase).",
        "inventory_coverage": _inventory_coverage(db, run),
        "unmapped_by_category": {c or "?": n for c, n in unmapped_by_cat},
        "largest_unmapped": [
            {"category": c or "?", "quantity": q, "unit": u} for c, q, u in largest_unmapped
        ],
        "warning": " ".join(warnings) if warnings else None,
    }
