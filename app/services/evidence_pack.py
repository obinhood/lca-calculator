"""The assurance evidence pack: one frozen, hash-stamped bundle per run.

An assuror does not receive a dashboard. They receive a working-paper file, and
until now everything in that file existed in this platform but scattered across a
dozen endpoints — the inventory statement here, the boundary there, factor lineage
inside a per-line JSON blob, exclusions in `run.notes`. This assembles them into
the artefact that is actually handed over, and stamps it so the handover is
provable.

REPRODUCTION CONTRACT: every section reads only what the run froze. Re-generating
a pack for a filed run years later yields the same `content_hash` even after
activities are re-mapped, factors corrected or the organisation renamed. The one
field deliberately outside the hash is `generated_at` — a timestamp that changed
the hash would make the pack unverifiable by construction.

WHAT IT DOES NOT CONTAIN is as important as what it does, and is stated in
`evidence_gaps` rather than left for the assuror to discover. The platform's
auth model is an organisation-scoped API key with no concept of a person, so
"reviewer identity" cannot be produced at all; the mapping override endpoint
overwrites `factor_id` without journalling the prior value, so there is no
before/after override log; and no field on ActivityRecord carries a GL account or
cost centre, so no reconciliation to a trial balance is possible. Each is named
with what would be needed to close it. An evidence pack that quietly omitted them
would be worse than useless — it would look complete.
"""
import hashlib
import json
from typing import Optional

from sqlalchemy.orm import Session

from ..models import (
    ActivityRecord, CalculationRun, EmissionFactor, EmissionLineItem,
    Organisation, ReportingPeriod, RunEntityBoundary,
)

# Bumped when the pack's CONTENT or hashing changes in a way that would alter the
# content_hash of an unchanged run. Frozen into every pack so an old hash stays
# attributable to the assembler that produced it.
PACK_VERSION = "evp-v1"

# Transaction detail is the largest section by far and an inventory can carry tens
# of thousands of lines. It is capped, and a truncated pack SAYS SO in its own
# section rather than presenting a partial ledger as the whole one.
DEFAULT_MAX_LINES = 5_000


def _detail(raw) -> dict:
    """Parse a frozen line-detail blob; anything unusable yields {} rather than raising.

    Coerces non-object JSON too: `[]` and `null` are valid JSON but not a detail
    record, and returning them would hand a list to callers doing `.get()`.
    """
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _s1_inventory_statement(db: Session, run: CalculationRun) -> dict:
    """The figures being assured, on the bases the run actually computed."""
    return {
        "run_id": run.id,
        "status": run.status,
        "computed_at": run.created_at,
        "gwp_set": run.gwp_set,
        "total_co2e_kg_location_based": run.total_co2e,
        "total_co2e_kg_market_based": run.total_co2e_market,
        "biogenic_co2e_kg_reported_separately": run.total_biogenic_co2e,
        "total_co2e_kg_non_consolidated": run.total_co2e_non_consolidated,
        "note": "Biogenic CO2 is a separate pool under ISO 14067 and is never netted "
                "into the totals above.",
    }


def _s2_reporting_period(db: Session, run: CalculationRun) -> dict:
    if run.reporting_period_id is None:
        return {"period_scoped": False,
                "note": "This run is not period-scoped: it covers every activity held "
                        "for the organisation, not a declared reporting period."}
    p = db.query(ReportingPeriod).filter(
        ReportingPeriod.id == run.reporting_period_id).first()
    if p is None:
        return {"period_scoped": True, "resolved": False,
                "reporting_period_id": run.reporting_period_id,
                "note": "The run references a reporting period that no longer exists."}
    return {"period_scoped": True, "resolved": True, "id": p.id,
            "label": p.label, "start_date": p.start_date, "end_date": p.end_date,
            "frozen": bool(p.frozen)}


