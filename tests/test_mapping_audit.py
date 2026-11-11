"""The append-only journal of factor-binding decisions.

Closes two of the six gaps the evidence pack declares about itself — the override
log with before/after values, and the decision timestamp. It does NOT close
reviewer identity, and the tests assert that it keeps saying so.
"""
import pytest

from app.models import (
    ActivityRecord, EmissionFactor, MappingAuditEvent, Organisation,
)
from app.services.mapping_audit import (
    ACTIONS, binding_as_at, history, record, summary,
)

_SEQ = [0]


def _org(db):
    _SEQ[0] += 1
    o = Organisation(name=f"AuditOrg{_SEQ[0]}")
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, value=0.2):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="electricity", subcategory="", unit="kWh",
                       gwp_set="AR6", value=value, method_type="average_data")
    db.add(f); db.commit(); db.refresh(f)
    return f


def _act(db, org, factor=None, status="auto"):
    a = ActivityRecord(organisation_id=org.id, date="2027-01-01",
                       category="electricity", subcategory="", description="d",
                       quantity=100.0, unit="kWh", geo="GB",
                       factor_id=(factor.id if factor else None),
                       mapping_status=status, mapping_basis="exact")
    db.add(a); db.commit(); db.refresh(a)
    return a


def test_a_decision_is_journalled_with_before_and_after(db):
    org = _org(db)
    old, new = _factor(db, 0.2), _factor(db, 0.9)
    a = _act(db, org, old)
    prev = a.factor_id
    a.factor_id = new.id
    a.mapping_status = "overridden"
    record(db, a, "overridden", from_factor_id=prev, from_status="auto")
    e = db.query(MappingAuditEvent).one()
    assert e.from_factor_id == old.id and e.to_factor_id == new.id
    assert e.from_status == "auto" and e.to_status == "overridden"
    assert e.at


@pytest.mark.parametrize("action", ACTIONS)
def test_every_declared_action_is_journalled(action, db):
    org = _org(db)
    a = _act(db, org, _factor(db))
    assert record(db, a, action) is not None


def test_an_unknown_action_is_ignored_never_guessed(db):
    org = _org(db)
    a = _act(db, org, _factor(db))
    assert record(db, a, "vibes") is None
    assert db.query(MappingAuditEvent).count() == 0


def test_the_journal_is_append_only(db):
    """No update path and no status column to flip."""
    cols = {c.name for c in MappingAuditEvent.__table__.columns}
    assert "status" not in cols
    assert not any(c.startswith("resolved") for c in cols)


def test_history_is_oldest_first(db):
    org = _org(db)
    a = _act(db, org, _factor(db))
    for act in ("auto_mapped", "suggested", "approved"):
        record(db, a, act)
    assert [e["action"] for e in history(db, org.id, a.id)] == [
        "auto_mapped", "suggested", "approved"]


def test_binding_as_at_answers_what_it_was_then(db):
    """The question an assuror asks, and the one an in-place status column cannot
    answer."""
    org = _org(db)
    old, new = _factor(db, 0.2), _factor(db, 0.9)
    a = _act(db, org, old)
    record(db, a, "auto_mapped", now="2027-01-01T00:00:00Z")
    a.factor_id = new.id
    record(db, a, "overridden", from_factor_id=old.id, now="2027-06-01T00:00:00Z")

    early = binding_as_at(db, a.id, "2027-03-01T00:00:00Z")
    late = binding_as_at(db, a.id, "2027-09-01T00:00:00Z")
    assert early["factor_id"] == old.id
    assert late["factor_id"] == new.id


def test_a_time_before_any_decision_is_unknown_not_unmapped(db):
    org = _org(db)
    a = _act(db, org, _factor(db))
    record(db, a, "auto_mapped", now="2027-06-01T00:00:00Z")
    r = binding_as_at(db, a.id, "2027-01-01T00:00:00Z")
    assert r["determinable"] is False
    assert "not the same as unmapped" in r["reason"]


