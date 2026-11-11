"""Data lifecycle: erasure, retention, tenant deletion and portability.

Until now nothing in this platform deleted anything. There was no DELETE, PUT or PATCH
on any of the 125 operations, no deletion path by API or by DBA, and the obvious manual
workaround fails on referential integrity. Two commercial consequences followed: a
processor DPA under GDPR Art. 28(3)(g) promises deletion or return of all personal data
on termination and we could not honour it, and an onboarding typo was permanent and
invisible because nothing could be read back or exported.

This module is the whole deletion surface. Every path that removes or erases tenant data
goes through here, so the reproduction contract is defended in ONE place rather than
re-argued at each call site.


THE DELETION POLICY, AND WHY IT IS SAFE
=======================================

The hard question is not how to delete a row. It is what deleting one does to a filed
calculation run that froze it. A run is the product's core claim: it must re-render
byte-identically years later, reading only what it froze. So before choosing a policy I
traced every reader of ActivityRecord reachable from a frozen-run renderer. Three facts
decide it.

**1. The staleness fingerprint does not hash the free text.**
``calc.activities_fingerprint`` hashes
``id:factor_id:quantity:unit:date:category:ghgp_category:entity_id:coverage_start:coverage_end``.
``description`` and ``source_file`` are not in it. Blanking those two fields therefore
leaves every existing run's fingerprint BIT-IDENTICAL — no filed run flips to "stale".
Deleting the row instead removes its id from the set and changes the fingerprint, so
every prior run silently starts reporting itself stale.

**2. The frozen-run renderers INNER JOIN the live activities table.**
``reports/secr.py`` joins ActivityRecord to EmissionLineItem to build the SECR / ESOS /
ESRS E1-5 kWh figure; ``services/evidence_pack.py`` does the same for transaction detail,
and takes its line count from that query. An inner join does not raise when the parent
row disappears — the row simply stops being returned. A hard delete would therefore make
a filed energy total silently FALL while the emissions total beside it, read from
``EmissionLineItem.co2e``, stayed exactly where it was. That is the renderer-disagreement
defect class, reached by deletion.

**3. For runs frozen before the category/unit/quantity freeze, the live row is the only copy.**
``secr.py`` reads ``_d["activity_category"] if "activity_category" in _d else _live_cat``
(and the same for unit and quantity). The ``in`` test rather than a truthiness test is
deliberate there. A run frozen before those keys existed has NO frozen fallback: delete
its activity and the energy figure is not merely un-joinable, it is unrecoverable.

So the policy has three verbs, because the data has three genuinely different
relationships to a filed disclosure:

================================  ==========================  ==============================
situation                         action                      why
================================  ==========================  ==============================
no run froze this activity        HARD DELETE                 nothing depends on it
>= 1 run froze it                 REDACT the free text        erases the personal data;
                                                              provably cannot move a figure
caller demanded a hard delete     REFUSE, 409, naming every   a refusal must say WHY; never
of a frozen activity              blocking run                silently downgrade to redact
================================  ==========================  ==============================

**Why redaction is honest and not a dodge.** Every figure a filed run renders comes from
``EmissionLineItem.co2e``, the frozen ``details`` blob, or quantity/unit/category/date —
all of which redaction leaves untouched. An emissions figure cannot be in the free text.
The reproduction contract over NUMBERS is kept exactly, and there is a test asserting the
fingerprint is unchanged.

What redaction does change is rendered FREE TEXT: the evidence pack puts
``activity.description`` into its transaction detail. A pack re-rendered after an erasure
will show the tombstone instead. That is unavoidable — Art. 17 erasure and "re-renders
byte-identically forever" are in genuine conflict for personal free text, and only one of
the two is a legal obligation. The honest resolution is NOT to claim a byte-identity we
no longer have; it is to make the change VISIBLE. The tombstone is self-describing
(``[redacted <ISO> - <reason>]``) so a reader sees "this was erased under an erasure
request" and never "this line never had a description". An absence that announces itself
is the cannot_determine doctrine applied to text.

**Consequences deliberately not hidden.**

* ``services/screening.py`` keys duplicate detection partly on ``description``, so a
  redacted row hashes differently and a FUTURE screening run may group it differently.
  Filed runs are unaffected: ``RunScreeningStatement`` freezes the verdict and
  ``screening_version`` is frozen on the run.
* ``upload_hash`` is KEPT. Erasing an activity does not let the same CSV be re-uploaded
  (the idempotency guard still returns 409). The hash is not personal data, and the
  ingestion event genuinely happened.
* ``resolver.auto_map_activity`` reads ``description``, but only when binding a factor.
  An already-bound row never re-resolves, so redaction cannot re-map anything.
* A HARD delete of an unfrozen activity can CLEAR a staleness warning. ``summary``
  computes ``stale`` by comparing the live activity fingerprint with the frozen one, so
  removing a row that was added AFTER a run returns the live set to what the run froze
  and the run correctly stops reporting itself stale. No disclosed figure moves, and the
  verdict is accurate — the run really does match current data again — but the direction
  is worth knowing, so it is pinned by
  ``test_hard_delete_can_clear_a_staleness_warning``. Redaction, the path a frozen row
  takes, cannot do even this: the fingerprint is byte-identical either way.


WHY THE TENANT-DELETE DOES NOT TRUST THE CASCADE
================================================

``delete_tenant`` neither relies on ON DELETE CASCADE (the models declare none) nor
hardcodes a table list that would rot. It classifies EVERY table in ``Base.metadata``
into one of three buckets and ``assert_tables_classified`` raises on any table it has
never heard of. A table added later fails the test suite instead of silently orphaning
its rows — the recurring defect in this codebase is a readiness verdict that passes over
something it never looked at, so the classification is exhaustive by construction.

Delete order is ``reversed(Base.metadata.sorted_tables)``: SQLAlchemy sorts parents
first, so reversed is children first, which is FK-safe without anyone maintaining an
order by hand. ``PRAGMA foreign_keys=ON`` is set on every SQLite connection
(``database.py``), so self-referential FKs (``removal_records.reverses_record_id``) are
NULLed before that table's rows are deleted: a multi-row DELETE otherwise fails on the
row still referenced by its sibling.
"""
import json
import logging
from typing import Dict, List, Optional

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from ..models import (
    ActivityRecord, Base, CalculationRun, EmissionLineItem, Organisation,
)
from .frozen import parse_list

