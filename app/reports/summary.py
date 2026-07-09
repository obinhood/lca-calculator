import json
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import func
from ..models import ActivityRecord, CalculationRun, EmissionLineItem
from ..services.calc import activities_fingerprint


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

    scope2_location = next((v for s, v in by_scope if s == "2"), 0.0) or 0.0
    scope2_market = db.query(func.sum(li.co2e))\
        .filter(li.run_id == run.id, li.method == "market").scalar() or 0.0
    n_market_lines = db.query(func.count(li.id))\
        .filter(li.run_id == run.id, li.method == "market").scalar() or 0

    return {
        "run": {
            "id": run.id,
            "created_at": run.created_at,
            "gwp_set": run.gwp_set,
            "organisation_id": run.organisation_id,
            "reporting_period_id": run.reporting_period_id,
            "status": run.status,
        },
        "total_co2e": run.total_co2e,                     # location-based (headline)
        "total_co2e_market": run.total_co2e_market,       # dual reporting counterpart
        "by_scope": [{"scope": s or "?", "co2e": v or 0.0} for s, v in by_scope],
        "by_category": [{"category": c or "?", "co2e": v or 0.0} for c, v in by_cat],
        # GHG Protocol Scope 2 Guidance: dual reporting, both bases side by side.
        "scope2": {
            "location_based": scope2_location,
            "market_based": scope2_market,
            "market_line_items": n_market_lines,
        },
        "coverage": coverage(db, run),
        # Per-activity exclusion reasons captured at compute time (assurer lineage).
        "exclusions": json.loads(run.notes or "[]"),
        "notes": "Quantities are unit-converted to factor units; incompatible units are "
                 "rejected (not guessed). Scope 2 is dual-reported (location + market).",
    }


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
    acts_now = db.query(ActivityRecord)\
        .filter(ActivityRecord.organisation_id == run.organisation_id).all()
    n_activities_now = len(acts_now)
    # Content fingerprint (not just count): catches re-mapping / edits at equal count.
    stale = activities_fingerprint(acts_now) != (run.activities_fingerprint or "")

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

    return {
        "activities_total": n_total,
        "activities_calculated": n_calc,
        "activities_uncovered": uncovered,
        "unit_errors": run.unit_errors,
        "data_errors": run.data_errors,
        "gwp_mismatch": run.gwp_mismatch,
        "activities_unmapped_now": n_unmapped_now,
        "stale": stale,
        "coverage_pct": round(100.0 * n_calc / n_total, 2) if n_total else 0.0,
        "coverage_basis": "activity_count",
        "coverage_caveat": "Count-based, NOT emissions-weighted; see largest_unmapped. "
                           "Emissions-weighted coverage lands in Phase 2b.",
        "unmapped_by_category": {c or "?": n for c, n in unmapped_by_cat},
        "largest_unmapped": [
            {"category": c or "?", "quantity": q, "unit": u} for c, q, u in largest_unmapped
        ],
        "warning": " ".join(warnings) if warnings else None,
    }
