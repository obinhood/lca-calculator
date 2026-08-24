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

from .frozen import corrupt_details, parse_detail, parse_list
from ..models import (
    ActivityRecord, CalculationRun, EmissionFactor, EmissionLineItem,
    Organisation, ReportingPeriod, RunEntityBoundary,
)

# Bumped when the pack's CONTENT or hashing changes in a way that would alter the
# content_hash of an unchanged run. Frozen into every pack so an old hash stays
# attributable to the assembler that produced it.
PACK_VERSION = "evp-v5"

# Transaction detail is the largest section by far and an inventory can carry tens
# of thousands of lines. It is capped, and a truncated pack SAYS SO in its own
# section rather than presenting a partial ledger as the whole one.
DEFAULT_MAX_LINES = 5_000


# One parser for every frozen blob in the platform — see services/frozen.py.
_detail = parse_detail


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
    exclusions = parse_list(run.notes)
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
        # Frozen blobs parse defensively so one corrupt row cannot take down every
        # report — but failing soft is only defensible when something counts. This
        # is that count: it stops "the pack rendered" being mistaken for "every
        # line was legible".
        "line_detail_integrity": corrupt_details(db, run.id),
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
        # summary nests both under "method_split"; reading them from the top level
        # published null for two figures the run had actually determined — in the file
        # an assuror reads to judge how much of the inventory rests on primary data.
        "primary_data_share_pct": (summary_payload.get("method_split") or {})
                                  .get("primary_data_share_pct"),
        "spend_based_share_pct": (summary_payload.get("method_split") or {})
                                 .get("spend_based_share_pct"),
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


def _s11_screening_register(db: Session, run: CalculationRun) -> dict:
    """The exception register frozen onto the run — the misstatement ledger.

    ISSA 5000 para 161 requires the engagement file to contain "all misstatements
    accumulated during the engagement, other than those that are clearly trivial
    ... and whether they have been corrected". This is that table, pre-built.
    """
    from .screening import completeness
    c = completeness(db, run)
    return {
        "assessable": c["assessable"], "legacy": c["legacy"],
        "blockers": c["blockers"], "warnings": c["warnings"],
        "statement": c["statement"],
        "note": "ISAE 3410 paras 50-56 / ISSA 5000 paras 153-161: accumulate every "
                "non-trivial misstatement, record whether it was corrected or "
                "accepted uncorrected, and evaluate the accumulated uncorrected "
                "effect against materiality. A finding whose effect could not be "
                "quantified is counted separately and never treated as nil.",
    }


def _s12_mapping_audit(db: Session, run: CalculationRun) -> dict:
    """The append-only journal of factor-binding decisions.

    Live-read like section 6 and for the same reason: the journal accumulates
    after the run. What it adds over section 6 is the ability to answer what a
    binding WAS at a moment in time, which an in-place status column cannot.
    """
    from .mapping_audit import history, summary
    return {
        "summary": summary(db, run.organisation_id),
        "events": history(db, run.organisation_id),
        "note": "Read LIVE, not frozen: the journal accumulates after the run. Its "
                "value is that it can answer what an activity was bound to on the "
                "day an opinion was issued — the question an in-place status column "
                "cannot.",
    }


# Assembled in a fixed order so the hash cannot move with dict iteration order.
_SECTION_ORDER = (
    "1_inventory_statement", "2_reporting_period", "3_organisational_boundary",
    "4_transaction_detail", "5_factor_register", "6_mapping_decisions",
    "7_completeness_controls", "8_data_quality_and_uncertainty",
    "9_methodology", "10_readiness_and_standard", "11_screening_register",
    "12_mapping_audit",
)

# WHICH SECTIONS DESCRIBE THE RUN, AND WHICH DESCRIBE THE CATALOGUE TODAY.
#
# The pack used to stamp ONE hash over everything and call it a reproduction guarantee.
# Four of the twelve sections read live tables — the factor register carries live factor
# metadata, transaction detail reads live activity fields, mapping decisions read live
# mapping status, and the audit journal is org-wide — so editing a factor moved the hash
# of a run that had not changed. An assuror comparing two packs for one run saw a
# mismatch and could not tell "the run was restated" (impossible; runs are immutable)
# from "somebody renamed a factor".
#
# Those sections are the right thing to show — seeing that a factor was edited under a
# filed run is exactly what an assuror is looking for. The defect was labelling them as
# frozen. So there are two stamps: one an assuror can hold the run to, and one that is
# expected to move.
_FROZEN_SECTIONS = (
    "1_inventory_statement", "2_reporting_period", "3_organisational_boundary",
    "7_completeness_controls", "8_data_quality_and_uncertainty",
    "9_methodology", "10_readiness_and_standard", "11_screening_register",
)
_CURRENT_STATE_SECTIONS = (
    "4_transaction_detail", "5_factor_register", "6_mapping_decisions",
    "12_mapping_audit",
)