# A local logger with no format of its own. If app/observability.py is present on the
# branch, swapping this one line to `observability.get_logger(__name__)` is the whole
# integration — the name is already in the `app.*` tree that module configures.
log = logging.getLogger("app.lifecycle")

# Reasons recorded on a redaction. Free-form strings are rejected at the API boundary so
# the tombstone cannot become a second free-text field carrying personal data.
REDACTION_REASONS = ("erasure_request", "retention", "correction", "tenant_deletion")

# The fields a redaction clears. EXACTLY the two free-text fields an ingestion path can
# put personal data into, and deliberately nothing else: every other column on the row is
# either a number a filed run depends on, or a code with no personal content.
#
# `description` carries employee names on commuting and business-travel lines and site
# contacts on supply lines; `source_file` carries uploaded filenames, which in practice
# carry names and employee ids. Neither is in activities_fingerprint().
REDACTED_FIELDS = ("description", "source_file")

# Columns that must NEVER leave the process, on any read path: list endpoints, exports,
# subject-access responses. One set, applied centrally, so a new serialiser cannot leak a
# credential by forgetting — same doctrine as models.factor_visible_to().
#
# Deliberately an exact-name set and NOT a regex over "hash|key|secret": `upload_hash`,
# `series_key`, `finding_key`, `entity_key`, `market_key` and `year_key` are ordinary
# business data a tenant needs back, and a pattern match would silently strip them from
# every export — losing the customer's own identifiers to protect nothing.
NEVER_SERIALISE = frozenset((
    "api_key_hash",            # organisations — the tenant credential
    "client_secret_hash",      # pact_clients — a partner's secret
    "token_hash",              # pact_tokens — a bearer token
    "access_token_hash",       # assurance_engagements — the assuror's access grant
))


def safe_row(mapping) -> dict:
    """One row as a plain dict with every credential column dropped."""
    return {k: v for k, v in dict(mapping).items() if k not in NEVER_SERIALISE}


# The tables erasure and retention actually reach: `activities` and its two children.
ERASURE_TABLES = ("activities", "activity_findings", "mapping_audit_events")


def erasure_scope() -> dict:
    """What an erasure or retention run covers, and what it does NOT.

    Declared in every success payload. Both operations act on activity records only —
    reporting entities, nature sites, financed positions, assurance engagements,
    product footprints and the rest are untouched. A payload that said only "retention
    applied" invited the reading that the whole inventory had aged out.

    The uncovered count is DERIVED from the table classification, not written down, for
    the same reason the delete order is: a hardcoded number silently stops being true
    the next time a tenant table is added.
    """
    all_tenant = set(TENANT_DIRECT) | set(TENANT_VIA)
    not_covered = sorted(all_tenant - set(ERASURE_TABLES))
    return {
        "tables_covered": list(ERASURE_TABLES),
        "tables_not_covered": not_covered,
        "tables_not_covered_count": len(not_covered),
        "note": ("Erasure and retention act on activity records only. The tables listed "
                 "in tables_not_covered are NOT touched — use "
                 "DELETE /organisations/self to remove everything, or "
                 "GET /organisations/export to see the full set first."),
    }


def tombstone(at: str, reason: str, activity_id: int) -> str:
    """The text left in a redacted field. UNIQUE PER ROW, deliberately.

    Self-describing on purpose. An empty string or NULL would make an erased
    description indistinguishable from one that was never recorded, which is the
    silent-absence failure this platform treats as a defect everywhere else.

    The activity id is in the string because `source_file` is a BULK-MUTATION SELECTOR:
    four endpoints (/activities/coverage_window, /activities/entity,
    /activities/crosswalk_chain, /activities/series_key) accept
    `source_file=<name>` and UPDATE every matching row. A tombstone shared by a whole
    retention sweep would be a selector matching all of them at once, and three of the
    fields those endpoints set — entity_id, coverage_start, coverage_end — are in
    activities_fingerprint(), with entity_id also driving the consolidation share. A
    wrongly-wide bulk update there moves published figures.

    Per-row uniqueness removes that entirely: no two rows ever carry the same redacted
    value, so no redacted value can select a batch. (The converse is inherent to erasure
    and not fixable here: a filter on the ORIGINAL filename cannot match a row whose
    filename has been erased. That is what erasure means; `upload_hash` is deliberately
    retained so the provenance link to the upload survives without the name.)
    """
    return "[redacted {} - {} - activity {}]".format(at, reason, activity_id)


# --- THE shared rule --------------------------------------------------------------------