def _s3_organisational_boundary(db: Session, run: CalculationRun) -> dict:
    """The entity population and every weight, exactly as frozen onto the run.

    Includes entities weighted 0.0 and entities that contributed nothing: those
    rows ARE the "excluded from the consolidation, and why" list an assuror asks
    for, and dropping them would turn a complete population into an assertion.
    """
    rows = (db.query(RunEntityBoundary)
            .filter(RunEntityBoundary.run_id == run.id)
            .order_by(RunEntityBoundary.id).all())
    return {
        "entity_count": len(rows),
        "consolidation_approach": rows[0].approach if rows else None,
        "boundary_version": rows[0].boundary_version if rows else None,
        "frozen_at": rows[0].frozen_at if rows else None,
        "entities": [{
            "entity_key": r.entity_key,
            "entity_id": r.entity_id,
            "entity_name": r.entity_name,
            "entity_ref": r.entity_ref,
            "accounting_category": r.accounting_category,
            "group_class": r.group_class,
            "share_factor": r.share_factor,
            "share_basis": r.share_basis,
            "share_resolved": r.resolved,
            "line_count": r.line_count,
            "equity_share_pct": r.equity_share_pct,
            "financial_control": r.financial_control,
            "operational_control": r.operational_control,
            "in_consolidated_accounting_group": r.in_consolidated_accounting_group,
            "control_rationale": r.control_rationale,
            "effective_from": r.effective_from,
            "effective_to": r.effective_to,
        } for r in rows],
        "note": "Rows weighted 0.0 or carrying no activity are retained deliberately: "
                "they are the excluded-entity population, not padding.",
    }


def _s4_transaction_detail(db: Session, run: CalculationRun, max_lines: int) -> dict:
    """Every emission line joined to its source record and its frozen factor lineage."""
    q = (db.query(EmissionLineItem, ActivityRecord)
         .join(ActivityRecord, ActivityRecord.id == EmissionLineItem.activity_id)
         .filter(EmissionLineItem.run_id == run.id)
         .order_by(EmissionLineItem.id))
    total = q.count()
    rows = q.limit(max_lines).all()

    lines = []
    for li, a in rows:
        d = _detail(li.details)
        dq = d.get("data_quality") or {}
        lines.append({
            "line_id": li.id,
            "scope": li.scope,
            "method": li.method,
            "co2e_kg": li.co2e,
            "source_record": {
                "activity_id": a.id, "date": a.date,
                "coverage_start": a.coverage_start, "coverage_end": a.coverage_end,
                "category": a.category, "subcategory": a.subcategory,
                "description": a.description,
                "quantity_as_recorded": a.quantity, "unit": a.unit, "geo": a.geo,
                "source_file": a.source_file, "upload_hash": a.upload_hash,
            },
            "factor_lineage": {
                "factor_id": d.get("factor_id"),
                "factor_unit": d.get("factor_unit"),
                "activity_unit": d.get("activity_unit"),
                "quantity_used": d.get("quantity"),
                "calc_method": d.get("calc_method"),
                "method_type": d.get("method_type"),
                "lca_boundary": d.get("lca_boundary"),
                "gwp_set_applied": d.get("gwp_set_applied") or d.get("gwp_set"),
                "gwp_values": d.get("gwp_values"),
                "gases_kg_per_unit": d.get("gases_kg_per_unit"),
                "factor_value": d.get("factor_value"),
                "temporal_proration": d.get("temporal_proration"),
                "spend_normalization": d.get("spend_normalization"),
            },
            "consolidation": d.get("consolidation"),
            "data_quality": {
                "overall": dq.get("overall"), "rating": dq.get("rating"),
                "indicators": dq.get("indicators"), "sigma_log": dq.get("sigma_log"),
            },
        })

    out = {"line_count_total": total, "line_count_included": len(lines),
           "truncated": total > len(lines), "lines": lines}
    if out["truncated"]:
        out["truncation_note"] = (
            f"{total - len(lines)} of {total} lines are omitted from this section at the "
            f"{max_lines}-line cap. The inventory statement, factor register and "
            f"completeness controls still describe ALL {total} lines — only this "
            f"line-by-line listing is bounded. Raise max_lines to obtain the full ledger.")
    return out


