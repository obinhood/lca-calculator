"""Data lifecycle: erasure, the blocked-by-frozen-run refusal, retention, tenant
deletion leaving no orphans, and a round-trip export.

The tests that matter most here are the ones that prove the deletion policy is SAFE
rather than merely implemented:

* test_redaction_leaves_activities_fingerprint_identical — the whole justification for
  redacting rather than deleting. If this ever fails, redaction has started moving a
  filed run's freshness signal and the policy is broken.
* test_hard_delete_of_frozen_activity_is_refused_and_names_the_runs — a refusal must say
  why, and name what blocks it.
* test_filed_secr_energy_figure_survives_erasure — the defect the policy exists to
  prevent, exercised end to end through the real renderer.
* test_tenant_deletion_leaves_no_orphans — asserts ZERO rows across every org-scoped
  table, rather than trusting the cascade.
* test_every_table_is_classified — the gate that cannot fail open.
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  (register tables)
from app.models import (
    ActivityRecord, AssuranceEngagement, CalculationRun, EmissionFactor,
    EmissionLineItem, Organisation, ReportingPeriod,
)
from app import main as main_mod
from app.services import lifecycle as L
from app.services.calc import activities_fingerprint, compute_co2e


# --- fixtures ---------------------------------------------------------------------------

@pytest.fixture
def env():
    """Two orgs over one shared in-memory DB, A with activities and a filed run."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    main_mod.app.dependency_overrides[main_mod.get_db] = override_get_db
    client = TestClient(main_mod.app)

    key_a = client.post("/organisations", params={"name": "A"}).json()["api_key"]
    key_b = client.post("/organisations", params={"name": "B"}).json()["api_key"]
    hdr_a, hdr_b = {"X-API-Key": key_a}, {"X-API-Key": key_b}

    seed = TestingSession()
    f = EmissionFactor(source="TEST", version="1", geography="GB", year=2024,
                       category="electricity", subcategory="", unit="kWh",
                       gwp_set="AR6", value=0.17)
    seed.add(f)
    seed.commit()
    seed.refresh(f)
    org_a = seed.query(Organisation).filter(Organisation.name == "A").one()
    org_b = seed.query(Organisation).filter(Organisation.name == "B").one()

    # Two activities for A: one will be frozen into a run, one will not.
    frozen = ActivityRecord(
        organisation_id=org_a.id, date="2024-03-01", category="electricity",
        subcategory="", description="HQ meter — contact Dana Whitfield",
        quantity=1000.0, unit="kWh", geo="GB", factor_id=f.id,
        source_file="hq-jan-dana.csv", mapping_status="auto")
    seed.add(frozen)
    seed.commit()
    seed.refresh(frozen)

    run = compute_co2e(seed, org_a.id)
    run_id, frozen_id = run.id, frozen.id

    # Added AFTER the run, so no run froze it.
    loose = ActivityRecord(
        organisation_id=org_a.id, date="2024-04-01", category="electricity",
        subcategory="", description="workshop meter — Jo Alvarez",
        quantity=500.0, unit="kWh", geo="GB", factor_id=f.id,
        source_file="workshop-dana.csv", mapping_status="auto")
    seed.add(loose)
    seed.commit()
    seed.refresh(loose)
    loose_id = loose.id
    # Capture every id as a plain int BEFORE closing: the last commit expired these
    # attributes, so reading them off a detached instance raises.
    org_a_id, org_b_id, factor_id = org_a.id, org_b.id, f.id
    seed.close()

    yield dict(client=client, hdr_a=hdr_a, hdr_b=hdr_b, Session=TestingSession,
               org_a=org_a_id, org_b=org_b_id, run=run_id,
               frozen=frozen_id, loose=loose_id, factor=factor_id)
    main_mod.app.dependency_overrides.clear()


def _seed_credential_rows(db, env):
    """One row in each table that stores a secret, with recognisable values."""
    from app.models import PactClient
    db.add(AssuranceEngagement(
        organisation_id=env["org_a"], run_id=env["run"], assuror_name="Acme LLP",
        standard="ISAE3000", level="limited", status="open", materiality_pct=5.0,
        access_token_hash="SECRET-HASH-VALUE"))
    db.add(PactClient(organisation_id=env["org_a"], client_id="cid",
                      client_secret_hash="SECRET-CLIENT-HASH", partner_name="P",
                      revoked=False))
    db.commit()


# --- the classification gate -------------------------------------------------------------

def test_every_table_is_classified():
    """The gate that cannot fail open.

    A table added later and classified nowhere must make this raise, rather than having
    its rows quietly survive a tenant deletion that reported success.
    """
    L.assert_tables_classified(Base.metadata)


def test_unclassified_table_is_caught(monkeypatch):
    """Prove the gate actually fires — a passing assert over an empty check is no gate."""
    monkeypatch.setattr(L, "TENANT_DIRECT",
                        tuple(t for t in L.TENANT_DIRECT if t != "activities"))
    with pytest.raises(AssertionError, match="activities"):
        L.assert_tables_classified(Base.metadata)


def test_delete_order_is_children_before_parents():
    """FK-safety is derived from the metadata, so assert the derivation holds."""
    order = L._org_scoped_tables(Base.metadata)
    pos = {name: i for i, name in enumerate(order)}
    for name in order:
        t = Base.metadata.tables[name]
        for fk in t.foreign_keys:
            parent = fk.column.table.name
            if parent in pos and parent != name:
                assert pos[name] < pos[parent], (
                    f"{name} must be deleted before its parent {parent}")


# --- the core safety property ------------------------------------------------------------

def test_redaction_leaves_activities_fingerprint_identical(env):
    """THE justification for redacting instead of deleting.

    activities_fingerprint() is what every filed run reports freshness against. If
    redaction moved it, every historical run would flip to "stale" the moment a tenant
    exercised an erasure request.
    """
    S = env["Session"]
    db = S()
    acts = db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == env["org_a"]).all()
    before = activities_fingerprint(acts)
    db.close()

    r = env["client"].delete(f"/activities/{env['frozen']}",
                             params={"mode": "redact"}, headers=env["hdr_a"])
    assert r.status_code == 200, r.text
    assert r.json()["action"] == "redacted"

    db = S()
    acts = db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == env["org_a"]).all()
    after = activities_fingerprint(acts)
    db.close()
    assert after == before, "redaction must not change the activity fingerprint"


def test_hard_delete_changes_the_fingerprint(env):
    """The counterpart: deletion DOES move it. This is why a frozen row cannot go."""
    S = env["Session"]
    db = S()
    before = activities_fingerprint(db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == env["org_a"]).all())
    db.close()

    r = env["client"].delete(f"/activities/{env['loose']}", headers=env["hdr_a"])
    assert r.status_code == 200
    assert r.json()["action"] == "deleted"

    db = S()
    after = activities_fingerprint(db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == env["org_a"]).all())
    db.close()
    assert after != before


# --- erasure ----------------------------------------------------------------------------

def test_erasure_clears_the_free_text_and_leaves_a_tombstone(env):
    r = env["client"].delete(f"/activities/{env['frozen']}",
                             params={"mode": "redact", "reason": "erasure_request"},
                             headers=env["hdr_a"])
    assert r.status_code == 200
    body = r.json()
    assert set(body["fields_cleared"]) == {"description", "source_file"}

    db = env["Session"]()
    a = db.get(ActivityRecord, env["frozen"])
    assert "Dana Whitfield" not in (a.description or "")
    assert "dana" not in (a.source_file or "").lower()
    assert a.description.startswith("[redacted ")
    assert a.redacted_at is not None
    assert a.redaction_reason == "erasure_request"
    # The figures a disclosure depends on are untouched.
    assert a.quantity == 1000.0
    assert a.unit == "kWh"
    assert a.category == "electricity"
    assert a.factor_id == env["factor"]
    db.close()