def exclusion_index(db: Session, organisation_id: int) -> Dict[int, List[dict]]:
    """{activity_id: [runs that froze it as an EXCLUSION]} for one organisation.

    A run freezes an activity in one of two ways, and only one of them leaves a line
    item. ``compute_co2e`` writes a per-activity exclusion register to ``run.notes`` —
    ``[{"activity_id": ..., "error": ...}]`` — for a negative quantity, an unconvertible
    unit, a missing quantity and the rest. Those activities produce NO EmissionLineItem,
    so a blocker check that only joins line items reports "no run froze this" and the
    row is hard-deleted, leaving the filed run's exclusion register pointing at an
    activity id that no longer resolves: a disclosure naming a record that does not
    exist.

    Built as an index rather than queried per activity because a retention sweep asks
    this of every row, and ``notes`` is a JSON blob — portable JSON querying across
    SQLite and PostgreSQL is not worth buying for this.
    """
    index: Dict[int, List[dict]] = {}
    rows = db.query(CalculationRun.id, CalculationRun.created_at, CalculationRun.status,
                    CalculationRun.reporting_period_id, CalculationRun.notes).filter(
        CalculationRun.organisation_id == organisation_id).all()
    for run_id, created_at, status, period_id, notes in rows:
        for entry in parse_list(notes):
            if not isinstance(entry, dict):
                continue
            aid = entry.get("activity_id")
            if not isinstance(aid, int):
                continue
            index.setdefault(aid, []).append({
                "run_id": run_id, "created_at": created_at, "status": status,
                "reporting_period_id": period_id, "frozen_lines": 0,
                "frozen_as": "exclusion",
                "exclusion_reason": entry.get("error"),
            })
    return index


def runs_freezing_activity(db: Session, activity_id: int,
                           exclusions: Optional[Dict[int, List[dict]]] = None
                           ) -> List[dict]:
    """THE definition of "a filed run depends on this activity". One rule, every path.

    Returns a row per run that froze this activity, newest first, with enough identity
    for a refusal to NAME what blocks it rather than just saying no.

    Every deletion path must consult this — single-record erasure, retention sweeps, and
    anything added later. The callers are pinned by tests/test_lifecycle.py, so a new
    deletion path that forgets it fails the suite rather than quietly breaking a filed
    disclosure. Same doctrine as models.factor_visible_to().

    A run freezes an activity TWO ways, and both count:

      * as a LINE — any EmissionLineItem references it, whatever the method or scope.
        The SECR energy join and the evidence pack's transaction detail read location
        lines; a market line is just as frozen.
      * as an EXCLUSION — the run's frozen ``notes`` register names it as an errored or
        uncalculable record. No line item exists, but the run has filed a statement
        ABOUT that activity id, and its coverage counters were computed over it.

    Only counting lines decided the reproduction contract on half the picture. Pass
    `exclusions` (from exclusion_index) when asking about many activities at once; it is
    computed on demand otherwise.
    """
    rows = (db.query(CalculationRun.id, CalculationRun.created_at, CalculationRun.status,
                     CalculationRun.reporting_period_id,
                     func.count(EmissionLineItem.id))
            .join(EmissionLineItem, EmissionLineItem.run_id == CalculationRun.id)
            .filter(EmissionLineItem.activity_id == activity_id)
            .group_by(CalculationRun.id, CalculationRun.created_at,
                      CalculationRun.status, CalculationRun.reporting_period_id)
            .all())
    out = [{"run_id": r[0], "created_at": r[1], "status": r[2],
            "reporting_period_id": r[3], "frozen_lines": r[4],
            "frozen_as": "line"} for r in rows]

    if exclusions is None:
        activity = db.get(ActivityRecord, activity_id)
        exclusions = (exclusion_index(db, activity.organisation_id)
                      if activity is not None else {})
    seen = {r["run_id"] for r in out}
    for entry in exclusions.get(activity_id, []):
        # A run that BOTH lined and excluded the activity is already blocking; the line
        # is the stronger statement, so don't double-report it.
        if entry["run_id"] not in seen:
            out.append(entry)

    # Newest first, but a NULL created_at must not sort as newest — it sorts last, and
    # stays visible either way.
    out.sort(key=lambda r: (r["created_at"] is not None, r["created_at"] or ""),
             reverse=True)
    return out


def _own_activity(db: Session, organisation_id: int, activity_id: int) -> Optional[ActivityRecord]:
    """The caller's own activity, or None. Mirrors main._get_own_activity but returns
    None rather than raising, so the service stays free of HTTP concerns."""
    a = db.get(ActivityRecord, activity_id)
    if a is None or a.organisation_id != organisation_id:
        return None
    return a


# --- erasure ----------------------------------------------------------------------------

def redact_activity(db: Session, activity: ActivityRecord, reason: str, now: str,
                    commit: bool = True) -> dict:
    """Clear the free text, keep the row. Idempotent.

    Re-redacting an already-redacted row does NOT overwrite the original tombstone:
    the first erasure is the one that happened, and moving its timestamp forward would
    misreport when the personal data actually went.
    """
    if activity.redacted_at is not None:
        return {"action": "already_redacted", "activity_id": activity.id,
                "redacted_at": activity.redacted_at,
                "redaction_reason": activity.redaction_reason}
    mark = tombstone(now, reason, activity.id)
    cleared = []
    for field in REDACTED_FIELDS:
        # Stamp ONLY a field that actually held something. Stamping an empty one wrote
        # "[redacted ...]" over a NULL and made the evidence pack assert an erasure that
        # never happened — personal data reported as removed where none existed. That is
        # the absence-is-not-a-finding invariant inverted, and on an artefact whose whole
        # purpose is to be shown to an assuror a fabricated erasure record is worse than
        # no record at all. An empty field stays exactly as it was.
        if getattr(activity, field, None):
            cleared.append(field)
            setattr(activity, field, mark)
    activity.redacted_at = now
    activity.redaction_reason = reason
    if commit:
        db.commit()
    log.info("activity redacted", extra={"fields": {
        "activity_id": activity.id, "organisation_id": activity.organisation_id,
        "reason": reason, "fields_cleared": cleared}})
    out = {"action": "redacted", "activity_id": activity.id, "redacted_at": now,
           "redaction_reason": reason, "fields_cleared": cleared,
           "scope": erasure_scope()}
    if not cleared:
        # `redacted_at` still records that the request was honoured — the row is now
        # guaranteed free of free text, which is true and auditable — but the reader is
        # told plainly that nothing had to be removed, so "redacted" is never mistaken
        # for "personal data was found here".
        out["nothing_to_redact"] = True
        out["note"] = ("the request was applied, but this record held no free text: "
                       "no field was overwritten and nothing was erased")
    return out


