import json
from typing import Optional

from . import derivation as D
from sqlalchemy.orm import Session
from sqlalchemy import func
from ..models import (
    ActivityRecord, CalculationRun, EmissionLineItem, EmissionFactor, ReportingPeriod,
)
from ..services.calc import (
    activities_fingerprint, activities_in_scope, FINGERPRINT_VERSION,
)
from ..services.frozen import parse_detail, parse_list


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


def _residual_mix_block(db: Session, run) -> dict:
    """The run's frozen Scope 2 residual-mix statement, read only from frozen state."""
    from ..services.residual_mix import scope2_residual_mix_completeness
    g = scope2_residual_mix_completeness(db, run)
    return {
        "assessable": g["assessable"], "legacy": g.get("legacy", False),
        "version": g.get("version"),
        "statements": g.get("statements", []),
        "understatement_remaining_consolidated_kg":
            g.get("understatement_remaining_consolidated_kg"),
        "blockers": g.get("blockers", []), "warnings": g.get("warnings", []),
    }


def _cat15_double_declared(db, run) -> bool:
    """True when Cat 15 is declared through BOTH activity lines and a PCAF portfolio.

    The same investee emissions then enter twice — once via the equity-stake activity and
    once via the attribution — so any total combining them is double-counted.

    Matched to `scope3.py`'s gate (`financed_kg > 0 and cats[15]["co2e_kg"] > 0`): only
    Scope 3 lines, only positive amounts, and only runs carrying the GHGP category
    dimension. An earlier version tested none of those, so a ZERO-quantity Cat-15 line
    nulled a CDP filing field. Selects the `details` column alone rather than whole ORM
    entities, and short-circuits — it used to load every line item three times per call.
    """
    from ..models import EmissionLineItem
    if not run.financed_co2e or not run.ghgp_standard_version:
        return False
    rows = db.query(EmissionLineItem.details).filter(
        EmissionLineItem.run_id == run.id,
        EmissionLineItem.method == "location",
        EmissionLineItem.scope == "3",
        EmissionLineItem.co2e > 0).all()
    for (details,) in rows:
        if parse_detail(details).get("ghgp_category") == 15:
            return True
    return False