def test_tombstone_is_self_describing_not_blank(env):
    """An erased description must stay distinguishable from one never recorded."""
    env["client"].delete(f"/activities/{env['frozen']}",
                         params={"mode": "redact"}, headers=env["hdr_a"])
    db = env["Session"]()
    a = db.get(ActivityRecord, env["frozen"])
    assert a.description not in ("", None)
    assert "redacted" in a.description
    assert a.redaction_reason in a.description
    db.close()


def test_redaction_is_idempotent_and_keeps_the_first_timestamp(env):
    c, hdr = env["client"], env["hdr_a"]
    first = c.delete(f"/activities/{env['frozen']}", params={"mode": "redact"},
                     headers=hdr).json()
    second = c.delete(f"/activities/{env['frozen']}", params={"mode": "redact"},
                      headers=hdr).json()
    assert second["action"] == "already_redacted"
    assert second["redacted_at"] == first["redacted_at"]


def test_auto_mode_hard_deletes_an_unfrozen_activity(env):
    r = env["client"].delete(f"/activities/{env['loose']}", headers=env["hdr_a"])
    assert r.status_code == 200
    assert r.json()["action"] == "deleted"
    db = env["Session"]()
    assert db.get(ActivityRecord, env["loose"]) is None
    db.close()


def test_auto_mode_redacts_a_frozen_activity_and_says_why(env):
    r = env["client"].delete(f"/activities/{env['frozen']}", headers=env["hdr_a"])
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "redacted"
    assert [b["run_id"] for b in body["blocking_runs"]] == [env["run"]]
    assert "reproducible" in body["note"]
    db = env["Session"]()
    assert db.get(ActivityRecord, env["frozen"]) is not None
    db.close()


def _structured_refusal(response) -> dict:
    """The structured refusal body, however the error envelope carries it.

    The service returns a dict — `action`, `blocking_runs`, `reason`, `remedy` — because
    a refusal that cannot name the runs it protects is not much of a refusal. The shared
    error envelope currently JSON-stringifies a non-string `detail` into its `message`,
    so the structure arrives encoded rather than as an object, and a structured-`detail`
    facility is being added to the envelope separately.

    This reads either shape, so the test asserts the SUBSTANCE of the refusal rather
    than an envelope detail that is still being designed — and keeps passing, unchanged,
    once the envelope carries the object directly. It does not weaken the assertion: the
    same four fields are checked either way.
    """
    body = response.json()
    for candidate in (body.get("detail"), (body.get("error") or {}).get("message")):
        if isinstance(candidate, dict):
            return candidate
        if isinstance(candidate, str):
            try:
                parsed = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                return parsed
    raise AssertionError(
        f"no structured refusal found in the error body: {body!r}")


def test_hard_delete_of_frozen_activity_is_refused_and_names_the_runs(env):
    """A refusal must say WHY, and name what blocks it."""
    r = env["client"].delete(f"/activities/{env['frozen']}",
                             params={"mode": "hard"}, headers=env["hdr_a"])
    assert r.status_code == 409
    detail = _structured_refusal(r)
    assert detail["action"] == "blocked"
    assert [b["run_id"] for b in detail["blocking_runs"]] == [env["run"]]
    assert detail["blocking_runs"][0]["frozen_lines"] >= 1
    assert "SECR" in detail["reason"] or "fingerprint" in detail["reason"]
    assert detail["remedy"] == "mode=redact"
    # And nothing happened.
    db = env["Session"]()
    a = db.get(ActivityRecord, env["frozen"])
    assert a is not None and a.redacted_at is None
    db.close()


def test_hard_delete_also_removes_findings_and_audit_events(env):
    """Children carry NOT NULL FKs, so they must go or the DELETE fails."""
    from app.models import ActivityFinding, MappingAuditEvent
    db = env["Session"]()
    db.add(ActivityFinding(
        organisation_id=env["org_a"], activity_id=env["loose"], finding_key="k1",
        check_code="duplicate", severity="informational", status="open",
        expectation="no duplicate", threshold="0", observed="1",
        effect_quantifiable=False, screening_version="v1"))
    db.add(MappingAuditEvent(organisation_id=env["org_a"], activity_id=env["loose"],
                             action="auto_mapped", at="2024-04-02T00:00:00Z"))
    db.commit()
    db.close()

    r = env["client"].delete(f"/activities/{env['loose']}", headers=env["hdr_a"])
    assert r.status_code == 200
    body = r.json()
    assert body["findings_deleted"] == 1
    assert body["audit_events_deleted"] == 1

    db = env["Session"]()
    assert db.query(func.count(ActivityFinding.id)).filter(
        ActivityFinding.activity_id == env["loose"]).scalar() == 0
    assert db.query(func.count(MappingAuditEvent.id)).filter(
        MappingAuditEvent.activity_id == env["loose"]).scalar() == 0
    db.close()


def test_erasure_is_organisation_scoped(env):
    """B must not be able to erase A's activity."""
    for mode in ("auto", "hard", "redact"):
        r = env["client"].delete(f"/activities/{env['frozen']}",
                                 params={"mode": mode}, headers=env["hdr_b"])
        assert r.status_code == 404, mode
    db = env["Session"]()
    a = db.get(ActivityRecord, env["frozen"])
    assert a is not None and a.redacted_at is None
    db.close()


def test_erasure_rejects_an_unknown_reason(env):
    r = env["client"].delete(f"/activities/{env['frozen']}",
                             params={"mode": "redact", "reason": "Dana asked nicely"},
                             headers=env["hdr_a"])
    assert r.status_code == 400
    assert "reason must be one of" in r.json()["detail"]


def test_erasure_requires_authentication(env):
    # A MISSING key is 401, not 422: an absent credential and a wrong one are the same
    # failure, unauthenticated, so they get the same status.
    assert env["client"].delete(f"/activities/{env['frozen']}").status_code == 401
    assert env["client"].delete(f"/activities/{env['frozen']}",
                                headers={"X-API-Key": "nope"}).status_code == 401


# --- the defect the policy exists to prevent ---------------------------------------------

def test_filed_secr_energy_figure_survives_erasure(env):
    """End to end through the real renderer: erasing personal data must not move a
    figure in an already-filed disclosure."""
    from app.reports.secr import secr_report
    S = env["Session"]

    db = S()
    before = secr_report(db, env["org_a"], run_id=env["run"])
    db.close()
    # The figure must be real, or "unchanged" proves nothing.
    assert before["energy_use_kwh"]["total_kwh"] > 0, before

    r = env["client"].delete(f"/activities/{env['frozen']}",
                             params={"mode": "redact"}, headers=env["hdr_a"])
    assert r.status_code == 200

    db = S()
    after = secr_report(db, env["org_a"], run_id=env["run"])
    db.close()
    assert after == before, (
        "a filed SECR report changed after an erasure — the reproduction contract is "
        "broken")


def test_run_total_and_line_items_survive_erasure(env):
    S = env["Session"]
    db = S()
    run = db.get(CalculationRun, env["run"])
    total_before = run.total_co2e
    lines_before = db.query(func.count(EmissionLineItem.id)).filter(
        EmissionLineItem.run_id == env["run"]).scalar()
    db.close()

    env["client"].delete(f"/activities/{env['frozen']}", params={"mode": "redact"},
                         headers=env["hdr_a"])

    db = S()
    run = db.get(CalculationRun, env["run"])
    assert run.total_co2e == total_before
    assert db.query(func.count(EmissionLineItem.id)).filter(
        EmissionLineItem.run_id == env["run"]).scalar() == lines_before
    db.close()