def _journal_gap(coverage: Optional[dict], item: str, expected_by: str,
                 closed_text: str) -> dict:
    """A control is CLOSED only if this run's own journal shows it operating."""
    if coverage is None:
        return {"item": item, "expected_by": expected_by,
                "why_absent": f"NOT MEASURED for this run. {closed_text}",
                "what_would_close_it": "Render the pack with journal coverage measured."}
    bound, journalled = coverage["bound_activities"], coverage["journalled_activities"]
    if bound and journalled >= bound:
        return {"item": item, "expected_by": expected_by,
                "why_absent": f"CLOSED, and measured on this run: all {bound} bound "
                              f"activities carry a journal entry. {closed_text}",
                "what_would_close_it": "Already closed."}
    if not bound:
        return {"item": item, "expected_by": expected_by,
                "why_absent": "NOT APPLICABLE to this run: it binds no activities, so "
                              "there is no binding decision to journal.",
                "what_would_close_it": "Not applicable."}
    return {"item": item, "expected_by": expected_by,
            "why_absent": f"OPEN on this run: {bound - journalled} of {bound} bound "
                          f"activities have no journal entry, so the audit trail is "
                          f"incomplete for them. {closed_text}",
            "what_would_close_it": "Re-run automatic mapping, or record the missing "
                                   "decisions, so every bound activity is journalled."}


