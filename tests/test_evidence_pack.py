"""The assurance evidence pack.

Three properties carry this module. The hash must be stable for unchanged frozen
state and must move when the content moves, or it verifies nothing. Every section
must read frozen run state, so a later re-map cannot rewrite a filed pack. And the
gap list must name what the platform genuinely cannot produce — a pack that quietly
omitted reviewer identity would read as complete to the one person who most needs
to know it is not.
"""
import json

import pytest

from app.models import (
    ActivityRecord, CalculationRun, EmissionFactor, EmissionLineItem, Organisation,
    ReportingEntity, ReportingPeriod,
)
from app.services.calc import compute_co2e
from app.services.evidence_pack import (
    PACK_VERSION, DEFAULT_MAX_LINES, build_evidence_pack, _SECTION_ORDER,
)


_ORG_SEQ = [0]


def _org(db, name=None):
    """Organisation names are UNIQUE, so each caller gets a fresh one by default."""
    _ORG_SEQ[0] += 1
    o = Organisation(name=name or f"PackOrg{_ORG_SEQ[0]}")
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category="electricity", value=0.2, unit="kWh", year=2024):
    f = EmissionFactor(source="DEFRA", version="2024", geography="GB", year=year,
                       category=category, subcategory="", unit=unit, gwp_set="AR6",
                       value=value, method_type="average_data")
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor, quantity=100.0, category="electricity", date="2024-06-01"):
    a = ActivityRecord(organisation_id=org_id, date=date, category=category,
                       subcategory="grid", description="metered", quantity=quantity,
                       unit=factor.unit, geo="GB", factor_id=factor.id,
                       mapping_basis="exact", mapping_status="approved",
                       source_file="bills.csv")
    db.add(a); db.commit(); db.refresh(a)
    return a


def _run(db, n=4, factors=2, with_period=False):
    org = _org(db)
    period_id = None
    if with_period:
        p = ReportingPeriod(organisation_id=org.id, label="FY24",
                            start_date="2024-01-01", end_date="2024-12-31")
        db.add(p); db.commit(); db.refresh(p)
        period_id = p.id
    fs = [_factor(db, category=f"cat{i}", value=0.2 + i * 0.1) for i in range(factors)]
    for i in range(n):
        _activity(db, org.id, fs[i % factors], quantity=100.0 + i,
                  category=f"cat{i % factors}")
    return org, compute_co2e(db, org.id, reporting_period_id=period_id)


# --- structure --------------------------------------------------------------

def test_pack_carries_every_declared_section(db):
    _, run = _run(db)
    pack = build_evidence_pack(db, run, uncertainty_iterations=1000)
    assert set(pack["sections"]) == set(_SECTION_ORDER)
    assert pack["pack"]["section_order"] == list(_SECTION_ORDER)
    assert pack["pack"]["pack_version"] == PACK_VERSION
    assert pack["pack"]["run_id"] == run.id


def test_inventory_statement_matches_the_run(db):
    _, run = _run(db)
    s = build_evidence_pack(db, run, uncertainty_iterations=1000)["sections"]["1_inventory_statement"]
    assert s["total_co2e_kg_location_based"] == run.total_co2e
    assert s["total_co2e_kg_market_based"] == run.total_co2e_market
    assert s["gwp_set"] == run.gwp_set


def test_transaction_detail_joins_source_record_to_factor_lineage(db):
    _, run = _run(db, n=3, factors=1)
    sec = build_evidence_pack(db, run, uncertainty_iterations=1000)["sections"]["4_transaction_detail"]
    assert sec["line_count_total"] == sec["line_count_included"] > 0
    assert sec["truncated"] is False
    line = sec["lines"][0]
    # The assurer's drill-down: figure -> source record -> pinned factor.
    assert line["source_record"]["activity_id"]
    assert line["source_record"]["source_file"] == "bills.csv"
    assert line["factor_lineage"]["factor_id"]
    assert line["factor_lineage"]["gwp_set_applied"]
    assert line["data_quality"]["sigma_log"] is not None


