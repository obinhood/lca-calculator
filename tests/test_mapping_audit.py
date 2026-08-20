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
    gap = s["does_not_close"][0]
    assert gap["gap"] == "Reviewer identity"
    assert "no concept of a person" in gap["why"]
    assert "misleading answer" in gap["why"]


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


def test_the_pack_now_reports_two_gaps_as_closed(env):
    client, hdr, (aid, _, f2) = env
    client.post(f"/mappings/{aid}/override", headers=hdr, params={"factor_id": f2})
    client.post("/calculate/run", headers=hdr)
    gaps = client.get("/assurance/evidence_pack", headers=hdr,
                      params={"uncertainty_iterations": 1000}).json()["evidence_gaps"]
    by_item = {g["item"]: g for g in gaps}
    assert "CLOSED" in by_item["Override log with before/after values"]["why_absent"]
    assert "CLOSED" in by_item["Reviewer timestamp"]["why_absent"]
    # And reviewer identity is STILL open — it cannot be closed by a journal.
    assert "CLOSED" not in by_item["Reviewer identity"]["why_absent"]