def test_the_summary_names_what_it_closes_and_what_it_does_not(db):
    org = _org(db)
    a = _act(db, org, _factor(db))
    record(db, a, "overridden")
    s = summary(db, org.id)
    assert "Override log with before/after values" in s["closes_evidence_gaps"]
    assert "Reviewer timestamp" in s["closes_evidence_gaps"]
    # UNATTRIBUTED, because `record` above named no actor: the gap is still declared,
    # and declared as historical rather than absolute. The old assertion here was
    # `gap == "Reviewer identity"` with "no concept of a person" in the reason, which
    # was true while no actor could exist. It can now, so the honest claim is narrower,
    # not gone.
    gap = s["does_not_close"][0]
    # Label is plain "Reviewer identity": the gap is no longer only about historical
    # events, since a decision made TODAY with the shared organisation key also names
    # nobody. The `why` carries the breakdown.
    assert gap["gap"] == "Reviewer identity"
    assert "misleading answer" in gap["why"]
    assert s["attribution"]["unattributed"] == 1
    assert s["attribution"]["attributed"] == 0
    assert "Reviewer identity" not in s["closes_evidence_gaps"]


def test_an_attributed_decision_closes_the_identity_gap(db):
    """The other half, without which the test above would pass over a product where
    attribution never works at all."""
    org = _org(db)
    a = _act(db, org, _factor(db))
    record(db, a, "overridden", actor="user:preparer@acme.test")
    s = summary(db, org.id)
    assert "Reviewer identity" in s["closes_evidence_gaps"]
    assert s["does_not_close"] == []
    assert s["attribution"]["attributed"] == 1
    assert s["attribution"]["by_a_named_person"] == 1


def test_one_unattributed_decision_reopens_it(db):
    """A journal is only as attributed as its weakest event. If a single historical
    decision names nobody, the pack must not say the control is closed."""
    org = _org(db)
    a = _act(db, org, _factor(db))
    record(db, a, "overridden", actor="user:preparer@acme.test")
    record(db, a, "approved")                      # historical: no actor
    s = summary(db, org.id)
    assert "Reviewer identity" not in s["closes_evidence_gaps"]
    assert s["does_not_close"][0]["gap"] == "Reviewer identity"
    assert s["attribution"] == {
        **s["attribution"], "attributed": 1, "unattributed": 1}


def test_an_empty_journal_does_not_close_the_gap_by_having_nothing_in_it(db):
    """The empty-collection pass. Zero human decisions must not read as "all of them
    are attributed" — this codebase has found that exact shape in its own gates."""
    org = _org(db)
    a = _act(db, org, _factor(db))
    record(db, a, "auto_mapped", actor="engine")   # machine, not a reviewer
    s = summary(db, org.id)
    assert s["human_decisions"] == 0
    assert "Reviewer identity" not in s["closes_evidence_gaps"]
    assert "nothing to attribute" in s["does_not_close"][0]["why"]


def test_the_resolver_attributes_its_own_bindings(db):
    """Through the REAL auto-mapping path, not by calling `record` with the answer.

    The first version of this test passed `actor="engine"` to `record` directly, so it
    asserted that the journal can store the string — and deleting `actor="engine"` from
    `services/resolver.py` left it green. A test that supplies the value it is checking
    for tests nothing about the code that should supply it.
    """
    from app.services.resolver import auto_map_activity
    org = _org(db)
    f = _factor(db)
    a = _act(db, org, None)
    a.factor_id = None
    a.mapping_status = "unmapped"
    db.commit()

    auto_map_activity(db, a)
    db.commit()

    evs = summary(db, org.id)
    from app.models import MappingAuditEvent
    rows = db.query(MappingAuditEvent).filter(
        MappingAuditEvent.organisation_id == org.id).all()
    assert rows, f"the resolver journalled nothing: {evs}"
    assert {r.actor for r in rows} == {"engine"}, {r.actor for r in rows}
    # And a machine binding is not counted as a reviewer decision.
    assert evs["human_decisions"] == 0


def test_a_machine_binding_is_not_an_unknown_one(db):
    """NULL and 'engine' are different facts. An assuror reading an unattributed event
    must be able to tell "nobody knows who" from "no human was involved"."""
    org = _org(db)
    a = _act(db, org, _factor(db))
    e1 = record(db, a, "auto_mapped", actor="engine")
    e2 = record(db, a, "approved")
    assert e1.actor == "engine"
    assert e2.actor is None


def test_human_decisions_are_counted_separately(db):
    org = _org(db)
    a = _act(db, org, _factor(db))
    record(db, a, "auto_mapped")
    record(db, a, "approved")
    record(db, a, "overridden")
    assert summary(db, org.id)["human_decisions"] == 2