def test_factor_register_lists_source_version_and_vintage(db):
    _, run = _run(db, n=6, factors=3)
    sec = build_evidence_pack(db, run, uncertainty_iterations=1000)["sections"]["5_factor_register"]
    assert sec["distinct_factors"] == 3
    for f in sec["factors"]:
        assert f["source"] == "DEFRA"
        assert f["version"] == "2024"
        assert f["vintage_year"] == 2024
        assert f["gwp_set"] == "AR6"
        assert f["lines_using"] >= 1


def test_boundary_section_keeps_the_whole_entity_population(db):
    """Entities weighted 0.0 and entities with no activity ARE the excluded-entity
    list an assuror asks for — dropping them turns a population into an assertion."""
    org = _org(db)
    f = _factor(db)
    _activity(db, org.id, f)
    db.add(ReportingEntity(organisation_id=org.id, name="Dormant JV",
                           accounting_category="joint_venture_incorporated", equity_share_pct=0.0))
    db.commit()
    run = compute_co2e(db, org.id)
    sec = build_evidence_pack(db, run, uncertainty_iterations=1000)["sections"]["3_organisational_boundary"]
    names = [e["entity_name"] for e in sec["entities"]]
    assert "Dormant JV" in names
    assert sec["entity_count"] == len(sec["entities"]) >= 2


def test_completeness_controls_carry_exclusions_and_the_fingerprint(db):
    _, run = _run(db)
    sec = build_evidence_pack(db, run, uncertainty_iterations=1000)["sections"]["7_completeness_controls"]
    assert sec["coverage_pct"] is not None
    assert sec["activities_fingerprint"] == run.activities_fingerprint
    assert isinstance(sec["excluded_activities"], list)
    assert sec["excluded_count"] == len(sec["excluded_activities"])


def test_uncertainty_section_embeds_the_monte_carlo_interval(db):
    _, run = _run(db, n=6, factors=2)
    sec = build_evidence_pack(db, run, uncertainty_iterations=1000)["sections"]["8_data_quality_and_uncertainty"]
    mc = sec["monte_carlo"]
    assert mc["interval"]["low"] < run.total_co2e < mc["interval"]["high"]
    assert mc["reproducibility"]["input_fingerprint"]
    assert set(mc["correlation_bounds"]) == {"independent", "by_factor", "perfect"}


def test_readiness_section_names_the_applicable_standard(db):
    _, run = _run(db, with_period=True)
    sec = build_evidence_pack(db, run, uncertainty_iterations=1000)["sections"]["10_readiness_and_standard"]
    assert "ready" in sec and sec["checks"]
    assert sec["applicable_standard"]["period_start"] == "2024-01-01"
    assert "ISAE_3410" in sec["applicable_standard"]["applicable"]


def test_period_section_distinguishes_scoped_from_unscoped(db):
    _, scoped = _run(db, with_period=True)
    _, unscoped = _run(db, with_period=False)
    a = build_evidence_pack(db, scoped, uncertainty_iterations=1000)["sections"]["2_reporting_period"]
    b = build_evidence_pack(db, unscoped, uncertainty_iterations=1000)["sections"]["2_reporting_period"]
    assert a["period_scoped"] is True and a["start_date"] == "2024-01-01"
    assert b["period_scoped"] is False


# --- the hash ---------------------------------------------------------------

def test_hash_is_stable_across_regeneration(db):
    _, run = _run(db)
    a = build_evidence_pack(db, run, uncertainty_iterations=1000)
    b = build_evidence_pack(db, run, uncertainty_iterations=1000)
    assert a["pack"]["content_hash"] == b["pack"]["content_hash"]


def test_generated_at_is_excluded_from_the_hash(db):
    """A hash that moved on every render could verify nothing — which is the whole
    purpose of stamping it."""
    _, run = _run(db)
    a = build_evidence_pack(db, run, uncertainty_iterations=1000)
    b = build_evidence_pack(db, run, uncertainty_iterations=1000)
    # Same content, same hash — even though the two renders carry their own
    # generation timestamps, which is the property being asserted.
    assert a["pack"]["content_hash"] == b["pack"]["content_hash"]
    assert a["pack"]["generated_at"] and b["pack"]["generated_at"]
    from app.services.evidence_pack import _content_hash
    assert _content_hash(a["sections"], a["evidence_gaps"], a["pack"]["run_id"]) \
        == a["pack"]["content_hash"]