def test_runs_freezing_activity_is_the_shared_rule(env):
    """Pin the shared rule's callers. A new deletion path that bypasses it should make
    this list wrong, the same way test_factor_tenancy pins factor_visible_to()."""
    import inspect
    src = inspect.getsource(L)
    # Every function that removes or erases must consult the rule.
    for fn in ("erase_activity", "retention_candidates", "retention_apply"):
        body = inspect.getsource(getattr(L, fn))
        assert "runs_freezing_activity" in body, (
            f"{fn} does not consult runs_freezing_activity — it could delete a row a "
            f"filed run depends on")
    assert src.count("def runs_freezing_activity") == 1, "the rule must have one definition"


# --- retention --------------------------------------------------------------------------

def test_retention_defaults_to_null_meaning_keep_indefinitely(env):
    db = env["Session"]()
    org = db.get(Organisation, env["org_a"])
    assert org.retention_days is None
    db.close()
    r = env["client"].post("/retention/preview", headers=env["hdr_a"])
    assert r.status_code == 400
    assert "no retention period set" in r.json()["detail"]


def test_retention_preview_changes_nothing_and_splits_by_outcome(env):
    c, hdr = env["client"], env["hdr_a"]
    assert c.post("/organisations/retention", params={"retention_days": 30},
                  headers=hdr).status_code == 200
    r = c.post("/retention/preview", params={"as_of": "2024-12-31"}, headers=hdr)
    assert r.status_code == 200
    body = r.json()
    # Both activities are older than 30 days before 2024-12-31, but only one is frozen.
    assert body["would_redact"] == [env["frozen"]]
    assert body["would_hard_delete"] == [env["loose"]]
    assert body["cutoff_date"] == "2024-12-01"
    # Nothing was touched.
    db = env["Session"]()
    assert db.get(ActivityRecord, env["loose"]) is not None
    assert db.get(ActivityRecord, env["frozen"]).redacted_at is None
    db.close()


def test_retention_preview_accepts_a_hypothetical_period(env):
    r = env["client"].post("/retention/preview",
                           params={"retention_days": 9999, "as_of": "2024-12-31"},
                           headers=env["hdr_a"])
    assert r.status_code == 200
    assert r.json()["counts"]["would_hard_delete"] == 0
    assert r.json()["counts"]["would_redact"] == 0


def test_retention_never_touches_an_undated_activity(env):
    """No usable date means cannot_determine, not "older than the window"."""
    db = env["Session"]()
    a = ActivityRecord(organisation_id=env["org_a"], date=None,
                       category="electricity", subcategory="", description="no date",
                       quantity=1.0, unit="kWh", geo="GB")
    db.add(a)
    db.commit()
    db.refresh(a)
    undated = a.id
    bad = ActivityRecord(organisation_id=env["org_a"], date="not-a-date",
                         category="electricity", subcategory="", description="bad date",
                         quantity=1.0, unit="kWh", geo="GB")
    db.add(bad)
    db.commit()
    db.refresh(bad)
    bad_id = bad.id
    db.close()

    c, hdr = env["client"], env["hdr_a"]
    c.post("/organisations/retention", params={"retention_days": 1}, headers=hdr)
    prev = c.post("/retention/preview", params={"as_of": "2026-01-01"},
                  headers=hdr).json()
    assert undated in prev["undated_skipped"]
    assert bad_id in prev["undated_skipped"]
    assert undated not in prev["would_hard_delete"]
    assert bad_id not in prev["would_hard_delete"]

    c.post("/retention/apply", params={"confirm": True, "as_of": "2026-01-01"},
           headers=hdr)
    db = env["Session"]()
    assert db.get(ActivityRecord, undated) is not None
    assert db.get(ActivityRecord, bad_id) is not None
    db.close()


def test_retention_apply_requires_confirmation(env):
    c, hdr = env["client"], env["hdr_a"]
    c.post("/organisations/retention", params={"retention_days": 30}, headers=hdr)
    r = c.post("/retention/apply", params={"as_of": "2024-12-31"}, headers=hdr)
    assert r.status_code == 400
    assert "confirm=true" in r.json()["detail"]