def test_the_journal_survives_a_retired_factor(db):
    """Factor ids are provenance and are never joined back."""
    org = _org(db)
    f = _factor(db)
    a = _act(db, org, f)
    record(db, a, "auto_mapped", from_factor_id=f.id)
    a.factor_id = None
    db.commit()
    db.query(EmissionFactor).filter(EmissionFactor.id == f.id).delete()
    db.commit()
    assert history(db, org.id, a.id)[0]["from_factor_id"] == f.id


# --- endpoints ----------------------------------------------------------------

@pytest.fixture
def env():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from fastapi.testclient import TestClient
    from app.database import Base
    from app import main as main_mod

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    def override_get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    main_mod.app.dependency_overrides[main_mod.get_db] = override_get_db
    client = TestClient(main_mod.app)
    key = client.post("/organisations", params={"name": "A"}).json()["api_key"]
    hdr = {"X-API-Key": key}

    seed = TestingSession()
    org = seed.query(Organisation).filter(Organisation.name == "A").one()
    f1, f2 = _factor(seed, 0.2), _factor(seed, 0.9)
    a = _act(seed, org, f1)
    ids = (a.id, f1.id, f2.id)
    seed.close()
    yield client, hdr, ids
    main_mod.app.dependency_overrides.clear()


def test_the_override_endpoint_journals(env):
    client, hdr, (aid, f1, f2) = env
    r = client.post(f"/mappings/{aid}/override", headers=hdr,
                    params={"factor_id": f2})
    assert r.status_code == 200
    body = client.get("/mappings/audit", headers=hdr).json()
    assert body["events"]
    e = body["events"][-1]
    assert e["action"] == "overridden"
    assert e["from_factor_id"] == f1 and e["to_factor_id"] == f2


def test_the_as_at_endpoint_is_scoped_to_the_organisation(env):
    client, hdr, (aid, _, _) = env
    other = client.post("/organisations", params={"name": "B"}).json()["api_key"]
    assert client.get("/mappings/audit/as_at", headers={"X-API-Key": other},
                      params={"activity_id": aid,
                              "at": "2027-01-01T00:00:00Z"}).status_code == 404


def test_the_audit_appears_in_the_evidence_pack(env):
    from app.services.evidence_pack import _SECTION_ORDER
    client, hdr, (aid, _, f2) = env
    client.post(f"/mappings/{aid}/override", headers=hdr, params={"factor_id": f2})
    client.post("/calculate/run", headers=hdr)
    body = client.get("/assurance/evidence_pack", headers=hdr,
                      params={"uncertainty_iterations": 1000}).json()
    assert "12_mapping_audit" in _SECTION_ORDER
    sec = body["sections"]["12_mapping_audit"]
    assert sec["events"]
    assert "in-place status column" in sec["note"]


def test_the_pack_now_reports_three_gaps_as_closed(env):
    """Reviewer identity joins the two the journal already closed.

    It was previously asserted here that identity "cannot be closed by a journal", which
    was right: a journal records WHAT changed and when, and the actor has to come from
    authentication. It now does — the override below goes through an authenticated
    endpoint, so the event names the principal that made it. The pack MEASURES that
    rather than asserting it, so a journal with unattributed history still reports the
    gap (see `test_the_summary_names_what_it_closes_and_what_it_does_not`).
    """
    client, hdr, (aid, _, f2) = env

    # THE OVERRIDE IS MADE BY A NAMED PERSON, and the first version of this test did not
    # do that — it used the organisation key, which names nobody. It passed, because
    # `attributed` then counted any truthy actor, so the shared tenant secret closed the
    # ISSA 5000 "who made each judgement" control. The test was pinning the misleading
    # verdict it was written to prevent. An adversarial review of this very commit found
    # it, and the negative control below is the half that was missing.
    uid = client.post("/users", json={"email": "preparer@a.test", "role": "member"},
                      headers=hdr).json()["id"]
    tok = client.post("/tokens", headers=hdr,
                      params={"label": "preparer", "scopes": "write",
                              "user_id": uid}).json()["api_key"]
    person = {"X-API-Key": tok}

    client.post(f"/mappings/{aid}/override", headers=person, params={"factor_id": f2})
    client.post("/calculate/run", headers=hdr)
    gaps = client.get("/assurance/evidence_pack", headers=hdr,
                      params={"uncertainty_iterations": 1000}).json()["evidence_gaps"]
    by_item = {g["item"]: g for g in gaps}
    assert "CLOSED" in by_item["Override log with before/after values"]["why_absent"]
    assert "CLOSED" in by_item["Reviewer timestamp"]["why_absent"]
    identity = by_item["Reviewer identity"]["why_absent"]
    assert "CLOSED" in identity, identity
    assert "OPEN" not in identity, identity
    # And the journal names the credential, not merely that someone acted.
    j = client.get("/mappings/audit", headers=hdr, params={"activity_id": aid}).json()
    actors = {e["actor"] for e in j["events"]}
    assert "user:preparer@a.test" in actors, actors
    assert j["summary"]["attribution"]["by_a_named_person"] == 1