def _s5_factor_register(db: Session, run: CalculationRun) -> dict:
    """The distinct factors this run used, resolved from FROZEN line lineage.

    Deliberately NOT joined through ActivityRecord.factor_id: a later re-map would
    otherwise rewrite an immutable run's factor register.
    """
    rows = db.query(EmissionLineItem.details).filter(
        EmissionLineItem.run_id == run.id).all()
    counts = {}
    for (d,) in rows:
        fid = _detail(d).get("factor_id")
        if fid is not None:
            counts[fid] = counts.get(fid, 0) + 1

    register, unresolved = [], []
    for fid in sorted(counts):
        f = db.get(EmissionFactor, fid)
        if f is None:
            unresolved.append({"factor_id": fid, "lines": counts[fid]})
            continue
        register.append({
            "factor_id": f.id, "lines_using": counts[fid],
            "source": f.source, "version": f.version, "vintage_year": f.year,
            "geography": f.geography, "category": f.category,
            "subcategory": f.subcategory, "unit": f.unit,
            "gwp_set": f.gwp_set, "value": f.value,
            "method_type": f.method_type, "lca_boundary": f.lca_boundary,
            "supersedes_id": getattr(f, "supersedes_id", None),
        })
    out = {"distinct_factors": len(counts), "factors": register}
    if unresolved:
        out["unresolved"] = unresolved
        out["unresolved_note"] = (
            "These factor ids are frozen on the run's lines but no longer exist in the "
            "catalogue. The run's figures are unaffected — they were computed from the "
            "frozen values — but the register cannot restate the factor's metadata.")
    return out


def _s6_mapping_decisions(db: Session, run: CalculationRun, max_lines: int) -> dict:
    """How each activity came to be bound to its factor: auto, approved or overridden."""
    ids = [r[0] for r in db.query(EmissionLineItem.activity_id).filter(
        EmissionLineItem.run_id == run.id).distinct().all()]
    acts = (db.query(ActivityRecord)
            .filter(ActivityRecord.id.in_(ids))
            .order_by(ActivityRecord.id).limit(max_lines).all()) if ids else []

    by_status = {}
    for a in acts:
        by_status[a.mapping_status or "unmapped"] = \
            by_status.get(a.mapping_status or "unmapped", 0) + 1

    return {
        "activities_in_run": len(ids),
        "activities_listed": len(acts),
        "truncated": len(ids) > len(acts),
        "by_status": by_status,
        "human_decisions": sum(v for k, v in by_status.items()
                               if k in ("approved", "overridden")),
        "decisions": [{
            "activity_id": a.id,
            "mapping_status": a.mapping_status,
            "mapping_basis": a.mapping_basis,
            "mapping_confidence": a.mapping_confidence,
            "bound_factor_id": a.factor_id,
            "suggested_factor_id": a.suggested_factor_id,
        } for a in acts],
        "note": "mapping_status/basis are read LIVE from the activity, not frozen onto "
                "the run, so a re-map after this run changes this section while the "
                "run's figures stay fixed. The frozen per-line factor_id in section 4 "
                "is the authoritative record of what was actually calculated.",
    }


def _s7_completeness_controls(db: Session, run: CalculationRun, summary_payload: dict) -> dict:
    """Coverage, every excluded activity with its reason, and the Scope 3 statement."""
    cov = summary_payload.get("coverage") or {}
    try:
        exclusions = json.loads(run.notes or "[]")
    except (ValueError, TypeError):
        exclusions = []
    return {
        "coverage_pct": cov.get("coverage_pct"),
        "coverage_basis": cov.get("coverage_basis"),
        "activities_total": cov.get("activities_total"),
        "activities_calculated": cov.get("activities_calculated"),
        "stale": cov.get("stale"),
        "counters": {"mapped": run.mapped, "unmapped": run.unmapped,
                     "unit_errors": run.unit_errors, "data_errors": run.data_errors,
                     "gwp_mismatch": run.gwp_mismatch},
        "excluded_activities": exclusions,
        "excluded_count": len(exclusions),
        "partial": summary_payload.get("partial"),
        "partial_reasons": summary_payload.get("partial_reasons"),
        "scope3_completeness": (summary_payload.get("scope3_ghgp") or {}).get("completeness"),
        "activities_fingerprint": run.activities_fingerprint,
    }