def _hard_delete_activity(db: Session, activity: ActivityRecord,
                          commit: bool = True) -> dict:
    """Remove an activity no run froze, plus the child rows that reference it.

    activity_findings and mapping_audit_events both carry a NOT NULL FK to activities,
    so they must go first or the DELETE fails under PRAGMA foreign_keys=ON.
    """
    from ..models import ActivityFinding, MappingAuditEvent
    aid = activity.id
    org_id = activity.organisation_id
    findings = db.query(ActivityFinding).filter(
        ActivityFinding.activity_id == aid).delete(synchronize_session=False)
    events = db.query(MappingAuditEvent).filter(
        MappingAuditEvent.activity_id == aid).delete(synchronize_session=False)
    db.delete(activity)
    if commit:
        db.commit()
    log.info("activity hard deleted", extra={"fields": {
        "activity_id": aid, "organisation_id": org_id,
        "findings_deleted": findings, "audit_events_deleted": events}})
    return {"action": "deleted", "activity_id": aid,
            "findings_deleted": findings, "audit_events_deleted": events}


def erase_activity(db: Session, organisation_id: int, activity_id: int, *,
                   mode: str, reason: str, now: str) -> dict:
    """Erase one activity under the policy in this module's docstring.

    mode:
      auto    - hard delete if no run froze it, else redact. The sane default.
      hard    - hard delete, or REFUSE with the blocking runs named.
      redact  - always redact, even when nothing froze it (erase the text, keep the row).

    Returns a dict carrying `action`; the caller maps that onto HTTP. `blocked` is the
    only action that is a refusal, and it always carries `blocking_runs`.
    """
    if mode not in ("auto", "hard", "redact"):
        raise ValueError("mode must be auto, hard or redact")
    if reason not in REDACTION_REASONS:
        raise ValueError("reason must be one of {}".format(", ".join(REDACTION_REASONS)))

    activity = _own_activity(db, organisation_id, activity_id)
    if activity is None:
        return {"action": "not_found", "activity_id": activity_id}

    blockers = runs_freezing_activity(db, activity_id)

    if mode == "redact":
        out = redact_activity(db, activity, reason, now)
        out["blocking_runs"] = blockers
        return out

    if not blockers:
        return _hard_delete_activity(db, activity)

    if mode == "hard":
        # The refusal names what blocks it. A bare 409 would leave the caller with no
        # way to find out which disclosure they would have broken.
        log.info("hard delete refused", extra={"fields": {
            "activity_id": activity_id, "organisation_id": organisation_id,
            "blocking_runs": [b["run_id"] for b in blockers]}})
        return {
            "action": "blocked", "activity_id": activity_id,
            "blocking_runs": blockers,
            "reason": (
                "{} calculation run(s) froze this activity. Hard-deleting it would "
                "drop its lines from the SECR/ESOS energy total and the evidence pack's "
                "transaction detail of those filed runs, and would change the activity "
                "fingerprint every one of them reports freshness against. Use "
                "mode=redact to erase the personal data while keeping the run "
                "reproducible.".format(len(blockers))),
            "remedy": "mode=redact",
        }

    out = redact_activity(db, activity, reason, now)
    out["blocking_runs"] = blockers
    out["note"] = ("hard delete was not available: {} filed run(s) froze this activity, "
                   "so its free text was erased and the row kept so those runs stay "
                   "reproducible".format(len(blockers)))
    return out


# --- retention --------------------------------------------------------------------------

def _cutoff(retention_days: int, as_of: str) -> str:
    """The ISO date on/before which an activity is out of retention."""
    from datetime import date, timedelta
    from .calc import _parse_iso_date
    d = _parse_iso_date(as_of)
    if d is None:
        raise ValueError("as_of must be an ISO date (YYYY-MM-DD)")
    if isinstance(d, date):
        return (d - timedelta(days=retention_days)).isoformat()
    raise ValueError("as_of must be an ISO date (YYYY-MM-DD)")


def retention_candidates(db: Session, organisation_id: int, retention_days: int,
                         as_of: str) -> dict:
    """Which activities fall outside retention, split by what CAN be done to them.

    This is the dry run's whole substance, and it is deliberately split rather than
    reporting one total: "we would touch 400 rows" hides that 380 of them can only be
    redacted because filed runs froze them. A preview that overstated what would be
    deleted would be as misleading as one that understated it.

    An activity with a NULL or malformed date is NEVER a candidate. It cannot be shown to
    be outside the window, and the honest answer to "is this older than N days" with no
    usable date is cannot_determine, not yes. Those rows are counted and reported so the
    silence is visible.
    """
    from .calc import _parse_iso_date
    cutoff = _cutoff(retention_days, as_of)
    rows = db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == organisation_id).all()

    # Built once for the whole sweep rather than per row: the exclusion register lives in
    # each run's JSON `notes`, so asking per activity would re-read every run per row.
    exclusions = exclusion_index(db, organisation_id)

    deletable, redactable, already, undated = [], [], [], []
    for a in rows:
        if a.date is None or _parse_iso_date(a.date) is None:
            undated.append(a.id)
            continue
        if a.date >= cutoff:
            continue
        if a.redacted_at is not None:
            already.append(a.id)
            continue
        if runs_freezing_activity(db, a.id, exclusions=exclusions):
            redactable.append(a.id)
        else:
            deletable.append(a.id)

    return {
        "retention_days": retention_days, "as_of": as_of, "cutoff_date": cutoff,
        "activities_total": len(rows),
        "scope": erasure_scope(),
        "would_hard_delete": sorted(deletable),
        "would_redact": sorted(redactable),
        "already_redacted": sorted(already),
        "undated_skipped": sorted(undated),
        "counts": {
            "would_hard_delete": len(deletable),
            "would_redact": len(redactable),
            "already_redacted": len(already),
            "undated_skipped": len(undated),
        },
        "note": ("Rows with no usable date are skipped, never deleted: nothing shows them "
                 "to be outside the window. Rows a filed run froze are redacted, not "
                 "deleted, so those runs stay reproducible."),
    }


