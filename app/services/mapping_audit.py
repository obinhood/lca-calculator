"""The append-only journal of factor-binding decisions.

Closes two of the six gaps the evidence pack has to declare about itself: the
override log with before and after values, and the decision timestamp. It does
NOT close the third, reviewer identity, and does not pretend to — authentication
is an organisation-scoped API key with no concept of a person, so there is no
actor to record. Recording the organisation as though it were a reviewer would
turn an honest gap into a misleading answer, so the pack keeps naming it.

APPEND-ONLY BY CONSTRUCTION. There is no update path and no status column to
flip. The older AssuranceFinding table resolves in place with no journal, which
cannot answer "what was this activity bound to on the day the opinion was
issued" — precisely the question audit evidence exists to answer.

Factor ids are recorded for PROVENANCE and are never joined back. A superseded
factor may since have been retired from the catalogue, and the journal must still
report what the binding was.
"""
import json
from typing import Optional

from sqlalchemy.orm import Session

from ..models import ActivityRecord, MappingAuditEvent

AUDIT_VERSION = "map-v1"

ACTIONS = ("auto_mapped", "suggested", "approved", "overridden", "unmapped",
           "pact_bound")


def record(db: Session, activity: ActivityRecord, action: str, *,
           from_factor_id: Optional[int] = None,
           from_status: Optional[str] = None,
           note: Optional[str] = None,
           now: Optional[str] = None,
           commit: bool = True) -> Optional[MappingAuditEvent]:
    """Journal one binding decision. Unknown actions are ignored, never guessed."""
    from .calc import _utcnow_iso
    if action not in ACTIONS:
        return None
    ev = MappingAuditEvent(
        organisation_id=activity.organisation_id, activity_id=activity.id,
        action=action, from_factor_id=from_factor_id,
        to_factor_id=activity.factor_id, from_status=from_status,
        to_status=activity.mapping_status, basis=activity.mapping_basis,
        confidence=activity.mapping_confidence, note=note,
        at=now or _utcnow_iso())
    db.add(ev)
    if commit:
        db.commit()
        db.refresh(ev)
    return ev


def history(db: Session, organisation_id: int,
            activity_id: Optional[int] = None) -> list:
    """The journal, oldest first — the order a reader reconstructs a binding in."""
    q = db.query(MappingAuditEvent).filter(
        MappingAuditEvent.organisation_id == organisation_id)
    if activity_id is not None:
        q = q.filter(MappingAuditEvent.activity_id == activity_id)
    return [{
        "id": e.id, "activity_id": e.activity_id, "action": e.action,
        "from_factor_id": e.from_factor_id, "to_factor_id": e.to_factor_id,
        "from_status": e.from_status, "to_status": e.to_status,
        "basis": e.basis, "confidence": e.confidence, "note": e.note, "at": e.at,
    } for e in q.order_by(MappingAuditEvent.id).all()]


def binding_as_at(db: Session, activity_id: int, at: str) -> dict:
    """What an activity was bound to at a moment in time.

    The question an assuror actually asks, and the one an in-place status column
    cannot answer.
    """
    events = db.query(MappingAuditEvent).filter(
        MappingAuditEvent.activity_id == activity_id,
        MappingAuditEvent.at <= at).order_by(MappingAuditEvent.id).all()
    if not events:
        return {"determinable": False, "activity_id": activity_id, "as_at": at,
                "reason": "no journalled decision at or before that time — the "
                          "binding is UNKNOWN then, which is not the same as unmapped"}
    last = events[-1]
    return {"determinable": True, "activity_id": activity_id, "as_at": at,
            "factor_id": last.to_factor_id, "status": last.to_status,
            "basis": last.basis, "action": last.action, "decided_at": last.at,
            "events_considered": len(events)}


def summary(db: Session, organisation_id: int) -> dict:
    """What the journal can and cannot evidence."""
    rows = db.query(MappingAuditEvent).filter(
        MappingAuditEvent.organisation_id == organisation_id).all()
    by_action = {}
    for e in rows:
        by_action[e.action] = by_action.get(e.action, 0) + 1
    human = sum(v for k, v in by_action.items()
                if k in ("approved", "overridden", "pact_bound"))
    return {
        "version": AUDIT_VERSION,
        "events": len(rows),
        "by_action": by_action,
        "activities_journalled": len({e.activity_id for e in rows}),
        "human_decisions": human,
        "closes_evidence_gaps": [
            "Override log with before/after values",
            "Reviewer timestamp",
        ],
        "does_not_close": [{
            "gap": "Reviewer identity",
            "why": "Authentication is an organisation-scoped API key with no concept "
                   "of a person, so there is no actor to record. Journalling the "
                   "organisation as though it were a reviewer would turn an honest "
                   "gap into a misleading answer.",
        }],
        "note": "Append-only by construction: no update path and no status column to "
                "flip. Factor ids are provenance and are never joined back — a "
                "superseded factor may since have been retired, and the journal must "
                "still report what the binding was.",
    }