def _s8_data_quality_and_uncertainty(db: Session, run: CalculationRun,
                                     summary_payload: dict, iterations: int) -> dict:
    from .uncertainty import propagate
    dq = summary_payload.get("data_quality") or {}
    mc = propagate(db, run.id, method="location", iterations=iterations)
    block = {
        "pedigree": {
            "emissions_weighted_score": dq.get("emissions_weighted_score"),
            "scale": dq.get("scale"),
            "co2e_by_rating": dq.get("co2e_by_rating"),
            "approx_ci95_low": dq.get("approx_ci95_low"),
            "approx_ci95_high": dq.get("approx_ci95_high"),
        },
        "primary_data_share_pct": summary_payload.get("primary_data_share_pct"),
        "spend_based_share_pct": summary_payload.get("spend_based_share_pct"),
    }
    if mc.get("available"):
        block["monte_carlo"] = {
            "interval": mc["interval"],
            "correlation": mc["correlation"],
            "correlation_bounds": mc["correlation_bounds"],
            "coverage": mc["coverage"],
            "sensitivity": mc["sensitivity"],
            "reproducibility": mc["reproducibility"],
        }
    else:
        block["monte_carlo"] = {"available": False, "reason": mc.get("reason")}
    return block


def _s9_methodology(db: Session, run: CalculationRun, summary_payload: dict) -> dict:
    return {
        "gwp_set": run.gwp_set,
        "ghgp_standard_version": run.ghgp_standard_version,
        "ghgp_map_version": run.ghgp_map_version,
        "ghgp_boundary_policy_version": run.ghgp_boundary_policy_version,
        "scope3_temporal_basis_version": run.scope3_temporal_basis_version,
        "scope2_residual_mix_version": run.scope2_residual_mix_version,
        "consolidation": summary_payload.get("consolidation"),
        "scope2": summary_payload.get("scope2"),
        "residual_mix": summary_payload.get("residual_mix"),
        "scope_assumptions": summary_payload.get("scope_assumptions"),
        "note": "NULL version sentinels mean the run PREDATES that requirement and its "
                "gate only warned. They are never back-filled — a NULL here is evidence "
                "about the run, not a missing value.",
    }


def _s10_readiness_and_standard(db: Session, run: CalculationRun) -> dict:
    from .assurance import readiness_assessment
    r = readiness_assessment(db, run)
    return {"ready": r["ready"], "checks": r["checks"],
            "applicable_standard": r.get("assurance_standard"),
            "note": r["note"]}


# Assembled in a fixed order so the hash cannot move with dict iteration order.
_SECTION_ORDER = (
    "1_inventory_statement", "2_reporting_period", "3_organisational_boundary",
    "4_transaction_detail", "5_factor_register", "6_mapping_decisions",
    "7_completeness_controls", "8_data_quality_and_uncertainty",
    "9_methodology", "10_readiness_and_standard",
)