def retention_apply(db: Session, organisation_id: int, retention_days: int,
                    as_of: str, now: str) -> dict:
    """Apply retention. Same split as the preview, then acted on.

    Recomputes the candidate set rather than taking it from the caller: a preview the
    client replayed later could act on a row a run has frozen in the meantime.
    """
    plan = retention_candidates(db, organisation_id, retention_days, as_of)
    exclusions = exclusion_index(db, organisation_id)
    deleted, redacted = [], []
    for aid in plan["would_hard_delete"]:
        a = _own_activity(db, organisation_id, aid)
        if a is None:
            continue
        # Re-check under the shared rule: a run may have frozen this row since the plan
        # was computed, which turns a delete into a redaction.
        if runs_freezing_activity(db, aid, exclusions=exclusions):
            redact_activity(db, a, "retention", now, commit=False)
            redacted.append(aid)
        else:
            _hard_delete_activity(db, a, commit=False)
            deleted.append(aid)
    for aid in plan["would_redact"]:
        a = _own_activity(db, organisation_id, aid)
        if a is None:
            continue
        redact_activity(db, a, "retention", now, commit=False)
        redacted.append(aid)
    db.commit()
    log.info("retention applied", extra={"fields": {
        "organisation_id": organisation_id, "retention_days": retention_days,
        "as_of": as_of, "deleted": len(deleted), "redacted": len(redacted)}})
    return {
        "retention_days": retention_days, "as_of": as_of,
        "cutoff_date": plan["cutoff_date"],
        "hard_deleted": sorted(deleted), "redacted": sorted(redacted),
        "undated_skipped": plan["undated_skipped"],
        "counts": {"hard_deleted": len(deleted), "redacted": len(redacted),
                   "undated_skipped": len(plan["undated_skipped"])},
        "scope": erasure_scope(),
    }


# --- tenant deletion --------------------------------------------------------------------

# Every table in Base.metadata, classified. See the module docstring for why this is
# exhaustive rather than a list of the ones someone remembered.
#
# TENANT_DIRECT: has its own organisation_id column.
TENANT_DIRECT = (
    "activities", "activity_findings", "api_tokens", "assurance_engagements",
    "carbon_credits", "cbam_goods", "calculation_runs", "deliverability_links",
    "emissions_targets", "financed_positions", "granular_certificates", "hourly_loads",
    "lca_assessments", "mapping_audit_events", "market_instruments", "nature_sites",
    "nature_targets", "pact_clients", "pact_tokens", "product_footprints",
    "removal_records", "reporting_entities", "reporting_periods",
    "scope3_category_declarations", "taxonomy_activities", "users",
)

# TENANT_VIA: no organisation_id of its own; reached through a parent that has one.
# (child table) -> (fk column on child, parent table, parent pk)
TENANT_VIA = {
    "assurance_findings": ("engagement_id", "assurance_engagements", "id"),
    "emission_line_items": ("run_id", "calculation_runs", "id"),
    # Per-gas masses follow their FACTOR's tenancy. The parent is the one MIXED table
    # here (see GLOBAL_REFERENCE below): the join to emission_factors.organisation_id
    # therefore removes the gas rows of factors THIS tenant contributed and leaves the
    # platform catalogue's gas rows — which is the same split delete_tenant applies to
    # the parent itself, so the two cannot disagree. Delete order is derived from the
    # FK graph, so these children go before their factors without a hand-kept list.
    "factor_gas_masses": ("factor_id", "emission_factors", "id"),
    "lca_items": ("assessment_id", "lca_assessments", "id"),
    "nature_impacts_dependencies": ("site_id", "nature_sites", "id"),
    "run_entity_boundary": ("run_id", "calculation_runs", "id"),
    "run_financed_lines": ("run_id", "calculation_runs", "id"),
    "run_removal_lines": ("run_id", "calculation_runs", "id"),
    "run_residual_mix_statements": ("run_id", "calculation_runs", "id"),
    "run_scope3_declarations": ("run_id", "calculation_runs", "id"),
    "run_screening_statements": ("run_id", "calculation_runs", "id"),
}

# GLOBAL_REFERENCE: platform data shared by every tenant, or the tenant row itself.
# Deleting any of these as part of a tenant wipe would damage other tenants.
#
# emission_factors is the subtle one: it is MOSTLY global, but a non-NULL
# organisation_id row is a factor that tenant contributed (see models.factor_visible_to).
# Those rows ARE tenant data and are handled explicitly in delete_tenant rather than by
# this classification, because the table is a mix.
GLOBAL_REFERENCE = (
    "cbam_benchmarks", "cbam_default_values", "crosswalks", "crosswalk_mappings",
    "emission_factors", "fx_rates", "hourly_grid_intensities", "organisations",
    "price_indices", "residual_mix_rates",
)