def test_the_same_override_by_the_shared_key_leaves_the_gap_open(env):
    """The negative control for the test above, and the one that was missing.

    Identical in every way except WHO made the override: the tenant's shared
    organisation key instead of a named person's token. If the pack reports CLOSED here
    too, it is not measuring attribution at all — it is reporting that a journal exists.
    """
    client, hdr, (aid, _, f2) = env
    client.post(f"/mappings/{aid}/override", headers=hdr, params={"factor_id": f2})
    client.post("/calculate/run", headers=hdr)
    gaps = client.get("/assurance/evidence_pack", headers=hdr,
                      params={"uncertainty_iterations": 1000}).json()["evidence_gaps"]
    identity = {g["item"]: g for g in gaps}["Reviewer identity"]["why_absent"]
    assert "OPEN" in identity, identity
    assert "shared organisation key" in identity, identity


# --- what the adversarial review of PR #114 found ----------------------------------------

def test_the_shared_organisation_key_is_not_a_reviewer(db):
    """BLOCKER, live on main before this fix.

    `attributed` counted any truthy actor, so 'org_key' — the tenant's SHARED secret,
    which #113 deliberately keeps alive as the bootstrap and legacy-integration path —
    closed the ISSA 5000 "who made each judgement" control. The pack served
    "CLOSED ... all N human decisions name the principal that made them" while reporting
    "(0 a named person)" in the same sentence.

    That is verbatim the claim the docstring it replaced existed to refuse: "recording
    the organisation as though it were a reviewer would turn an honest gap into a
    misleading answer."
    """
    org = _org(db)
    a = _act(db, org, _factor(db))
    record(db, a, "overridden", actor="org_key")
    s = summary(db, org.id)
    assert "Reviewer identity" not in s["closes_evidence_gaps"]
    assert s["attribution"]["by_a_named_person"] == 0
    assert s["attribution"]["by_an_impersonal_credential"] == 1
    assert s["attribution"]["unattributed"] == 1


def test_a_token_naming_no_person_is_not_a_reviewer_either(db):
    """`token:<id>` is a credential issued to a FIRM rather than an individual — the
    assuror case #113 added it for. It names nobody, so it cannot answer "who"."""
    org = _org(db)
    a = _act(db, org, _factor(db))
    record(db, a, "overridden", actor="token:7")
    s = summary(db, org.id)
    assert "Reviewer identity" not in s["closes_evidence_gaps"]
    assert s["attribution"]["by_an_impersonal_credential"] == 1


def test_the_three_kinds_of_unattributed_stay_distinguishable(db):
    """NULL, an impersonal credential and a named person are three different facts, and
    collapsing any two of them is how an honest gap becomes a misleading answer."""
    org = _org(db)
    a = _act(db, org, _factor(db))
    record(db, a, "overridden", actor="user:p@acme.test")
    record(db, a, "approved", actor="org_key")
    record(db, a, "pact_bound")                       # historical: no actor
    att = summary(db, org.id)["attribution"]
    assert att["by_a_named_person"] == 1
    assert att["by_an_impersonal_credential"] == 1
    assert att["no_actor_recorded"] == 1
    assert att["unattributed"] == 2
    gap = summary(db, org.id)["does_not_close"][0]
    assert "shared organisation key" in gap["why"]
    assert "name no actor at all" in gap["why"]