def _evidence_gaps() -> list:
    """What an ISAE 3410 / ISSA 5000 file expects that this platform cannot produce.

    Named explicitly, with the reason and the change that would close each. A pack
    that silently omitted these would read as complete to the one reader who most
    needs to know it is not.
    """
    return [
        {
            "item": "Reviewer identity",
            "expected_by": "ISSA 5000 / ISAE 3410 — who made each judgement",
            "why_absent": "Authentication is an organisation-scoped API key with no "
                          "concept of a person, so no per-user identity exists to "
                          "record. mapping_status proves A human decided, never which.",
            "what_would_close_it": "Per-user authentication with an actor id written "
                                   "onto every mutation.",
        },
        {
            "item": "Override log with before/after values",
            "expected_by": "Audit trail of changes to the mapping",
            "why_absent": "POST /mappings/{id}/override overwrites factor_id in place; "
                          "the prior binding is not journalled, so the previous value "
                          "cannot be recovered.",
            "what_would_close_it": "An append-only mapping audit table capturing "
                                   "(activity, timestamp, action, from_factor, to_factor).",
        },
        {
            "item": "Reviewer timestamp",
            "expected_by": "When each judgement was made",
            "why_absent": "No decision time is stored on the activity — only the "
                          "resulting status.",
            "what_would_close_it": "The same append-only mapping audit table.",
        },
        {
            "item": "GL account and cost centre per transaction",
            "expected_by": "Reconciliation of the inventory to the financial ledger",
            "why_absent": "ActivityRecord carries no financial coding fields; ingestion "
                          "is CSV with a fixed canonical schema.",
            "what_would_close_it": "Financial coding columns on the activity plus an "
                                   "ingestion path that populates them.",
        },
        {
            "item": "Reconciliation to trial balance",
            "expected_by": "Tying spend-based activity to the accounts it came from",
            "why_absent": "Follows from the above — with no GL coding there is nothing "
                          "to reconcile against.",
            "what_would_close_it": "GL coding, plus a ledger import to reconcile to.",
        },
        {
            "item": "Restatement log",
            "expected_by": "Prior-period adjustments and their triggers",
            "why_absent": "Base-year recalculation and boundary difference are computed "
                          "ON DEMAND between two runs (services/boundary.py) rather than "
                          "journalled as events, so there is no standing log to attach. "
                          "Compare any two runs to obtain the same information.",
            "what_would_close_it": "Persisting each recalculation verdict as an event "
                                   "when it is first determined.",
        },
    ]


def _content_hash(sections: dict, gaps: list, run_id: int) -> str:
    """SHA-256 over the pack's CONTENT — never over its generation time.

    `generated_at` is excluded deliberately: a hash that moved every time the pack
    was rendered could not verify anything, which is the whole purpose of stamping it.
    """
    payload = {"v": PACK_VERSION, "run": run_id,
               "sections": {k: sections[k] for k in _SECTION_ORDER if k in sections},
               "evidence_gaps": gaps}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def build_evidence_pack(db: Session, run: CalculationRun, *,
                        max_lines: int = DEFAULT_MAX_LINES,
                        uncertainty_iterations: int = 10_000) -> dict:
    """The full working-paper file for one immutable run, hash-stamped.

    Deterministic for a given run: two calls return the same `content_hash`, and so
    will a call made years later, because every section reads only frozen state.
    """
    from ..reports.summary import summary
    from .calc import _utcnow_iso

    org = db.get(Organisation, run.organisation_id)
    summary_payload = summary(db, organisation_id=run.organisation_id, run_id=run.id)

    sections = {
        "1_inventory_statement": _s1_inventory_statement(db, run),
        "2_reporting_period": _s2_reporting_period(db, run),
        "3_organisational_boundary": _s3_organisational_boundary(db, run),
        "4_transaction_detail": _s4_transaction_detail(db, run, max_lines),
        "5_factor_register": _s5_factor_register(db, run),
        "6_mapping_decisions": _s6_mapping_decisions(db, run, max_lines),
        "7_completeness_controls": _s7_completeness_controls(db, run, summary_payload),
        "8_data_quality_and_uncertainty": _s8_data_quality_and_uncertainty(
            db, run, summary_payload, uncertainty_iterations),
        "9_methodology": _s9_methodology(db, run, summary_payload),
        "10_readiness_and_standard": _s10_readiness_and_standard(db, run),
    }
    gaps = _evidence_gaps()

    return {
        "pack": {
            "pack_version": PACK_VERSION,
            "run_id": run.id,
            "organisation": {"id": org.id if org else run.organisation_id,
                             "name": org.name if org else None},
            "content_hash": _content_hash(sections, gaps, run.id),
            "generated_at": _utcnow_iso(),
            "hash_note": "content_hash covers every section and the evidence-gap list. "
                         "generated_at is EXCLUDED — a hash that moved on every render "
                         "could verify nothing.",
            "section_order": list(_SECTION_ORDER),
        },
        "sections": sections,
        "evidence_gaps": gaps,
        "note": "Assembled from frozen run state only. This is the evidence an assuror "
                "works from; it is not an assurance opinion, and evidence_gaps names "
                "what this platform cannot currently supply.",
    }