def assert_tables_classified(metadata) -> None:
    """Every table must be in exactly one bucket. Raises otherwise.

    This is the gate that cannot fail open. A table added later and classified nowhere
    makes this raise — and the test that calls it fail — instead of having its rows
    quietly survive a tenant deletion that reported success.
    """
    known = set(TENANT_DIRECT) | set(TENANT_VIA) | set(GLOBAL_REFERENCE)
    actual = set(metadata.tables.keys())
    unclassified = sorted(actual - known)
    if unclassified:
        raise AssertionError(
            "lifecycle.py does not classify these tables, so a tenant deletion would "
            "silently leave their rows behind: {}. Add each to TENANT_DIRECT, "
            "TENANT_VIA or GLOBAL_REFERENCE.".format(unclassified))
    stale = sorted(known - actual)
    if stale:
        raise AssertionError(
            "lifecycle.py classifies tables that no longer exist: {}".format(stale))
    overlap = (set(TENANT_DIRECT) & set(TENANT_VIA)) | \
              (set(TENANT_DIRECT) & set(GLOBAL_REFERENCE)) | \
              (set(TENANT_VIA) & set(GLOBAL_REFERENCE))
    if overlap:
        raise AssertionError("tables classified twice: {}".format(sorted(overlap)))


def _org_scoped_tables(metadata) -> List[str]:
    """Tenant tables in FK-SAFE DELETE ORDER (children first).

    Derived from metadata.sorted_tables, which puts parents first — so reversed is
    children first. Nobody maintains an order by hand, and a new FK reorders this
    automatically.
    """
    tenant = set(TENANT_DIRECT) | set(TENANT_VIA)
    return [t.name for t in reversed(metadata.sorted_tables) if t.name in tenant]


def _delete_where(metadata, table_name: str, organisation_id: int):
    """The DELETE statement selecting exactly this org's rows from one table."""
    t = metadata.tables[table_name]
    if table_name in TENANT_DIRECT:
        return delete(t).where(t.c.organisation_id == organisation_id)
    fk_col, parent, parent_pk = TENANT_VIA[table_name]
    p = metadata.tables[parent]
    inner = select(p.c[parent_pk]).where(p.c.organisation_id == organisation_id)
    return delete(t).where(t.c[fk_col].in_(inner))


def _count_where(db: Session, metadata, table_name: str, organisation_id: int) -> int:
    """How many rows this org still has in one table."""
    t = metadata.tables[table_name]
    if table_name in TENANT_DIRECT:
        stmt = select(func.count()).select_from(t).where(
            t.c.organisation_id == organisation_id)
    else:
        fk_col, parent, parent_pk = TENANT_VIA[table_name]
        p = metadata.tables[parent]
        inner = select(p.c[parent_pk]).where(p.c.organisation_id == organisation_id)
        stmt = select(func.count()).select_from(t).where(t.c[fk_col].in_(inner))
    return int(db.execute(stmt).scalar() or 0)


def parent_id_snapshot(db: Session, organisation_id: int) -> Dict[str, List[int]]:
    """{parent table: this org's row ids}, captured BEFORE a deletion.

    The key to verifying a TENANT_VIA table. Those tables have no organisation_id of
    their own and are reached through a parent, so once the parent rows are gone the
    subquery that identifies "this org's children" matches nothing and the child count
    reads zero whether or not any child survived. Holding the parent ids lets the
    after-scan ask the only question that distinguishes the two: are there still rows
    pointing at ids that used to belong to this organisation?
    """
    md = Base.metadata
    parents = {spec[1] for spec in TENANT_VIA.values()}
    snapshot: Dict[str, List[int]] = {}
    for parent in sorted(parents):
        p = md.tables[parent]
        pk = next(spec[2] for spec in TENANT_VIA.values() if spec[1] == parent)
        snapshot[parent] = [
            int(r[0]) for r in db.execute(
                select(p.c[pk]).where(p.c.organisation_id == organisation_id))]
    return snapshot


def orphan_scan(db: Session, organisation_id: int,
                parent_ids: Optional[Dict[str, List[int]]] = None) -> dict:
    """Rows still attributable to this org, per table. Used to VERIFY a deletion.

    A tenant deletion is only done when this is empty. The point is not to trust that
    the delete statements covered everything — it is to re-count and find out.

    `parent_ids` (from parent_id_snapshot, taken before the deletion) is what makes the
    TENANT_VIA half of that verdict real. WITHOUT it those counts are computed through
    the live parent, which is itself deleted, so they are structurally zero and the
    verdict silently covered only 24 of 34 tenant tables while reporting on all of them.
    A verification that cannot observe the thing it verifies is worse than none, because
    its green result gets quoted — so the return now DECLARES its own coverage instead of
    leaving the reader to assume it.
    """
    md = Base.metadata
    remaining = {}
    unverifiable = []
    for name in _org_scoped_tables(md):
        if name in TENANT_VIA and parent_ids is not None:
            fk_col, parent, _pk = TENANT_VIA[name]
            ids = parent_ids.get(parent) or []
            t = md.tables[name]
            n = 0
            # Chunked: a tenant can have more parent ids than SQLite's variable limit.
            for i in range(0, len(ids), 500):
                n += int(db.execute(
                    select(func.count()).select_from(t).where(
                        t.c[fk_col].in_(ids[i:i + 500]))).scalar() or 0)
        elif name in TENANT_VIA:
            n = _count_where(db, md, name, organisation_id)
            unverifiable.append(name)
        else:
            n = _count_where(db, md, name, organisation_id)
        if n:
            remaining[name] = n
    ef = md.tables["emission_factors"]
    own_factors = db.execute(
        select(func.count()).select_from(ef).where(
            ef.c.organisation_id == organisation_id)).scalar() or 0
    if own_factors:
        remaining["emission_factors"] = int(own_factors)
    org_rows = db.query(func.count(Organisation.id)).filter(
        Organisation.id == organisation_id).scalar() or 0
    scanned = _org_scoped_tables(md)
    return {
        "organisation_id": organisation_id, "remaining": remaining,
        "organisation_row_present": bool(org_rows),
        "clean": not remaining and not org_rows,
        "coverage": {
            "tables_scanned": len(scanned) + 1,          # + emission_factors
            "tables_verified_directly": len(scanned) + 1 - len(unverifiable),
            # Named, not just counted: a reader must be able to see WHICH tables the
            # verdict could not speak for, rather than trusting a bare number.
            "tables_not_verifiable": sorted(unverifiable),
            "note": ("Tables listed in tables_not_verifiable are reached through a "
                     "parent that no longer exists, so their counts cannot distinguish "
                     "'no rows' from 'orphaned rows'. Pass parent_ids from "
                     "parent_id_snapshot(), taken before the deletion, to verify them."
                     if unverifiable else
                     "Every org-scoped table was counted against ids known to belong "
                     "to this organisation."),
        },
    }