def test_hash_moves_when_frozen_content_moves(db):
    _, run = _run(db)
    before = build_evidence_pack(db, run, uncertainty_iterations=1000)["pack"]["content_hash"]
    line = db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id).order_by(EmissionLineItem.id).first()
    line.co2e = (line.co2e or 0.0) + 1.0
    db.commit()
    after = build_evidence_pack(db, run, uncertainty_iterations=1000)["pack"]["content_hash"]
    assert after != before


def test_two_different_runs_hash_differently(db):
    _, a = _run(db, n=3)
    _, b = _run(db, n=5)
    ha = build_evidence_pack(db, a, uncertainty_iterations=1000)["pack"]["content_hash"]
    hb = build_evidence_pack(db, b, uncertainty_iterations=1000)["pack"]["content_hash"]
    assert ha != hb


def test_hash_is_a_full_sha256(db):
    _, run = _run(db)
    h = build_evidence_pack(db, run, uncertainty_iterations=1000)["pack"]["content_hash"]
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


# --- truncation is disclosed, never silent ----------------------------------

def test_truncated_transaction_detail_says_so(db):
    _, run = _run(db, n=12, factors=2)
    sec = build_evidence_pack(db, run, max_lines=5,
                              uncertainty_iterations=1000)["sections"]["4_transaction_detail"]
    assert sec["truncated"] is True
    assert sec["line_count_included"] == 5
    assert sec["line_count_total"] == 12
    assert "omitted" in sec["truncation_note"]
    assert "max_lines" in sec["truncation_note"]


def test_truncation_does_not_shrink_the_totals(db):
    """Only the line-by-line listing is bounded — the statement and the completeness
    controls must still describe the whole inventory."""
    _, run = _run(db, n=12, factors=2)
    pack = build_evidence_pack(db, run, max_lines=3, uncertainty_iterations=1000)
    assert pack["sections"]["1_inventory_statement"]["total_co2e_kg_location_based"] == run.total_co2e
    assert pack["sections"]["7_completeness_controls"]["counters"]["mapped"] == run.mapped
    assert pack["sections"]["5_factor_register"]["distinct_factors"] == 2


# --- gaps are named, not omitted --------------------------------------------

def test_gap_list_names_what_cannot_be_produced(db):
    _, run = _run(db)
    gaps = build_evidence_pack(db, run, uncertainty_iterations=1000)["evidence_gaps"]
    items = {g["item"] for g in gaps}
    assert "Reviewer identity" in items
    assert "Override log with before/after values" in items
    assert "GL account and cost centre per transaction" in items
    assert "Reconciliation to trial balance" in items


def test_every_gap_states_a_reason_and_a_remedy(db):
    _, run = _run(db)
    for g in build_evidence_pack(db, run, uncertainty_iterations=1000)["evidence_gaps"]:
        assert g["why_absent"].strip(), g["item"]
        assert g["what_would_close_it"].strip(), g["item"]
        assert g["expected_by"].strip(), g["item"]


def test_gaps_are_inside_the_hash(db):
    """The gap list is part of what was handed over: it must not be silently
    editable without the hash moving."""
    from app.services import evidence_pack as ep
    _, run = _run(db)
    before = build_evidence_pack(db, run, uncertainty_iterations=1000)["pack"]["content_hash"]
    original = ep._evidence_gaps
    ep._evidence_gaps = lambda: [{"item": "X", "expected_by": "y",
                                  "why_absent": "z", "what_would_close_it": "w"}]
    try:
        after = build_evidence_pack(db, run, uncertainty_iterations=1000)["pack"]["content_hash"]
    finally:
        ep._evidence_gaps = original
    assert after != before


# --- frozen, not live -------------------------------------------------------