def _derivations(run, by_scope, scope2_location, scope2_market, method_split,
                 primary_kg, total_methods, kwh_contractual, kwh_residual_mix,
                 kwh_grid_fallback, cat15_double=False) -> dict:
    """Worked calculations for the headline figures.

    The gross total is the load-bearing one: `run.total_co2e` was FROZEN at compute time,
    while `by_scope` re-aggregates the run's line items now. Deriving the frozen total
    from the live re-aggregation therefore does double duty — it shows a reader the
    arithmetic AND fails loudly if the two ever disagree, which would mean the disclosed
    headline no longer matches the lines behind it.
    """
    rows = [{"scope": f"Scope {s or '?'}", "co2e": v or 0.0} for s, v in by_scope]
    out = [
        D.total_of("Gross emissions (location-based)", rows,
                   label_key="scope", value_key="co2e", unit="kgCO2e",
                   stated_value=run.total_co2e, independent=True,
                   basis=f"location-based, {run.gwp_set} GWP-100",
                   note="Cross-checks the total frozen at compute time against the sum "
                        "of this run's line items — a mismatch means the disclosed "
                        "headline has diverged from the lines behind it."),
    ]

    # Scope 2 dual reporting. Deliberately NOT a sum: the Scope 2 Guidance requires both
    # bases side by side, and adding them would double-count the same electricity.
    out.append(D.alternatives(
        "Scope 2 — dual reported",
        [("Location-based", scope2_location), ("Market-based", scope2_market)],
        reported=scope2_location, unit="kgCO2e",
        note="The GHG Protocol Scope 2 Guidance requires BOTH bases to be disclosed. "
             "They are alternative measurements of the same electricity and are never "
             "added together; the gross total above uses the location-based figure."))

    if run.total_removals_co2e is not None:
        net_removals = run.total_removals_co2e - (run.removals_reversed_co2e or 0.0)
        out.append(D.difference(
            "Net removals", [("Removals", run.total_removals_co2e),
                             ("Reversals", run.removals_reversed_co2e or 0.0)],
            unit="kgCO2e", stated_value=net_removals))
        out.append(D.difference(
            "Emissions net of removals",
            [("Gross emissions", run.total_co2e or 0.0), ("Net removals", net_removals)],
            unit="kgCO2e",
            stated_value=(run.total_co2e or 0.0) - net_removals,
            note="Shown for information only. GHG Protocol Land Sector & Removals keeps "
                 "GROSS emissions as the headline — removals are never netted into it."))

    if run.financed_co2e is not None:
        if cat15_double:
            out.append(D.blocked(
                "Disclosed total including financed emissions",
                "Category 15 is declared through BOTH activity-derived lines and a PCAF "
                "portfolio. Adding them counts the same investee emissions twice (GHG "
                "Protocol Scope 3 Standard Ch.9), so no combined total is reported.",
                reported=None, unit="kgCO2e"))
        else:
            out.append(D.total_of(
                "Disclosed total including financed emissions",
                [{"k": "Gross emissions (location-based)", "v": run.total_co2e or 0.0},
                 {"k": "Scope 3 Cat 15 financed (PCAF)", "v": run.financed_co2e}],
                label_key="k", value_key="v", unit="kgCO2e",
                stated_value=(run.total_co2e or 0.0) + run.financed_co2e,
                note="Financed emissions are added for DISCLOSURE only; the inventory "
                     "total itself is unchanged, because positions are a live ledger."))

    if total_methods:
        out.append(D.ratio(
            "Primary-data share", "Supplier-specific + hybrid (kgCO2e)", primary_kg,
            "All methods (kgCO2e)", total_methods, unit="fraction",
            stated_value=primary_kg / total_methods,
            note="Reported as a percentage in method_split; shown here as the underlying "
                 "quotient. Emissions-weighted, not record-weighted."))

    accounted = round(kwh_contractual + kwh_residual_mix + kwh_grid_fallback, 6)
    if accounted:
        out.append(D.total_of(
            "Electricity accounted for", [
                {"k": "Contractual instruments", "v": kwh_contractual},
                {"k": "Residual mix", "v": kwh_residual_mix},
                {"k": "Grid average fallback", "v": kwh_grid_fallback}],
            label_key="k", value_key="v", unit="kWh", stated_value=accounted,
            display_dp=6,
            note="These three must account for the run's whole electricity load; a "
                 "shortfall against consumption means some load is priced at neither a "
                 "contractual rate nor the residual mix."))

    return D.summarise(out)


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

    # Everything below is read from the FROZEN per-line detail, in ONE pass over the
    # run's location lines:
    #   * method split — GHG Protocol Scope 3 method hierarchy: how much of the total
    #     rests on which method (supplier_specific/hybrid = primary-leaning data;
    #     spend_based = lowest tier). Assurers and the Scope 3 revision ask for this.
    #   * by_category and the assumed-scope caveat, keyed on `activity_category`.
    # None of it may join back to the live ActivityRecord: a post-run re-map would
    # relabel this immutable run's method mix, and a post-run CATEGORY EDIT used to move
    # a filed run's emissions between breakdown rows (and rename the row the caveat
    # named) without the run itself changing at all.
    method_split, by_cat_kg, scope_assumed = {}, {}, {}
    _pre_freeze = []          # lines from runs computed before the category freeze

    def _bucket(cat, kg, assumed):
        cat = cat or "?"
        by_cat_kg[cat] = by_cat_kg.get(cat, 0.0) + kg
        if assumed:
            scope_assumed[cat] = scope_assumed.get(cat, 0) + 1

    for details, line_co2e, activity_id in db.query(li.details, li.co2e, li.activity_id)\
            .filter(li.run_id == run.id, li.method == "location").all():
        d = parse_detail(details)
        m = d.get("method_type") or "average_data"
        method_split[m] = method_split.get(m, 0.0) + (line_co2e or 0.0)
        # Surface activities whose scope was ASSUMED (unrecognised category -> Scope 3),
        # so a silent mis-scoping of purchased energy (Scope 2) or a fugitive source
        # (Scope 1) is visible to the report consumer.
        _assumed = d.get("scope_source") == "assumed_scope3"
        # `in`, not a truth test: a frozen NULL category must stay distinguishable from
        # a run that froze no category at all.
        if "activity_category" in d:
            _bucket(d["activity_category"], line_co2e or 0.0, _assumed)
        else:
            _pre_freeze.append((activity_id, line_co2e or 0.0, _assumed))
    if _pre_freeze:
        # A run frozen before `activity_category` existed has no better source than the
        # live activity — the same fallback doctrine as `kwh_contractual_rank0` below.
        # One batched lookup, so the leak is confined to legacy runs instead of every run.
        _live = dict(db.query(ActivityRecord.id, ActivityRecord.category).filter(
            ActivityRecord.id.in_([aid for aid, _, _ in _pre_freeze])).all())
        for aid, kg, assumed in _pre_freeze:
            _bucket(_live.get(aid), kg, assumed)
    by_cat = sorted(by_cat_kg.items())          # deterministic order, was SQL GROUP BY
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
    kwh_residual_mix = 0.0          # priced at the residual mix (neither contractual nor grid)
    kwh_market_unverified = 0.0     # covered by an instrument whose market couldn't be checked
    skipped_market = set()          # instruments excluded by a declared market mismatch
    skipped_vintage = set()         # instruments excluded because their GWP label differs
    for (details,) in market_lines:
        d = parse_detail(details)
        bases[d.get("method_basis", "?")] = bases.get(d.get("method_basis", "?"), 0) + 1
        # Prefer the rank-0-only figure; a run frozen before it existed has no such key
        # and its `kwh_contractual` is the right value for it (it had no residual legs).
        kwh_contractual += (d["kwh_contractual_rank0"] if "kwh_contractual_rank0" in d
                            else (d.get("kwh_contractual", 0.0) or 0.0))
        kwh_grid_fallback += d.get("kwh_grid_fallback", 0.0) or 0.0
        kwh_residual_mix += d.get("kwh_residual_mix", 0.0) or 0.0
        kwh_market_unverified += d.get("kwh_market_unverified", 0.0) or 0.0
        skipped_market.update(d.get("instruments_skipped_market", []) or [])
        skipped_vintage.update(d.get("instruments_skipped_gwp_vintage", []) or [])

    # Computed ONCE: three call sites used to load every line item separately, which was
    # ~a third of summary()'s runtime on a large run, paid by every renderer.
    _cat15_double = _cat15_double_declared(db, run)

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
                    "(Scope 1) before relying on the scope split. Resolve by mapping the "
                    "category to a scope rule and recomputing: the guess is re-derived on "
                    "every run and is never written back onto the activity, so a "
                    "correction always takes effect.",
        } if scope_assumed else None),
        "total_co2e": run.total_co2e,                     # location-based (headline, activity-derived)
        "total_co2e_market": run.total_co2e_market,       # dual reporting counterpart
        # DISCLOSED total incl. Scope 3 Cat 15 financed emissions (PCAF), when
        # evaluated. total_co2e itself is never changed (positions are a live ledger).
        "financed_co2e": run.financed_co2e,
        # None when Category 15 is declared through BOTH activity lines and a PCAF
        # portfolio: the two count the same investee emissions twice, so the sum is not a
        # meaningful number. scope3.py has always refused it (blocker B13) — this payload
        # was adding them anyway, twelve lines from the field that reports the refusal.
        "total_co2e_incl_financed_kg": (
            None if _cat15_double
            else ((run.total_co2e or 0.0) + run.financed_co2e
                  if run.financed_co2e is not None else None)),
        "cat15_double_count_blocked": _cat15_double,
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
            # Pricing the remainder at the residual mix moved it OUT of kwh_grid_fallback
            # without adding it to kwh_contractual, so the disclosed pair stopped
            # accounting for the run's electricity — a reader computing contractual
            # coverage from those two alone saw 100% for an org that covered 60%.
            "kwh_residual_mix": kwh_residual_mix,
            "kwh_electricity_accounted": round(
                kwh_contractual + kwh_residual_mix + kwh_grid_fallback, 6),
            # Contractual kWh applied without a verified market match (instrument or
            # consumption had no declared market) — a Scope 2 Guidance quality caveat.
            "kwh_market_unverified": kwh_market_unverified,
            # Scope 2 Guidance: uncovered load must be priced at the RESIDUAL MIX, not the
            # location grid average. This is the frozen per-(market, year) statement.
            "residual_mix": _residual_mix_block(db, run),
            "instruments_excluded_by_market": sorted(skipped_market),
            # An instrument whose gwp_set LABEL differs from the run's is dropped whole —
            # its covered MWh, not merely its rate. calc froze that exclusion onto every
            # market line and NO renderer read it, so a fully-REC'd org could publish a
            # market-based total identical to its location-based one, and ESRS E1-5 could
            # publish 0% renewable, with nothing anywhere saying why. Surfaced here so
            # every renderer reading scope2 inherits it.
            "instruments_excluded_by_gwp_vintage": sorted(skipped_vintage),
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
        "exclusions": parse_list(run.notes),
        # How each headline figure was arrived at. Built LAST so it checks the payload
        # the report actually publishes, not a parallel recomputation of it.
        "derivations": _derivations(
            run, by_scope, scope2_location, scope2_market, method_split,
            primary_kg, total_methods, kwh_contractual, kwh_residual_mix,
            kwh_grid_fallback, cat15_double=_cat15_double),
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
        dq = parse_detail(details).get("data_quality")
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
        # Two uncertainty figures exist in this platform and a reader must not have
        # to guess how they relate. Under full correlation the total is monotone in
        # the single shared draw, so this closed-form band IS the perfect-correlation
        # Monte Carlo bound — the two agree to sampling noise, and a test locks it.
        # The endpoint additionally reports the by_factor default (lines sharing an
        # emission factor share its error) and the independent lower bound, which
        # this figure deliberately does not attempt.
        "full_propagation": {
            "endpoint": "/runs/{run_id}/uncertainty",
            "relationship": "This band equals that endpoint's 'perfect' correlation "
                            "bound. Its headline 'by_factor' interval is narrower "
                            "and is the one to disclose.",
        },
    }


