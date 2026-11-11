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
           actor: Optional[str] = None,
           actor_user_id: Optional[int] = None,
           commit: bool = True) -> Optional[MappingAuditEvent]:
    """Journal one binding decision. Unknown actions are ignored, never guessed.

    `actor` is `Principal.actor` — "user:<email>", "token:<id>", "org_key" — or
    "engine" for a machine binding. It is OPTIONAL and defaults to None rather than to
    any stand-in: a caller that cannot name an actor must leave the column NULL, which
    this journal reads as "recorded before identity existed" and reports as
    unattributed. A default here would quietly attribute every unthreaded call site.
    """
    from .calc import _utcnow_iso
    if action not in ACTIONS:
        return None
    ev = MappingAuditEvent(
        organisation_id=activity.organisation_id, activity_id=activity.id,
        action=action, from_factor_id=from_factor_id,
        to_factor_id=activity.factor_id, from_status=from_status,
        to_status=activity.mapping_status, basis=activity.mapping_basis,
        confidence=activity.mapping_confidence, note=note,
        actor=actor, actor_user_id=actor_user_id,
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
        # NULL means recorded before identity existed — see the model docstring. The key
        # is always present so a reader can tell an unattributed event from one whose
        # actor this serialiser forgot.
        "actor": e.actor, "actor_user_id": e.actor_user_id,
    } for e in q.order_by(MappingAuditEvent.id).all()]


def history_page(db: Session, organisation_id: int,
                 activity_id: Optional[int] = None, *,
                 limit: int = 100, offset: int = 0) -> dict:
    """One window of the journal, oldest first, windowed IN THE DATABASE.

    ``history`` is retained for the callers that legitimately need the whole journal
    (the evidence pack has to contain all of it); an HTTP caller gets this one.
    """
    from .pagination import page_of_query
    q = db.query(MappingAuditEvent).filter(
        MappingAuditEvent.organisation_id == organisation_id)
    if activity_id is not None:
        q = q.filter(MappingAuditEvent.activity_id == activity_id)
    page = page_of_query(q.order_by(MappingAuditEvent.id), limit, offset)
    page["rows"] = [{
        "id": e.id, "activity_id": e.activity_id, "action": e.action,
        "from_factor_id": e.from_factor_id, "to_factor_id": e.to_factor_id,
        "from_status": e.from_status, "to_status": e.to_status,
        "basis": e.basis, "confidence": e.confidence, "note": e.note, "at": e.at,
        "actor": e.actor, "actor_user_id": e.actor_user_id,
    } for e in page["rows"]]
    return page


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
            # The assuror's actual question is "who bound this, and when" — answering
            # the second half alone was the gap.
            "decided_by": last.actor,
            "events_considered": len(events)}


def summary(db: Session, organisation_id: int) -> dict:
    """What the journal can and cannot evidence."""
    rows = db.query(MappingAuditEvent).filter(
        MappingAuditEvent.organisation_id == organisation_id).all()
    by_action = {}
    for e in rows:
        by_action[e.action] = by_action.get(e.action, 0) + 1
    human_actions = ("approved", "overridden", "pact_bound")
    human_rows = [e for e in rows if e.action in human_actions]
    human = len(human_rows)
    # ATTRIBUTION IS COUNTED, NOT ASSERTED. Every row written before `users` existed
    # carries actor IS NULL, and no migration back-filled them, so this journal is
    # partly attributed for as long as those rows are in it. Reporting "reviewer
    # identity: closed" over a journal whose older half names nobody would be the
    # misleading answer the gap was left open to avoid — so the pack is handed the two
    # counts and says "closed from <date>" rather than "closed".
    # ATTRIBUTED MEANS A PERSON IS NAMED, and the first version of this got it wrong.
    # It counted any truthy actor, so 'org_key' and 'token:<id>' — which name an
    # ORGANISATION and a CREDENTIAL, nobody — closed the gap. The pack then served
    # "CLOSED ... all N human decisions name the principal that made them" with
    # "(0 a named person)" in the same sentence. That is precisely the claim the
    # docstring this replaced existed to refuse: "recording the organisation as though
    # it were a reviewer would turn an honest gap into a misleading answer".
    #
    # ISSA 5000 / ISAE 3410 ask WHO made each judgement. Only "user:<email>" answers it.
    attributed = [e for e in human_rows if str(e.actor or "").startswith("user:")]
    # The rest are split out rather than lumped together, because "a credential acted and
    # we know which" is genuinely more than "we have no idea", and an assuror deciding
    # how much weight to give the journal needs to tell them apart.
    impersonal = [e for e in human_rows
                  if e.actor and not str(e.actor).startswith("user:")]
    unknown = [e for e in human_rows if not e.actor]
    unattributed = len(impersonal) + len(unknown)
    named_person = len(attributed)
    earliest_attributed = min((e.at for e in attributed), default=None)

    gaps_closed = ["Override log with before/after values", "Reviewer timestamp"]
    still_open = []
    if human == 0:
        # No human decision has been journalled at all, so this organisation has no
        # evidence either way. Claiming the gap closed on an empty set is the
        # empty-collection pass this codebase treats as a defect.
        still_open.append({
            "gap": "Reviewer identity",
            "why": "No human binding decision has been journalled yet, so there is "
                   "nothing to attribute. The mechanism is in place: an approve or "
                   "override made now records the principal that made it.",
        })
    elif unattributed:
        bits = []
        if unknown:
            bits.append(f"{len(unknown)} journalled before authentication had any "
                        f"concept of a person, so they name no actor at all")
        if impersonal:
            bits.append(f"{len(impersonal)} made with a credential that names no "
                        f"person (the tenant's shared organisation key, or a token "
                        f"issued to a firm rather than an individual)")
        still_open.append({
            "gap": "Reviewer identity",
            "why": f"{unattributed} of {human} human decisions do not name a person: "
                   + "; ".join(bits)
                   + f". Neither was back-filled — inventing a reviewer for a filed run "
                     f"is the misleading answer this gap was kept open to avoid. "
                     f"{len(attributed)} decision(s) name an individual.",
            "closed_from": earliest_attributed,
            "unknown_actor": len(unknown),
            "impersonal_actor": len(impersonal),
        })
    else:
        gaps_closed.append("Reviewer identity")

    return {
        "version": AUDIT_VERSION,
        "events": len(rows),
        "by_action": by_action,
        "activities_journalled": len({e.activity_id for e in rows}),
        "human_decisions": human,
        "attribution": {
            "human_decisions": human,
            "attributed": len(attributed),
            "unattributed": unattributed,
            "by_a_named_person": named_person,
            "by_an_impersonal_credential": len(impersonal),
            "no_actor_recorded": len(unknown),
            "earliest_attributed_at": earliest_attributed,
            "note": "An unattributed event was recorded before authentication had any "
                    "concept of a person. NULL there means 'recorded before identity "
                    "existed', which is not the same as the tenant's shared key acting "
                    "('org_key') and not the same as a machine binding ('engine').",
        },
        "closes_evidence_gaps": gaps_closed,
        "does_not_close": still_open,
        "note": "Append-only by construction: no update path and no status column to "
                "flip. Factor ids are provenance and are never joined back — a "
                "superseded factor may since have been retired, and the journal must "
                "still report what the binding was.",
    }