def delete_tenant(db: Session, organisation_id: int, org_name: str, now: str) -> dict:
    """Delete an organisation and everything belonging to it. IRREVERSIBLE.

    FK-safe by construction (children first, order derived from the metadata), and
    verified rather than trusted: orphan_scan runs afterwards and the result carries its
    verdict, so a caller is never told "deleted" over rows that survived.
    """
    md = Base.metadata

    assert_tables_classified(md)

    # Both captured BEFORE anything is deleted, and both are load-bearing: the counts
    # are what the deletion must account for, and the parent ids are the only way the
    # after-scan can tell an emptied TENANT_VIA table from an orphaned one.
    before = {}
    for name in _org_scoped_tables(md):
        n = _count_where(db, md, name, organisation_id)
        if n:
            before[name] = n
    parent_ids = parent_id_snapshot(db, organisation_id)

    log.warning("tenant deletion starting", extra={"fields": {
        "organisation_id": organisation_id, "organisation_name": org_name,
        "confirmed_at": now, "rows_by_table": before}})

    # Self-referential FKs must be broken before the rows go: PRAGMA foreign_keys=ON
    # checks each row as it is deleted, so a row still referenced by a sibling in the
    # same multi-row DELETE fails. removal_records.reverses_record_id is the only one
    # today; the loop finds any added later rather than naming it.
    self_refs = {}
    for name in _org_scoped_tables(md):
        t = md.tables[name]
        for fk in t.foreign_keys:
            if fk.column.table.name == name and fk.parent.nullable:
                self_refs.setdefault(name, []).append(fk.parent.name)
    for name, cols in self_refs.items():
        t = md.tables[name]
        if name in TENANT_DIRECT:
            where = t.c.organisation_id == organisation_id
        else:
            fk_col, parent, parent_pk = TENANT_VIA[name]
            p = md.tables[parent]
            where = t.c[fk_col].in_(
                select(p.c[parent_pk]).where(p.c.organisation_id == organisation_id))
        db.execute(update(t).where(where).values({c: None for c in cols}))

    deleted = {}
    for name in _org_scoped_tables(md):
        res = db.execute(_delete_where(md, name, organisation_id))
        if res.rowcount:
            deleted[name] = int(res.rowcount)

    # Factors this tenant contributed are theirs and go with them. The global rows
    # (organisation_id IS NULL) are platform reference data and must survive — deleting
    # them would break every other tenant's calculations.
    ef = md.tables["emission_factors"]
    # No need to unbind activities first: they are already deleted above, so nothing
    # references these factor rows any more. Another tenant's rows cannot reference them
    # either — a contributed factor is only visible to its owner (factor_visible_to).
    res = db.execute(delete(ef).where(ef.c.organisation_id == organisation_id))
    if res.rowcount:
        deleted["emission_factors"] = int(res.rowcount)

    org = db.get(Organisation, organisation_id)
    if org is not None:
        db.delete(org)
    db.commit()

    verdict = orphan_scan(db, organisation_id, parent_ids=parent_ids)
    log.warning("tenant deletion complete", extra={"fields": {
        "organisation_id": organisation_id, "organisation_name": org_name,
        "rows_deleted": deleted, "clean": verdict["clean"],
        "remaining": verdict["remaining"]}})
    return {
        "organisation_id": organisation_id, "organisation_name": org_name,
        "confirmed_at": now,
        "rows_before": before, "rows_deleted": deleted,
        "verified_no_orphans": verdict["clean"],
        "remaining": verdict["remaining"],
        # The verdict declares what it was able to check, so "verified" is never taken
        # on trust. With the pre-deletion parent ids this covers every tenant table.
        "verification_coverage": verdict["coverage"],
        "irreversible": True,
    }


# --- portability ------------------------------------------------------------------------