def test_retention_apply_deletes_the_unfrozen_and_redacts_the_frozen(env):
    c, hdr = env["client"], env["hdr_a"]
    c.post("/organisations/retention", params={"retention_days": 30}, headers=hdr)
    r = c.post("/retention/apply",
               params={"confirm": True, "as_of": "2024-12-31"}, headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["hard_deleted"] == [env["loose"]]
    assert body["redacted"] == [env["frozen"]]

    db = env["Session"]()
    assert db.get(ActivityRecord, env["loose"]) is None
    a = db.get(ActivityRecord, env["frozen"])
    assert a is not None
    assert a.redaction_reason == "retention"
    db.close()


def test_retention_apply_keeps_the_filed_run_reproducible(env):
    from app.reports.secr import secr_report
    S = env["Session"]
    db = S()
    before = secr_report(db, env["org_a"], run_id=env["run"])
    db.close()
    assert before["energy_use_kwh"]["total_kwh"] > 0, before

    c, hdr = env["client"], env["hdr_a"]
    c.post("/organisations/retention", params={"retention_days": 30}, headers=hdr)
    c.post("/retention/apply", params={"confirm": True, "as_of": "2024-12-31"},
           headers=hdr)

    db = S()
    after = secr_report(db, env["org_a"], run_id=env["run"])
    db.close()
    # Every DISCLOSED FIGURE is unchanged — that is the reproduction contract.
    assert after["emissions_tco2e"] == before["emissions_tco2e"]
    assert after["energy_use_kwh"] == before["energy_use_kwh"]
    assert after["derivations"] == before["derivations"]
    assert after["run"] == before["run"]
    # The staleness VERDICT legitimately changes, and the next test pins why.


def test_hard_delete_can_clear_a_staleness_warning(env):
    """A consequence worth stating rather than discovering later.

    The fixture's run was computed when only the frozen activity existed; adding the
    second one made the run STALE. Hard-deleting that second row returns the live
    activity set to exactly what the run froze, so the run correctly stops reporting
    itself stale — `stale` compares the live fingerprint with the frozen one, and they
    genuinely match again.

    This is accurate, not a laundered figure: no disclosed number moves (the previous
    test asserts that), and the run really does match current data again. But the
    direction matters — deletion can CLEAR a warning — so it is pinned here so a future
    change to the staleness rule cannot silently alter it.
    """
    from app.reports.summary import summary
    S = env["Session"]
    db = S()
    before = summary(db, organisation_id=env["org_a"], run_id=env["run"])
    db.close()
    assert before["coverage"]["stale"] is True, (
        "fixture precondition: the run should be stale, since an activity was added "
        "after it was computed")

    r = env["client"].delete(f"/activities/{env['loose']}", headers=env["hdr_a"])
    assert r.json()["action"] == "deleted"

    db = S()
    after = summary(db, organisation_id=env["org_a"], run_id=env["run"])
    db.close()
    assert after["coverage"]["stale"] is False
    # And the figures did not move.
    assert after["by_scope"] == before["by_scope"]
    assert after["total_co2e"] == before["total_co2e"]
    assert after["total_co2e_market"] == before["total_co2e_market"]


def test_redaction_never_changes_the_staleness_verdict(env):
    """The contrast: redaction, the path a frozen row takes, moves nothing at all."""
    from app.reports.summary import summary
    S = env["Session"]
    db = S()
    before = summary(db, organisation_id=env["org_a"], run_id=env["run"])
    db.close()

    env["client"].delete(f"/activities/{env['frozen']}", params={"mode": "redact"},
                         headers=env["hdr_a"])

    db = S()
    after = summary(db, organisation_id=env["org_a"], run_id=env["run"])
    db.close()
    assert after["coverage"]["stale"] == before["coverage"]["stale"]
    assert after == before


def test_retention_is_organisation_scoped(env):
    """B applying retention must not touch A's rows."""
    c = env["client"]
    c.post("/organisations/retention", params={"retention_days": 1},
           headers=env["hdr_b"])
    r = c.post("/retention/apply", params={"confirm": True, "as_of": "2026-01-01"},
               headers=env["hdr_b"])
    assert r.status_code == 200
    assert r.json()["counts"]["hard_deleted"] == 0
    db = env["Session"]()
    assert db.get(ActivityRecord, env["loose"]) is not None
    assert db.get(ActivityRecord, env["frozen"]) is not None
    db.close()


def test_retention_rejects_a_nonsense_period(env):
    r = env["client"].post("/organisations/retention",
                           params={"retention_days": 0}, headers=env["hdr_a"])
    assert r.status_code == 400


def test_retention_can_be_cleared(env):
    c, hdr = env["client"], env["hdr_a"]
    c.post("/organisations/retention", params={"retention_days": 30}, headers=hdr)
    r = c.post("/organisations/retention", params={"clear": True}, headers=hdr)
    assert r.status_code == 200
    assert r.json()["retention_days"] is None


# --- export -----------------------------------------------------------------------------

def test_export_round_trips_the_inventory(env):
    r = env["client"].get("/organisations/export", headers=env["hdr_a"])
    assert r.status_code == 200
    body = r.json()
    assert body["export_version"] == 1
    assert body["organisation"]["name"] == "A"

    acts = body["data"]["activities"]
    assert {a["id"] for a in acts} == {env["frozen"], env["loose"]}
    by_id = {a["id"]: a for a in acts}
    assert by_id[env["frozen"]]["quantity"] == 1000.0
    assert by_id[env["frozen"]]["unit"] == "kWh"
    assert "Dana Whitfield" in by_id[env["frozen"]]["description"]

    # Runs and their frozen lines come too, so the figures are reproducible offline.
    assert [r_["id"] for r_ in body["data"]["calculation_runs"]] == [env["run"]]
    lines = body["data"]["emission_line_items"]
    assert len(lines) >= 1
    assert all(ln["run_id"] == env["run"] for ln in lines)
    assert json.loads(lines[0]["details"])  # the frozen calculation context parses


def test_export_omits_credentials(env):
    """A credential in an export file is a credential in a mailbox."""
    db = env["Session"]()
    _seed_credential_rows(db, env)
    db.close()

    r = env["client"].get("/organisations/export", headers=env["hdr_a"])
    raw = r.text
    # No credential VALUE anywhere in the payload.
    assert "SECRET-HASH-VALUE" not in raw
    assert "SECRET-CLIENT-HASH" not in raw
    body = r.json()
    # No credential KEY on any exported row. (The column names do appear once, in the
    # payload's own `notes`, which states what was withheld — that is the disclosure,
    # not a leak, so assert on the rows rather than the raw text.)
    assert "access_token_hash" not in body["data"]["assurance_engagements"][0]
    assert "client_secret_hash" not in body["data"]["pact_clients"][0]
    assert "api_key_hash" not in body["organisation"]
    for table, rows in body["data"].items():
        for row in rows:
            leaked = set(row) & L.NEVER_SERIALISE
            assert not leaked, f"{table} exported credential columns {leaked}"
    # And the omission is declared rather than left for the reader to notice.
    assert "pact_tokens" in body["excluded"]


def test_export_is_organisation_scoped(env):
    r = env["client"].get("/organisations/export", headers=env["hdr_b"])
    assert r.status_code == 200
    body = r.json()
    assert body["organisation"]["name"] == "B"
    assert body["data"]["activities"] == []
    assert body["data"]["calculation_runs"] == []


def test_export_counts_redactions_so_erased_text_is_visible(env):
    env["client"].delete(f"/activities/{env['frozen']}", params={"mode": "redact"},
                         headers=env["hdr_a"])
    body = env["client"].get("/organisations/export", headers=env["hdr_a"]).json()
    assert body["redacted_activities"] == 1
    acts = {a["id"]: a for a in body["data"]["activities"]}
    assert acts[env["frozen"]]["redacted_at"] is not None
    assert "Dana Whitfield" not in json.dumps(body)


def test_export_json_is_stable_for_unchanged_data(env):
    db = env["Session"]()
    a = L.export_json(db, env["org_a"], "2026-01-01T00:00:00Z")
    b = L.export_json(db, env["org_a"], "2026-01-01T00:00:00Z")
    db.close()
    assert a == b


def test_export_covers_every_tenant_table_except_credentials():
    """A table added later must either be exported or explicitly excluded."""
    tenant = set(L.TENANT_DIRECT) | set(L.TENANT_VIA)
    missing = tenant - set(L.EXPORT_TABLES) - set(L.EXPORT_EXCLUDED)
    assert not missing, (
        f"these tenant tables are neither exported nor declared excluded: "
        f"{sorted(missing)} — a churning customer would silently not get them")


def test_export_requires_authentication(env):
    assert env["client"].get("/organisations/export").status_code == 401
    assert env["client"].get("/organisations/export",
                             headers={"X-API-Key": "nope"}).status_code == 401


# --- tenant deletion --------------------------------------------------------------------

def _org_row_counts(db, org_id: int) -> dict:
    """Every org-scoped table's row count for one org, counted directly rather than
    through the module under test."""
    md = Base.metadata
    out = {}
    for name in L._org_scoped_tables(md):
        t = md.tables[name]
        if name in L.TENANT_DIRECT:
            stmt = select(func.count()).select_from(t).where(
                t.c.organisation_id == org_id)
        else:
            fk, parent, pk = L.TENANT_VIA[name]
            p = md.tables[parent]
            stmt = select(func.count()).select_from(t).where(
                t.c[fk].in_(select(p.c[pk]).where(p.c.organisation_id == org_id)))
        n = int(db.execute(stmt).scalar() or 0)
        if n:
            out[name] = n
    return out


def _dummy(col):
    """A type-appropriate value for a required column.

    1.0 for every float is chosen because it satisfies every numeric CHECK in the schema
    at once: `> 0`, `>= 0`, `>= 1 AND <= 5` (data quality), `>= 0 AND <= 1` (allocation
    and share factors) and `> 0 AND <= 100` (materiality).
    """
    import sqlalchemy as sa
    t = col.type
    if isinstance(t, sa.Boolean):
        return False
    if isinstance(t, sa.Float):
        return 1.0
    if isinstance(t, sa.Integer):
        return 2024 if "year" in col.name else 1
    if isinstance(t, sa.Date):
        import datetime
        return datetime.date(2024, 1, 1)
    return "x"


def _enum_choices(table):
    """{column: first allowed value} parsed from this table's CHECK constraints.

    Covers the `col IN ('a','b',...)` constraints, which are most of them, so the filler
    below does not need a hand-written value per enum column.
    """
    import re
    import sqlalchemy as sa
    out = {}
    for c in table.constraints:
        if not isinstance(c, sa.CheckConstraint):
            continue
        for col, vals in re.findall(
                r"(\w+)\s+IN\s+\(([^)]*)\)", str(c.sqltext), re.I):
            choices = re.findall(r"'([^']*)'", vals)
            if choices and col in table.columns:
                out.setdefault(col, choices[0])
    return out


# The few CHECK constraints that relate two columns, so no per-column default can
# satisfy them. Kept explicit and tiny rather than solved: the assertion in
# test_tenant_deletion_covers_every_table fails loudly if a new table needs an entry,
# which is a better signal than a clever filler that silently skips a table.
_FILL_OVERRIDES = {
    ("emissions_targets", "base_year"): 2020,          # target_year > base_year
    ("emissions_targets", "target_year"): 2030,
    ("granular_certificates", "production_start"): "2024-01-01T00:00:00Z",
    ("granular_certificates", "production_end"): "2024-01-01T01:00:00Z",
    ("run_financed_lines", "ghgp_category"): 15,       # ck_rfl_cat15
    # ck_rms_rate_entailment: the statuses 'org_instrument' and 'reference_rate' REQUIRE
    # a rate, and the enum parser picks one of them out of that very constraint.
    ("run_residual_mix_statements", "status"): "fully_contractual",
    ("scope3_category_declarations", "category"): 1,   # 1..15
    ("run_scope3_declarations", "category"): 1,
}


def _fill_every_tenant_table(db, org_id: int) -> dict:
    """Insert one row into EVERY org-scoped table, driven by the metadata.

    Deliberately generic rather than a hand-written list of models: a table added later
    is populated here automatically, so test_tenant_deletion_covers_every_table keeps
    proving something about the whole schema rather than about the dozen tables someone
    remembered. Required columns get a type-appropriate dummy, enum columns get a value
    parsed from their CHECK constraint, and FK columns point at a row already inserted in
    this pass (parents first).
    """
    from sqlalchemy import insert
    md = Base.metadata
    # Parents first, which is the reverse of the delete order.
    order = list(reversed(L._org_scoped_tables(md)))
    ids = {}
    for name in order:
        t = md.tables[name]
        enums = _enum_choices(t)
        values = {}
        for col in t.columns:
            if col.primary_key:
                continue
            fks = list(col.foreign_keys)
            if fks:
                parent = fks[0].column.table.name
                if parent == "organisations":
                    values[col.name] = org_id
                elif parent == name:
                    continue           # self-FK: leave NULL on the first row
                elif parent in ids:
                    values[col.name] = ids[parent]
                elif not col.nullable:
                    values[col.name] = 1
                continue
            if col.name == "organisation_id":
                values[col.name] = org_id
                continue
            override = _FILL_OVERRIDES.get((name, col.name))
            if override is not None:
                values[col.name] = override
            elif col.name in enums:
                values[col.name] = enums[col.name]
            elif not col.nullable and col.default is None and col.server_default is None:
                values[col.name] = _dummy(col)
        res = db.execute(insert(t).values(**values))
        ids[name] = res.inserted_primary_key[0]
    db.commit()
    return ids


def test_tenant_deletion_leaves_no_orphans(env):
    """Assert ZERO remaining rows across every org-scoped table, rather than trusting
    the cascade (there is none — the models declare no ON DELETE)."""
    S = env["Session"]
    db = S()
    before = _org_row_counts(db, env["org_a"])
    db.close()
    # The fixture must actually give us something to delete, across several tables.
    assert len(before) >= 3, f"fixture too thin to prove anything: {before}"

    r = env["client"].request(
        "DELETE", "/organisations/self",
        params={"confirm_org_name": "A", "confirm_irreversible": True},
        headers=env["hdr_a"])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["verified_no_orphans"] is True
    assert body["remaining"] == {}
    assert body["irreversible"] is True

    db = S()
    after = _org_row_counts(db, env["org_a"])
    assert after == {}, f"orphan rows survived tenant deletion: {after}"
    assert db.get(Organisation, env["org_a"]) is None
    db.close()


def test_tenant_deletion_covers_every_table(env):
    """Populate EVERY org-scoped table from the metadata, then prove all of them go.

    The strong form of the orphan check: not "the dozen tables I thought of" but every
    table the classification claims to cover, including the run children and the
    self-referential removal_records chain.
    """
    S = env["Session"]
    db = S()
    oid = env["org_a"]
    _fill_every_tenant_table(db, oid)
    before = _org_row_counts(db, oid)
    db.close()

    tenant_tables = set(L._org_scoped_tables(Base.metadata))
    assert set(before) == tenant_tables, (
        "the generic filler did not populate every table it should: missing "
        f"{sorted(tenant_tables - set(before))}")

    r = env["client"].request(
        "DELETE", "/organisations/self",
        params={"confirm_org_name": "A", "confirm_irreversible": True},
        headers=env["hdr_a"])
    assert r.status_code == 200, r.text
    assert r.json()["verified_no_orphans"] is True

    db = S()
    after = _org_row_counts(db, oid)
    assert after == {}, f"orphan rows survived tenant deletion: {after}"
    db.close()


def test_tenant_deletion_handles_the_self_referential_chain(env):
    """removal_records.reverses_record_id points at the same table, so a multi-row
    DELETE fails under PRAGMA foreign_keys=ON unless the self-FK is broken first."""
    from app.models import RemovalRecord
    S = env["Session"]
    db = S()
    oid = env["org_a"]
    first = RemovalRecord(organisation_id=oid, record_kind="removal",
                          removal_category="technological", method="biochar",
                          scope="1", quantity_tco2e=10.0,
                          quantification_method="measured", attribute_retained=True)
    db.add(first)
    db.commit()
    db.refresh(first)
    db.add(RemovalRecord(organisation_id=oid, record_kind="reversal",
                         removal_category="technological", method="biochar",
                         scope="1", quantity_tco2e=2.0,
                         quantification_method="measured", attribute_retained=True,
                         reverses_record_id=first.id))
    db.commit()
    assert _org_row_counts(db, oid)["removal_records"] == 2
    db.close()

    r = env["client"].request(
        "DELETE", "/organisations/self",
        params={"confirm_org_name": "A", "confirm_irreversible": True},
        headers=env["hdr_a"])
    assert r.status_code == 200, r.text
    assert r.json()["verified_no_orphans"] is True

    db = S()
    assert "removal_records" not in _org_row_counts(db, oid)
    db.close()


def test_tenant_deletion_does_not_touch_the_other_tenant(env):
    S = env["Session"]
    db = S()
    _fill_every_tenant_table(db, env["org_b"])
    b_before = _org_row_counts(db, env["org_b"])
    db.close()
    assert b_before

    env["client"].request(
        "DELETE", "/organisations/self",
        params={"confirm_org_name": "A", "confirm_irreversible": True},
        headers=env["hdr_a"])

    db = S()
    assert _org_row_counts(db, env["org_b"]) == b_before
    assert db.get(Organisation, env["org_b"]) is not None
    db.close()


def test_tenant_deletion_keeps_global_reference_data(env):
    """The platform's shared factor catalogue must survive a tenant leaving."""
    S = env["Session"]
    db = S()
    global_factors = db.query(func.count(EmissionFactor.id)).filter(
        EmissionFactor.organisation_id.is_(None)).scalar()
    db.close()
    assert global_factors >= 1

    env["client"].request(
        "DELETE", "/organisations/self",
        params={"confirm_org_name": "A", "confirm_irreversible": True},
        headers=env["hdr_a"])

    db = S()
    assert db.query(func.count(EmissionFactor.id)).filter(
        EmissionFactor.organisation_id.is_(None)).scalar() == global_factors
    db.close()


def test_tenant_deletion_removes_factors_that_tenant_contributed(env):
    """A factor with this org's id is theirs — a PACT supplier footprint — and goes."""
    S = env["Session"]
    db = S()
    db.add(EmissionFactor(source="PACT", version="1", geography="GB", year=2024,
                          category="purchased_goods", subcategory="widget",
                          unit="kg", gwp_set="AR6", value=1.5,
                          organisation_id=env["org_a"]))
    db.commit()
    db.close()

    r = env["client"].request(
        "DELETE", "/organisations/self",
        params={"confirm_org_name": "A", "confirm_irreversible": True},
        headers=env["hdr_a"])
    assert r.json()["verified_no_orphans"] is True

    db = S()
    assert db.query(func.count(EmissionFactor.id)).filter(
        EmissionFactor.organisation_id == env["org_a"]).scalar() == 0
    db.close()


def test_tenant_deletion_needs_both_confirmations(env):
    c, hdr = env["client"], env["hdr_a"]
    r = c.request("DELETE", "/organisations/self",
                  params={"confirm_org_name": "A"}, headers=hdr)
    assert r.status_code == 400
    assert "confirm_irreversible=true" in r.json()["detail"]

    r = c.request("DELETE", "/organisations/self",
                  params={"confirm_org_name": "B", "confirm_irreversible": True},
                  headers=hdr)
    assert r.status_code == 400
    assert "does not match" in r.json()["detail"]

    db = env["Session"]()
    assert db.get(Organisation, env["org_a"]) is not None
    db.close()


def test_tenant_deletion_requires_authentication(env):
    r = env["client"].request("DELETE", "/organisations/self",
                              params={"confirm_org_name": "A",
                                      "confirm_irreversible": True})
    assert r.status_code == 401
    r = env["client"].request("DELETE", "/organisations/self",
                              params={"confirm_org_name": "A",
                                      "confirm_irreversible": True},
                              headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_tenant_deletion_is_logged_with_its_confirmation(env, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="app.lifecycle"):
        env["client"].request(
            "DELETE", "/organisations/self",
            params={"confirm_org_name": "A", "confirm_irreversible": True},
            headers=env["hdr_a"])
    msgs = [r.message for r in caplog.records]
    assert "tenant deletion starting" in msgs
    assert "tenant deletion complete" in msgs


def test_erasure_is_logged(env, caplog):
    import logging
    with caplog.at_level(logging.INFO, logger="app.lifecycle"):
        env["client"].delete(f"/activities/{env['frozen']}",
                             params={"mode": "redact"}, headers=env["hdr_a"])
    assert "activity redacted" in [r.message for r in caplog.records]


# --- read-back --------------------------------------------------------------------------

def test_activities_can_be_listed_back(env):
    r = env["client"].get("/activities", headers=env["hdr_a"])
    assert r.status_code == 200
    body = r.json()
    assert {a["id"] for a in body["items"]} == {env["frozen"], env["loose"]}
    assert body["total_matched"] == 2
    assert body["has_more"] is False
    assert body["next_offset"] is None


def test_list_endpoints_are_organisation_scoped(env):
    """The review's worst finding class: resolve the org, then never use it."""
    r = env["client"].get("/activities", headers=env["hdr_b"])
    assert r.status_code == 200
    assert r.json()["items"] == []
    assert r.json()["total_matched"] == 0


def test_list_pagination_reports_more(env):
    db = env["Session"]()
    for i in range(5):
        db.add(ActivityRecord(organisation_id=env["org_a"], date="2024-07-01",
                              category="electricity", subcategory="",
                              description=f"row {i}", quantity=1.0, unit="kWh",
                              geo="GB"))
    db.commit()
    db.close()

    r = env["client"].get("/activities", params={"limit": 2, "offset": 0},
                          headers=env["hdr_a"]).json()
    assert len(r["items"]) == 2
    assert r["total_matched"] == 7
    assert r["has_more"] is True
    assert r["next_offset"] == 2

    last = env["client"].get("/activities", params={"limit": 2, "offset": 6},
                             headers=env["hdr_a"]).json()
    assert len(last["items"]) == 1
    assert last["has_more"] is False
    assert last["next_offset"] is None

    # Walking the pages must visit every row exactly once.
    seen, offset = [], 0
    while True:
        page = env["client"].get("/activities", params={"limit": 3, "offset": offset},
                                 headers=env["hdr_a"]).json()
        seen += [i["id"] for i in page["items"]]
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    assert sorted(seen) == sorted({a["id"] for a in
                                   env["client"].get("/activities",
                                                     params={"limit": 500},
                                                     headers=env["hdr_a"]).json()["items"]})
    assert len(seen) == len(set(seen)) == 7


def test_list_rejects_a_bad_window(env):
    c, hdr = env["client"], env["hdr_a"]
    assert c.get("/activities", params={"limit": 0}, headers=hdr).status_code == 400
    assert c.get("/activities", params={"limit": 10_000},
                 headers=hdr).status_code == 400
    assert c.get("/activities", params={"offset": -1}, headers=hdr).status_code == 400


def test_list_endpoints_require_authentication(env):
    for path in ("/activities", "/reporting_periods", "/entities", "/credits"):
        assert env["client"].get(path).status_code == 401, path
        assert env["client"].get(
            path, headers={"X-API-Key": "nope"}).status_code == 401, path


def test_list_endpoints_never_return_credentials(env):
    db = env["Session"]()
    _seed_credential_rows(db, env)
    db.close()
    for path, secret in (("/pact/clients", "SECRET-CLIENT-HASH"),
                         ("/assurance/engagements", "SECRET-HASH-VALUE")):
        r = env["client"].get(path, headers=env["hdr_a"])
        assert r.status_code == 200, path
        assert r.json()["items"], path
        assert secret not in r.text, path


def test_every_tenant_object_type_is_listable(env):
    """The commercial complaint was "no list endpoint for 20+ object types". Pin the
    coverage so a new POST-created type without a GET is a visible gap."""
    paths = {r.path for r in main_mod.app.routes
             if "GET" in (getattr(r, "methods", None) or set())}
    expected = (
        "/activities", "/reporting_periods", "/entities", "/market_instruments",
        "/lca/assessments", "/finance/positions", "/removals",
        "/scope3/declarations", "/nature/sites", "/nature/targets",
        "/assurance/engagements", "/pact/clients", "/hourly/certificates",
        "/hourly/loads", "/hourly/deliverability_links", "/targets", "/credits",
        "/taxonomy/activities", "/cbam/goods",
    )
    assert len(expected) >= 19
    missing = [p for p in expected if p not in paths]
    assert not missing, f"no list endpoint for: {missing}"


def test_redacted_activity_is_visible_as_redacted_in_the_list(env):
    """Correction requires seeing what happened, not just what is left."""
    env["client"].delete(f"/activities/{env['frozen']}", params={"mode": "redact"},
                         headers=env["hdr_a"])
    items = env["client"].get("/activities", headers=env["hdr_a"]).json()["items"]
    row = next(i for i in items if i["id"] == env["frozen"])
    assert row["redacted_at"] is not None
    assert row["redaction_reason"] == "erasure_request"
    assert "redacted" in row["description"]


# --- review findings: regressions --------------------------------------------------------
# Each of these reproduced as a defect before the fix beside it. They are grouped so the
# shape stays visible: three of the four are the same invariant — a record must not assert
# something that did not happen, and a verdict must not certify over what it cannot see.

def test_redaction_does_not_stamp_fields_that_were_never_populated(env):
    """An absence must not be recorded as an erasure.

    Stamping "[redacted ...]" over a field that was always NULL made the evidence pack
    assert that personal data had been removed from a record that never held any —
    invariant 1 inverted, on the one artefact whose purpose is to be shown to an assuror.
    """
    db = env["Session"]()
    a = ActivityRecord(organisation_id=env["org_a"], date="2024-05-01",
                       category="electricity", subcategory="", description=None,
                       source_file=None, quantity=7.0, unit="kWh", geo="GB")
    db.add(a)
    db.commit()
    db.refresh(a)
    bare = a.id
    db.close()

    r = env["client"].delete(f"/activities/{bare}", params={"mode": "redact"},
                             headers=env["hdr_a"])
    assert r.status_code == 200
    body = r.json()
    assert body["fields_cleared"] == []
    assert body["nothing_to_redact"] is True
    assert "held no free text" in body["note"]

    db = env["Session"]()
    a = db.get(ActivityRecord, bare)
    # The fields are untouched: still absent, not claiming an erasure.
    assert a.description is None
    assert a.source_file is None
    # But the request itself is on the record.
    assert a.redacted_at is not None
    db.close()


def test_redaction_still_clears_a_field_that_was_populated(env):
    """The negative control for the test above: a real erasure must still happen."""
    r = env["client"].delete(f"/activities/{env['frozen']}", params={"mode": "redact"},
                             headers=env["hdr_a"])
    body = r.json()
    assert set(body["fields_cleared"]) == {"description", "source_file"}
    assert "nothing_to_redact" not in body
    db = env["Session"]()
    a = db.get(ActivityRecord, env["frozen"])
    assert a.description.startswith("[redacted ")
    db.close()


def test_a_run_that_froze_an_activity_as_an_exclusion_blocks_hard_delete(env):
    """The reproduction contract must not be decided on half the picture.

    compute_co2e writes a per-activity exclusion register to run.notes for records it
    could not calculate — a negative quantity, an unconvertible unit, a missing
    quantity. Those produce NO line item, so a blocker check that only joined line
    items hard-deleted them and left the filed run's register naming an activity id
    that no longer resolves.
    """
    from app.services.calc import compute_co2e
    db = env["Session"]()
    bad = ActivityRecord(organisation_id=env["org_a"], date="2024-06-01",
                         category="electricity", subcategory="",
                         description="meter read, Sam Okoro", quantity=-5.0,
                         unit="kWh", geo="GB", factor_id=env["factor"])
    db.add(bad)
    db.commit()
    db.refresh(bad)
    bad_id = bad.id
    run = compute_co2e(db, env["org_a"])
    db.commit()
    run_id = run.id
    named = [n.get("activity_id") for n in json.loads(run.notes or "[]")
             if isinstance(n, dict)]
    db.close()
    assert bad_id in named, "fixture precondition: the run must have excluded this row"

    # No line item exists for it...
    db = env["Session"]()
    assert db.query(func.count(EmissionLineItem.id)).filter(
        EmissionLineItem.activity_id == bad_id).scalar() == 0
    db.close()

    # ...yet the run froze a statement about it, so a hard delete is refused.
    r = env["client"].delete(f"/activities/{bad_id}", params={"mode": "hard"},
                             headers=env["hdr_a"])
    assert r.status_code == 409
    detail = _structured_refusal(r)
    blocking = {b["run_id"]: b for b in detail["blocking_runs"]}
    assert run_id in blocking
    assert blocking[run_id]["frozen_as"] == "exclusion"
    assert blocking[run_id]["exclusion_reason"]

    # And auto mode redacts rather than deletes.
    r = env["client"].delete(f"/activities/{bad_id}", headers=env["hdr_a"])
    assert r.json()["action"] == "redacted"
    db = env["Session"]()
    assert db.get(ActivityRecord, bad_id) is not None
    db.close()


def test_a_line_and_an_exclusion_are_distinguished_not_double_counted(env):
    """Both kinds of freeze are reported, and a run doing both is named once."""
    blockers = None
    db = env["Session"]()
    blockers = L.runs_freezing_activity(db, env["frozen"])
    db.close()
    assert [b["frozen_as"] for b in blockers] == ["line"]
    assert len(blockers) == len({b["run_id"] for b in blockers})


def test_retention_apply_refuses_a_future_as_of(env):
    """`as_of` is interchangeable with retention_days, so a future one would turn any
    retention period into "delete everything"."""
    c, hdr = env["client"], env["hdr_a"]
    c.post("/organisations/retention", params={"retention_days": 36500}, headers=hdr)
    r = c.post("/retention/apply",
               params={"confirm": True, "as_of": "2999-01-01"}, headers=hdr)
    assert r.status_code == 400
    assert "in the future" in r.json()["detail"]
    # Nothing was touched by the refused call.
    db = env["Session"]()
    assert db.get(ActivityRecord, env["loose"]) is not None
    assert db.get(ActivityRecord, env["frozen"]).redacted_at is None
    db.close()


def test_retention_apply_still_accepts_a_past_as_of(env):
    """The negative control: a past as_of only ever narrows the window, so it stays
    allowed — and it is what makes these tests deterministic."""
    c, hdr = env["client"], env["hdr_a"]
    c.post("/organisations/retention", params={"retention_days": 30}, headers=hdr)
    r = c.post("/retention/apply",
               params={"confirm": True, "as_of": "2024-12-31"}, headers=hdr)
    assert r.status_code == 200
    assert r.json()["hard_deleted"] == [env["loose"]]


def test_a_100_year_retention_deletes_nothing_whatever_as_of_is_passed(env):
    """The property the bound exists to guarantee."""
    c, hdr = env["client"], env["hdr_a"]
    c.post("/organisations/retention", params={"retention_days": 36500}, headers=hdr)
    for as_of in (None, "2999-01-01", "2026-01-01"):
        params = {"confirm": True}
        if as_of:
            params["as_of"] = as_of
        r = c.post("/retention/apply", params=params, headers=hdr)
        assert r.status_code in (200, 400), (as_of, r.text)
        if r.status_code == 200:
            assert r.json()["counts"]["hard_deleted"] == 0, as_of
            assert r.json()["counts"]["redacted"] == 0, as_of
    db = env["Session"]()
    assert db.get(ActivityRecord, env["loose"]) is not None
    db.close()


def test_the_orphan_verdict_can_see_the_tenant_via_tables(env):
    """A verification that cannot observe what it verifies is worse than none, because
    its green result gets quoted.

    TENANT_VIA tables are reached through a parent that the deletion itself removes, so
    counting them through the live parent is structurally zero whether or not a child
    survived. The scan now takes the parent ids captured before the deletion.
    """
    from sqlalchemy import text
    S = env["Session"]
    db = S()
    snap = L.parent_id_snapshot(db, env["org_a"])
    assert snap.get("calculation_runs"), "fixture must have a run to orphan from"
    # Manufacture exactly the state a missed TENANT_VIA delete would leave.
    db.execute(text("PRAGMA foreign_keys=OFF"))
    db.execute(text("DELETE FROM calculation_runs"))
    db.commit()
    surviving = db.execute(
        text("SELECT COUNT(*) FROM emission_line_items")).scalar()
    assert surviving > 0

    blind = L.orphan_scan(db, env["org_a"])
    seeing = L.orphan_scan(db, env["org_a"], parent_ids=snap)
    db.close()

    # Without the snapshot the orphan is invisible — and the verdict SAYS so rather
    # than implying it checked.
    assert "emission_line_items" not in blind["remaining"]
    assert "emission_line_items" in blind["coverage"]["tables_not_verifiable"]
    # With it, the orphan is found.
    assert seeing["remaining"]["emission_line_items"] == surviving
    assert seeing["clean"] is False
    assert seeing["coverage"]["tables_not_verifiable"] == []
    assert (seeing["coverage"]["tables_verified_directly"]
            == seeing["coverage"]["tables_scanned"])


def test_tenant_deletion_verdict_declares_full_coverage(env):
    """The real path must verify every tenant table, not 24 of 34."""
    db = env["Session"]()
    _fill_every_tenant_table(db, env["org_a"])
    db.close()
    r = env["client"].request(
        "DELETE", "/organisations/self",
        params={"confirm_org_name": "A", "confirm_irreversible": True},
        headers=env["hdr_a"])
    assert r.status_code == 200, r.text
    cov = r.json()["verification_coverage"]
    assert cov["tables_not_verifiable"] == []
    assert cov["tables_verified_directly"] == cov["tables_scanned"]
    assert cov["tables_scanned"] == len(L._org_scoped_tables(Base.metadata)) + 1


def test_erasure_and_retention_declare_what_they_do_not_cover(env):
    """Both reach activities only. A payload saying just "retention applied" invited
    the reading that the whole inventory had aged out."""
    c, hdr = env["client"], env["hdr_a"]
    scope_sources = []

    scope_sources.append(c.delete(f"/activities/{env['frozen']}",
                                  params={"mode": "redact"},
                                  headers=hdr).json()["scope"])
    c.post("/organisations/retention", params={"retention_days": 30}, headers=hdr)
    scope_sources.append(c.post("/retention/preview", params={"as_of": "2024-12-31"},
                                headers=hdr).json()["scope"])
    scope_sources.append(c.post("/retention/apply",
                                params={"confirm": True, "as_of": "2024-12-31"},
                                headers=hdr).json()["scope"])

    tenant = set(L.TENANT_DIRECT) | set(L.TENANT_VIA)
    for scope in scope_sources:
        assert set(scope["tables_covered"]) == set(L.ERASURE_TABLES)
        # Derived, not hardcoded: every tenant table is either covered or named as not.
        assert (set(scope["tables_covered"]) | set(scope["tables_not_covered"])
                == tenant)
        assert scope["tables_not_covered_count"] == len(scope["tables_not_covered"])
        assert "reporting_entities" in scope["tables_not_covered"]


def test_new_lifecycle_columns_need_no_backfill(env):
    """Phase 0's factor-tenancy column needed a backfill because NULL there means
    'readable by every tenant' — existing rows defaulted to permissive. Checked here
    that none of the three columns added by this work has that shape.

    retention_days NULL = keep indefinitely (a backfilled number would start deleting).
    redacted_at / redaction_reason NULL = never redacted, which is true of every row
    that existed before the migration. All three are conservative when absent.
    """
    db = env["Session"]()
    org = db.get(Organisation, env["org_a"])
    assert org.retention_days is None
    # A NULL retention period must delete nothing, not everything.
    r = env["client"].post("/retention/preview", headers=env["hdr_a"])
    assert r.status_code == 400
    assert "no retention period set" in r.json()["detail"]
    # Pre-existing activities read as never redacted, not as redacted-with-no-reason.
    for aid in (env["frozen"], env["loose"]):
        a = db.get(ActivityRecord, aid)
        assert a.redacted_at is None and a.redaction_reason is None
    db.close()


def test_source_file_is_a_bulk_selector_so_tombstones_must_be_unique(env):
    """`source_file` IS a bulk-mutation selector, so a shared redacted value would be
    a selector matching a whole retention sweep.

    Four endpoints accept `source_file=<name>` and UPDATE every matching row, and three
    of the fields they set (entity_id, coverage_start, coverage_end) are in
    activities_fingerprint() — entity_id also drives the consolidation share — so a
    wrongly-wide bulk update there moves published figures. The tombstone therefore
    carries the activity id and no two rows can ever share one.
    """
    import pathlib
    import re

    # First, pin the premise: these call sites exist. If they ever go away this test
    # should be revisited rather than silently guarding nothing.
    selector_sites = []
    for path in sorted(pathlib.Path("app").rglob("*.py")):
        if path.name == "lifecycle.py":
            continue
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"ActivityRecord\.source_file\s*(==|\.in_|\.like|\.ilike)",
                         line):
                selector_sites.append(f"{path}:{i}")
    assert selector_sites, (
        "no endpoint selects on source_file any more — the uniqueness requirement below "
        "may no longer be needed, so re-read the tombstone() rationale")

    # Now the property: redact several rows in one sweep and prove no two share a value.
    db = env["Session"]()
    ids = []
    for i in range(4):
        a = ActivityRecord(organisation_id=env["org_a"], date="2024-02-0%d" % (i + 1),
                           category="electricity", subcategory="",
                           description="meter %d" % i, source_file="batch.csv",
                           quantity=1.0, unit="kWh", geo="GB")
        db.add(a)
        db.commit()
        db.refresh(a)
        ids.append(a.id)
    db.close()

    # Redacted explicitly: these rows are not frozen by any run, so a retention sweep
    # would hard-delete them and there would be no tombstone to compare.
    c, hdr = env["client"], env["hdr_a"]
    for aid in ids:
        assert c.delete(f"/activities/{aid}", params={"mode": "redact"},
                        headers=hdr).status_code == 200

    db = env["Session"]()
    rows = [db.get(ActivityRecord, i) for i in ids]
    assert all(a is not None for a in rows)
    values = [a.source_file for a in rows]
    assert all(v and v.startswith("[redacted ") for v in values), values
    assert len(set(values)) == len(values), (
        "redacted source_file values collided, so one of them selects several rows: "
        f"{values}")
    assert len(set(a.description for a in rows)) == len(rows)
    # upload_hash is deliberately NOT redacted: provenance survives without the name.
    db.close()


def test_a_redacted_source_file_cannot_select_a_second_row(env):
    """The bulk endpoint is the real consumer, so exercise it rather than the column."""
    db = env["Session"]()
    ids = []
    for i in range(3):
        a = ActivityRecord(organisation_id=env["org_a"], date="2024-02-1%d" % i,
                           category="electricity", subcategory="",
                           description="m%d" % i, source_file="shared.csv",
                           quantity=1.0, unit="kWh", geo="GB")
        db.add(a)
        db.commit()
        db.refresh(a)
        ids.append(a.id)
    db.close()

    c, hdr = env["client"], env["hdr_a"]
    for aid in ids:
        assert c.delete(f"/activities/{aid}", params={"mode": "redact"},
                        headers=hdr).status_code == 200

    db = env["Session"]()
    stamped = db.get(ActivityRecord, ids[0]).source_file
    db.close()

    # Using one row's redacted value as a bulk selector must reach exactly that row.
    r = c.post("/activities/coverage_window",
               params={"source_file": stamped, "coverage_start": "2024-02-01",
                       "coverage_end": "2024-02-28"}, headers=hdr)
    assert r.status_code == 200, r.text
    db = env["Session"]()
    touched = [a for a in (db.get(ActivityRecord, i) for i in ids)
               if a.coverage_start is not None]
    db.close()
    assert len(touched) == 1, (
        f"a redacted source_file selected {len(touched)} rows; it must select at most "
        f"the one it belongs to")