def run_factor_sources(db: Session, run: CalculationRun) -> list:
    """Factor sources/versions used by a run, from FROZEN line lineage.

    Joining through the live ``ActivityRecord.factor_id`` would let a post-run
    re-map (or un-map) silently rewrite an immutable run's methodology
    statement — the factor ids must come from the line details captured at
    compute time.

    The ids alone were not enough. Taking the id from frozen lineage and then reading
    ``source``/``version`` off the LIVE row still let an in-place edit rewrite a filed
    run's methodology sentence, because those two columns were never frozen. Runs
    computed since carry ``factor_provenance`` and are answered entirely from it.

    Legacy runs predate that block and are NOT blocked or back-filled (a NULL sentinel
    is evidence about the run, not a missing value). They fall back to the live join and
    say so in the returned string, because a reader cannot otherwise tell a provenance
    that is guaranteed from one that merely has not been edited yet.
    """
    frozen_labels, unfrozen_ids = set(), set()
    for (details,) in db.query(EmissionLineItem.details)\
            .filter(EmissionLineItem.run_id == run.id,
                    EmissionLineItem.method == "location").all():
        d = parse_detail(details)
        fid = d.get("factor_id")
        if not fid:
            continue
        prov = d.get("factor_provenance")
        if isinstance(prov, dict) and prov.get("source"):
            frozen_labels.add(f"{prov['source']} v{prov.get('version')}")
        else:
            unfrozen_ids.add(fid)
    live_labels = set()
    if unfrozen_ids:
        rows = db.query(EmissionFactor.source, EmissionFactor.version)\
            .filter(EmissionFactor.id.in_(unfrozen_ids)).distinct().all()
        live_labels = {
            f"{src} v{ver} (read from the current catalog: this run predates frozen "
            f"factor provenance, so an in-place edit since the run would not show here)"
            for src, ver in rows}
    return sorted(frozen_labels | live_labels)