def test_factor_register_survives_a_factor_being_deleted(db):
    """The run's figures came from frozen values, so removing the catalogue row must
    not break the pack — it must report the row as unresolvable instead.

    A foreign key stops a factor being deleted while an activity still points at it,
    so the reachable version of this scenario is: activities are re-mapped onto a
    replacement, the superseded factor is then retired from the catalogue, and the
    filed run's frozen lines still cite it."""
    org, run = _run(db, n=2, factors=1)
    fid = json.loads(db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id).first().details)["factor_id"]
    replacement = _factor(db, category="replacement", value=0.3)
    for a in db.query(ActivityRecord).filter(
            ActivityRecord.organisation_id == org.id).all():
        a.factor_id = replacement.id
    db.commit()
    db.query(EmissionFactor).filter(EmissionFactor.id == fid).delete()
    db.commit()
    sec = build_evidence_pack(db, run, uncertainty_iterations=1000)["sections"]["5_factor_register"]
    assert sec["unresolved"] and sec["unresolved"][0]["factor_id"] == fid
    assert "frozen values" in sec["unresolved_note"]


def test_transaction_detail_reads_the_frozen_factor_not_the_live_mapping(db):
    """A re-map after the run must not rewrite what the pack says was calculated."""
    _, run = _run(db, n=2, factors=1)
    other = _factor(db, category="other", value=99.0)
    for a in db.query(ActivityRecord).all():
        a.factor_id = other.id
    db.commit()
    sec = build_evidence_pack(db, run, uncertainty_iterations=1000)["sections"]["4_transaction_detail"]
    assert all(l["factor_lineage"]["factor_id"] != other.id for l in sec["lines"])


def test_pack_parses_line_details_defensively():
    """The pack's own parser never raises on a corrupt blob.

    NOTE: a corrupt `details` row still breaks the pack END TO END, because
    reports/summary.py calls json.loads without a guard and is reached first. That
    is a pre-existing brittleness in the renderer — one bad row takes down every
    report for the organisation, not just this pack — and is tracked separately
    rather than patched from here.
    """
    from app.services.evidence_pack import _detail
    for bad in ("{not json", "", None, "[]", "null", 7):
        assert isinstance(_detail(bad), dict), bad


# --- endpoint ---------------------------------------------------------------

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
    key_a = client.post("/organisations", params={"name": "A"}).json()["api_key"]
    key_b = client.post("/organisations", params={"name": "B"}).json()["api_key"]

    seed = TestingSession()
    org_b = seed.query(Organisation).filter(Organisation.name == "B").one()
    f = _factor(seed)
    for i in range(4):
        _activity(seed, org_b.id, f, quantity=100.0 + i)
    run_id = compute_co2e(seed, org_b.id).id
    seed.close()

    yield client, {"X-API-Key": key_a}, {"X-API-Key": key_b}, run_id
    main_mod.app.dependency_overrides.clear()


def test_endpoint_returns_a_hash_stamped_pack(env):
    client, _, hdr_b, run_id = env
    r = client.get("/assurance/evidence_pack",
                   params={"run_id": run_id, "uncertainty_iterations": 1000},
                   headers=hdr_b)
    assert r.status_code == 200
    body = r.json()
    assert len(body["pack"]["content_hash"]) == 64
    assert set(body["sections"]) == set(_SECTION_ORDER)
    assert body["evidence_gaps"]


def test_endpoint_defaults_to_the_latest_run(env):
    client, _, hdr_b, run_id = env
    body = client.get("/assurance/evidence_pack",
                      params={"uncertainty_iterations": 1000}, headers=hdr_b).json()
    assert body["pack"]["run_id"] == run_id


def test_endpoint_scopes_runs_to_the_calling_organisation(env):
    client, hdr_a, hdr_b, run_id = env
    assert client.get("/assurance/evidence_pack",
                      params={"run_id": run_id, "uncertainty_iterations": 1000},
                      headers=hdr_b).status_code == 200
    assert client.get("/assurance/evidence_pack",
                      params={"run_id": run_id, "uncertainty_iterations": 1000},
                      headers=hdr_a).status_code == 404


@pytest.mark.parametrize("params", [
    {"max_lines": 0}, {"max_lines": 100001},
    {"uncertainty_iterations": 999}, {"uncertainty_iterations": 200001},
])
def test_endpoint_rejects_out_of_range_parameters(env, params):
    client, _, hdr_b, run_id = env
    r = client.get("/assurance/evidence_pack",
                   params={"run_id": run_id, **params}, headers=hdr_b)
    assert r.status_code == 400