# What a tenant export covers, in dependency order so an importer can replay it. Tables,
# not hand-written queries, so a column added later appears in the export without anyone
# remembering to add it — the same reason the delete plan is derived.
EXPORT_TABLES = (
    "reporting_periods", "reporting_entities", "activities", "activity_findings",
    "mapping_audit_events", "calculation_runs", "emission_line_items",
    "run_entity_boundary", "run_scope3_declarations", "run_screening_statements",
    "run_residual_mix_statements", "run_financed_lines", "run_removal_lines",
    "scope3_category_declarations", "assurance_engagements", "assurance_findings",
    "emissions_targets", "carbon_credits", "removal_records", "market_instruments",
    "granular_certificates", "hourly_loads", "deliverability_links",
    "financed_positions", "product_footprints", "nature_sites",
    "nature_impacts_dependencies", "nature_targets", "lca_assessments", "lca_items",
    "taxonomy_activities", "cbam_goods", "pact_clients",
    # Per-gas masses of the factors this tenant CONTRIBUTED. Exported rather than
    # excluded: the rows under own_emission_factors are incomplete without them — an
    # F-gas or refrigerant factor has NO column-backed mass at all, so exporting the
    # factor alone would hand a churning customer a row whose emissions cannot be
    # reproduced. The TENANT_VIA join scopes this to their own factors; the platform
    # catalogue's gas rows are reference data and stay out, exactly as its factors do.
    "factor_gas_masses",
    # The tenant's own people. A churning customer must be able to see who had access and
    # with what role, and a data subject asking under Art. 15 is answered from here as
    # well as from `activities` — `users.email` and `users.name` are the only columns in
    # the schema that identify a person BY DESIGN rather than incidentally through free
    # text. No credential travels with them: tokens live in `api_tokens`, excluded below.
    "users",
)

# pact_tokens is deliberately NOT exported: the rows are bearer credentials, and an
# export file is copied, mailed and left in buckets. Their absence is stated in the
# payload rather than left for the reader to notice.
EXPORT_EXCLUDED = {
    "pact_tokens": "bearer tokens are credentials, not inventory data",
    # Same reason, and the same doctrine as `pact_tokens`. The hash alone could not be
    # replayed, but an export naming every live credential, its scope and its expiry is a
    # map of the tenant's attack surface, and `NEVER_SERIALISE` would strip the hash
    # while leaving that map intact. The row is excluded whole instead.
    "api_tokens": ("API tokens are credentials, not inventory data; see the users "
                   "export for who had access"),
    "emission_factors": ("factors this tenant contributed are included under "
                         "own_emission_factors; the platform's global catalogue is "
                         "reference data, not tenant data"),
}


def export_tenant(db: Session, organisation_id: int, now: str) -> dict:
    """Everything this tenant owns, as plain JSON-able rows.

    Also the GDPR Art. 15 subject-access substrate: a data subject's rows live in
    `activities` (descriptions) and the site/commuting lines that reference them, so an
    Art. 15 request is answered by filtering this payload rather than by a second
    half-built export path.

    Rows are emitted column-for-column from the table metadata, so this cannot drift out
    of date as the schema grows.
    """
    md = Base.metadata

    assert_tables_classified(md)

    data: Dict[str, list] = {}
    counts: Dict[str, int] = {}
    for name in EXPORT_TABLES:
        t = md.tables[name]
        if name in TENANT_DIRECT:
            stmt = select(t).where(t.c.organisation_id == organisation_id)
        else:
            fk_col, parent, parent_pk = TENANT_VIA[name]
            p = md.tables[parent]
            inner = select(p.c[parent_pk]).where(p.c.organisation_id == organisation_id)
            stmt = select(t).where(t.c[fk_col].in_(inner))
        rows = [safe_row(r._mapping) for r in db.execute(stmt.order_by(t.c.id))]
        data[name] = rows
        counts[name] = len(rows)

    ef = md.tables["emission_factors"]
    own = [safe_row(r._mapping) for r in db.execute(
        select(ef).where(ef.c.organisation_id == organisation_id).order_by(ef.c.id))]
    data["own_emission_factors"] = own
    counts["own_emission_factors"] = len(own)

    org = db.get(Organisation, organisation_id)
    org_row = safe_row(
        {c.name: getattr(org, c.name) for c in md.tables["organisations"].columns}
    ) if org is not None else None

    redacted = db.query(func.count(ActivityRecord.id)).filter(
        ActivityRecord.organisation_id == organisation_id,
        ActivityRecord.redacted_at.isnot(None)).scalar() or 0

    return {
        "export_version": 1,
        "exported_at": now,
        "organisation": org_row,
        "counts": counts,
        "redacted_activities": int(redacted),
        "excluded": EXPORT_EXCLUDED,
        "notes": [
            "Credential columns are omitted ({}): they are secrets, not inventory, and "
            "an export file gets copied and mailed. The plaintext API key was never "
            "stored at all.".format(", ".join(sorted(NEVER_SERIALISE))),
            "Rows whose free text was erased carry a [redacted ...] tombstone and a "
            "redacted_at timestamp; redacted_activities counts them, so a reader can "
            "tell erased text from text that was never recorded.",
            "The platform's global emission factor catalogue is NOT included: it is "
            "reference data shared by every tenant. Figures already computed are "
            "reproducible from the frozen `details` on each emission line item.",
        ],
        "data": data,
    }


def export_json(db: Session, organisation_id: int, now: str) -> str:
    """The export as a JSON string, keys sorted so two exports of unchanged data are
    byte-identical and can be diffed."""
    return json.dumps(export_tenant(db, organisation_id, now),
                      sort_keys=True, default=str, indent=2)


# --- read-back --------------------------------------------------------------------------

def paginate(query, limit: int, offset: int) -> dict:
    """One page plus whether more remain.

    Deliberately the same shape as services/pact_host.list_footprints — limit/offset in,
    rows + has_more + next_offset out — so every list endpoint in the platform answers
    the "is there more?" question the same way.

    Counts with COUNT(*) rather than len() on a fetched list: these tables grow to the
    size of an upload, and materialising all of them to serve 50 rows is the bug
    pagination exists to avoid.
    """
    total = query.order_by(None).count()
    rows = query.limit(limit).offset(offset).all()
    return {"rows": rows, "total_matched": total,
            "has_more": (offset + limit) < total,
            "next_offset": offset + limit}