def scope3_by_category(db: Session, run: CalculationRun) -> dict:
    """Scope 3 kg CO2e by activity category, filtered by the line items' FROZEN
    scope — never by category-name heuristics (a declared scope or a new
    non-carrier Scope-1 category would silently misattribute otherwise).

    The category is the FROZEN one too, for the same reason `summary()` uses it: a
    post-run category edit must not move a filed run's emissions between rows.
    """
    li = EmissionLineItem
    rows = db.query(li.details, li.co2e, li.activity_id)\
        .filter(li.run_id == run.id, li.method == "location", li.scope == "3").all()
    out: dict = {}
    pre_freeze = []
    for details, co2e, activity_id in rows:
        d = parse_detail(details)
        if "activity_category" in d:
            c = d["activity_category"] or "?"
            out[c] = out.get(c, 0.0) + (co2e or 0.0)
        else:
            pre_freeze.append((activity_id, co2e or 0.0))
    if pre_freeze:
        live = dict(db.query(ActivityRecord.id, ActivityRecord.category).filter(
            ActivityRecord.id.in_([aid for aid, _ in pre_freeze])).all())
        for aid, kg in pre_freeze:
            c = live.get(aid) or "?"
            out[c] = out.get(c, 0.0) + kg
    return out


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
    frozen, frozen_prov = {}, {}
    for (details,) in db.query(EmissionLineItem.details)\
            .filter(EmissionLineItem.run_id == run.id,
                    EmissionLineItem.method == "location").all():
        d = parse_detail(details)
        fid, fval = d.get("factor_id"), d.get("factor_value")
        if fid is not None and fval is not None:
            frozen[fid] = fval
        prov = d.get("factor_provenance")
        if fid is not None and isinstance(prov, dict) and prov.get("source"):
            frozen_prov[fid] = prov
    if frozen or frozen_prov:
        _ids = set(frozen) | set(frozen_prov)
        for f in db.query(EmissionFactor).filter(EmissionFactor.id.in_(_ids)).all():
            if f.id in frozen and f.value is not None and f.value != frozen[f.id]:
                factor_drift.append(
                    f"factor {f.id} ({f.source} v{f.version}) value changed in place since "
                    f"this run ({frozen[f.id]} -> {f.value}) — the run's figures no longer "
                    f"reproduce from the current catalog; supersede factors, never edit them")
            # Identity drift. The value can be untouched while source/version/year/
            # geography are edited underneath a filed run — which changes nothing
            # numerically and everything about the methodology statement and the
            # temporal pedigree score an assuror reads. Comparing only `value` was blind
            # to it. Only checkable for runs that froze provenance; legacy runs have no
            # frozen side to compare against and are silent here rather than accused.
            p = frozen_prov.get(f.id)
            if p:
                for field, was in (("source", p.get("source")), ("version", p.get("version")),
                                   ("year", p.get("year")), ("geography", p.get("geography"))):
                    now = getattr(f, field, None)
                    if was is not None and now != was:
                        factor_drift.append(
                            f"factor {f.id} {field} changed in place since this run "
                            f"({was!r} -> {now!r}) — the value is unchanged, so the totals "
                            f"still reproduce, but the methodology statement and pedigree "
                            f"this run was filed with no longer match the catalog; "
                            f"supersede factors, never edit them")

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