def _evidence_gaps(coverage: Optional[dict] = None) -> list:
    """What an ISAE 3410 / ISSA 5000 file expects that this platform cannot produce.

    Named explicitly, with the reason and the change that would close each. A pack
    that silently omitted these would read as complete to the one reader who most
    needs to know it is not.

    A gap marked CLOSED is now MEASURED against this run, not asserted. The override-log
    gap said "CLOSED — journals every binding decision" while the automatic binding path
    wrote nothing, so an inventory with an empty journal carried a written statement that
    its audit trail was complete. Claiming a control exists is exactly the failure this
    list was built to prevent, and the list was committing it.
    """
    return [
        {
            "item": "Reviewer identity",
            "expected_by": "ISSA 5000 / ISAE 3410 — who made each judgement",
            "why_absent": "Authentication is an organisation-scoped API key with no "
                          "concept of a person, so no per-user identity exists to "
                          "record. mapping_status proves A human decided, never which. "
                          "NOTE: the screening register (section 11) now records what "
                          "was investigated and concluded, and when — but still not "
                          "by whom.",
            "what_would_close_it": "Per-user authentication with an actor id written "
                                   "onto every mutation.",
        },
        _journal_gap(
            coverage, "Override log with before/after values",
            "Audit trail of changes to the mapping",
            "services/mapping_audit.py journals every binding decision append-only "
            "with from_factor_id and to_factor_id; see section 12 and "
            "GET /mappings/audit."),
        _journal_gap(
            coverage, "Reviewer timestamp",
            "When each judgement was made",
            "Every journalled decision carries `at`, and GET /mappings/audit/as_at "
            "answers what an activity was bound to at a given moment."),
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


def _hash_over(sections: dict, keys, run_id: int, extra=None) -> str:
    """SHA-256 over a named subset of the pack — never over its generation time.

    `generated_at` is excluded deliberately: a hash that moved every time the pack was
    rendered could not verify anything, which is the whole purpose of stamping it.
    """
    payload = {"v": PACK_VERSION, "run": run_id,
               "sections": {k: sections[k] for k in keys if k in sections}}
    if extra is not None:
        payload["evidence_gaps"] = extra
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _content_hash(sections: dict, gaps: list, run_id: int) -> str:
    """The stamp an assuror can hold the RUN to.

    Covers only sections derived from frozen run state, so it is stable for the life of
    the run: two calls years apart return the same value unless the run itself changed,
    and a run cannot change. See _FROZEN_SECTIONS for why the other four are excluded.
    """
    return _hash_over(sections, _FROZEN_SECTIONS, run_id, extra=gaps)


def _current_state_hash(sections: dict, run_id: int) -> str:
    """The stamp over what the CATALOGUE says today.

    Expected to move: it changes when a factor is edited, an activity re-mapped or the
    journal appended to. Comparing it between two packs for one run is how an assuror
    sees that something moved underneath a filed figure — which is the reason those
    sections are in the pack at all.
    """
    return _hash_over(sections, _CURRENT_STATE_SECTIONS, run_id)


def build_evidence_pack(db: Session, run: CalculationRun, *,
                        max_lines: int = DEFAULT_MAX_LINES,
                        uncertainty_iterations: int = 10_000) -> dict:
    """The full working-paper file for one immutable run, hash-stamped.

    TWO STAMPS, because the pack contains two kinds of thing.

    `content_hash` covers the sections derived from frozen run state. It is stable for the
    life of the run: two calls years apart agree, because a run cannot change, so a
    mismatch means the pack is not of that run.

    `current_state_hash` covers the sections that deliberately describe the CURRENT
    catalogue — the factor register, transaction detail, live mapping status and the
    org-wide journal. Those belong in the pack: seeing that a factor was edited under a
    filed run is exactly what an assuror is looking for. But they move, and a single hash
    over both meant an edit to the catalogue moved the stamp of an immutable run, so an
    assuror comparing two packs could not tell "this is a different run" from "somebody
    renamed a factor".
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
        "11_screening_register": _s11_screening_register(db, run),
        "12_mapping_audit": _s12_mapping_audit(db, run),
    }
    # Measure whether the journal actually covers THIS run's bound activities, rather
    # than asserting that the control exists.
    from ..models import ActivityRecord, MappingAuditEvent
    _bound = {a for (a,) in db.query(ActivityRecord.id).filter(
        ActivityRecord.organisation_id == run.organisation_id,
        ActivityRecord.factor_id.isnot(None)).all()}
    _journalled = {a for (a,) in db.query(MappingAuditEvent.activity_id).filter(
        MappingAuditEvent.organisation_id == run.organisation_id).distinct().all()
        if a in _bound}
    journal_coverage = {
        "bound_activities": len(_bound),
        "journalled_activities": len(_journalled),
        "unjournalled_activities": len(_bound) - len(_journalled),
        "note": "Measured on this organisation's bound activities. A control is only "
                "closed if it can be shown to have operated, not because the code that "
                "implements it exists.",
    }
    gaps = _evidence_gaps(journal_coverage)

    return {
        "pack": {
            "pack_version": PACK_VERSION,
            "run_id": run.id,
            "organisation": {"id": org.id if org else run.organisation_id,
                             "name": org.name if org else None},
            "content_hash": _content_hash(sections, gaps, run.id),
            "current_state_hash": _current_state_hash(sections, run.id),
            "generated_at": _utcnow_iso(),
            "hash_note": "TWO stamps, because the pack contains two kinds of thing. "
                         "content_hash covers the sections derived from FROZEN run state "
                         f"({', '.join(_FROZEN_SECTIONS)}) plus the evidence-gap list: it "
                         "is stable for the life of the run, and a mismatch means the "
                         "pack is not of that run. current_state_hash covers the sections "
                         f"describing the catalogue TODAY ({', '.join(_CURRENT_STATE_SECTIONS)}): "
                         "it is EXPECTED to move when a factor is edited, an activity "
                         "re-mapped or the journal appended to, and comparing it between "
                         "two packs for one run is how you see that something moved "
                         "underneath a filed figure. generated_at is excluded from both — "
                         "a hash that moved on every render could verify nothing.",
            "hashed_sections": {"content_hash": list(_FROZEN_SECTIONS),
                                "current_state_hash": list(_CURRENT_STATE_SECTIONS)},
            "section_order": list(_SECTION_ORDER),
        },
        "sections": sections,
        "evidence_gaps": gaps,
        "journal_coverage": journal_coverage,
        "note": "Assembled from the run's frozen state plus, where an assuror needs to "
                "see it, the CURRENT catalog and mapping state (sections 4, 5, 6, 12). "
                "This is the evidence an assuror "
                "works from; it is not an assurance opinion, and evidence_gaps names "
                "what this platform cannot currently supply.",
    }
