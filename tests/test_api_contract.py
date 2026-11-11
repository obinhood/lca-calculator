"""The HTTP contract a customer integrates against.

Four defects, all of which a client discovers only in production:

1. A collection that truncates WITHOUT SAYING SO. The mapping review queue stopped at
   500 rows and returned a bare array, so clearing a page looked exactly like clearing
   the queue; the assurer drill-down had no cap at all and returned 65.5 MB in one body.
2. A write with no idempotency. A market instrument submitted twice declared its coverage
   twice and drove published market-based Scope 2 down — to zero where the claim was 100%
   renewable — while the readiness gate still passed.
3. An error body whose `detail` was a string from one layer and an array of objects from
   another, and a MISSING credential answered 422 where a WRONG one answered 401.
4. Narrative free text and people's names in query strings, which the production command
   writes verbatim to an access log that has no deletion path.

These tests pin the contract, not the implementation: they assert what a client can rely
on seeing.
"""
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  (registers tables on Base.metadata)
from app import main as main_mod
from app.models import (
    ActivityRecord, CalculationRun, EmissionFactor, MarketInstrument, Organisation,
)
from app.services.assurance import readiness_assessment
from app.services.calc import compute_co2e


@pytest.fixture
def env():
    """A real app and a session over ONE in-memory database."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    def override():
        s = Session()
        try:
            yield s
        finally:
            s.close()

    main_mod.app.dependency_overrides[main_mod.get_db] = override
    try:
        yield TestClient(main_mod.app), db
    finally:
        main_mod.app.dependency_overrides.clear()
        db.close()


def _org(db, name="Alpha", key="ka"):
    o = Organisation(name=name, api_key_hash=main_mod._hash_key(key))
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category="electricity", value=0.2, unit="kWh"):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category=category, subcategory="", unit=unit, gwp_set="AR6",
                       value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _act(db, org_id, factor_id=None, quantity=100.0, status="needs_review",
         category="electricity"):
    a = ActivityRecord(organisation_id=org_id, date="2025-03-01", category=category,
                       subcategory="", description="", quantity=quantity, unit="kWh",
                       geo="GB", factor_id=factor_id, mapping_status=status)
    db.add(a); db.commit(); db.refresh(a)
    return a


HDR = {"X-API-Key": "ka"}


# =============================================================================
# 1. Pagination — and above all, a cap that SAYS it is one
# =============================================================================

def test_review_queue_reports_the_rows_it_withheld(env):
    """The defect in one assertion: a short page must be distinguishable from a
    finished queue."""
    client, db = env
    org = _org(db)
    for _ in range(12):
        _act(db, org.id)

    r = client.get("/mappings/review", headers=HDR, params={"limit": 5})
    assert r.status_code == 200
    body = r.json()
    assert len(body["activities"]) == 5
    page = body["page"]
    assert page["returned"] == 5
    assert page["total_matched"] == 12, "the count withheld must be stated, not implied"
    assert page["has_more"] is True
    assert page["next_offset"] == 5


def test_the_last_page_says_it_is_the_last(env):
    client, db = env
    org = _org(db)
    for _ in range(12):
        _act(db, org.id)

    page = client.get("/mappings/review", headers=HDR,
                      params={"limit": 5, "offset": 10}).json()["page"]
    assert page["returned"] == 2 and page["has_more"] is False
    # None, not the past-the-end integer: a client looping on next_offset terminates.
    assert page["next_offset"] is None


def test_paging_covers_every_row_exactly_once(env):
    """A window is only stable over a deterministic order; without one, pages overlap
    or skip and the caller never finds out."""
    client, db = env
    org = _org(db)
    for _ in range(12):
        _act(db, org.id)

    seen, offset = [], 0
    while True:
        body = client.get("/mappings/review", headers=HDR,
                          params={"limit": 5, "offset": offset}).json()
        seen += [a["activity_id"] for a in body["activities"]]
        if not body["page"]["has_more"]:
            break
        offset = body["page"]["next_offset"]
    assert len(seen) == 12 and len(set(seen)) == 12


def test_an_offset_past_the_end_is_an_empty_page_not_an_error(env):
    client, db = env
    org = _org(db)
    _act(db, org.id)
    body = client.get("/mappings/review", headers=HDR,
                      params={"offset": 500}).json()
    assert body["activities"] == []
    assert body["page"]["total_matched"] == 1 and body["page"]["has_more"] is False


@pytest.mark.parametrize("params,why", [
    ({"limit": 0}, "limit below the floor"),
    ({"limit": -1}, "negative limit"),
    ({"limit": 1001}, "limit above the ceiling"),
    ({"offset": -1}, "negative offset"),
])
def test_a_window_outside_the_contract_is_refused_not_clamped(env, params, why):
    """Clamping is how a silent cap comes back: a caller asking for 5000 and receiving
    1000 without being told has the original problem again."""
    client, db = env
    _org(db)
    r = client.get("/mappings/review", headers=HDR, params=params)
    assert r.status_code == 400, why
    assert isinstance(r.json()["detail"], str)


@pytest.mark.parametrize("limit", [1, 1000])
def test_the_boundary_values_are_accepted(env, limit):
    client, db = env
    org = _org(db)
    _act(db, org.id)
    assert client.get("/mappings/review", headers=HDR,
                      params={"limit": limit}).status_code == 200


def test_a_next_link_is_emitted_and_preserves_the_other_parameters(env):
    """Dropping the caller's filters from the next link is the classic pagination bug:
    page two of a filtered list silently becomes page two of the unfiltered one."""
    client, db = env
    org = _org(db)
    for _ in range(6):
        _act(db, org.id)

    r = client.get("/mappings/review", headers=HDR, params={"limit": 2})
    link = r.headers.get("Link")
    assert link and 'rel="next"' in link
    assert "limit=2" in link and "offset=2" in link

    last = client.get("/mappings/review", headers=HDR,
                      params={"limit": 2, "offset": 4})
    assert "Link" not in last.headers, "a next link on the last page loops a client forever"


def test_a_next_link_is_replayable(env):
    client, db = env
    org = _org(db)
    for _ in range(6):
        _act(db, org.id)
    r = client.get("/mappings/review", headers=HDR, params={"limit": 2})
    url = r.headers["Link"].split(">")[0].lstrip("<")
    follow = client.get(url, headers=HDR)
    assert follow.status_code == 200
    assert follow.json()["page"]["offset"] == 2


def test_the_assurer_drill_down_is_paginated(env):
    """The 65.5 MB response. `run` and `exclusions` repeat on every page so a page is
    self-describing, and the line items are windowed."""
    client, db = env
    org = _org(db)
    f = _factor(db)
    for _ in range(8):
        _act(db, org.id, factor_id=f.id, status="approved")
    run = compute_co2e(db, org.id)

    body = client.get(f"/runs/{run.id}/lineage", headers=HDR,
                      params={"limit": 3}).json()
    assert body["run"]["id"] == run.id
    assert "exclusions" in body
    assert len(body["line_items"]) == 3
    assert body["page"]["total_matched"] >= 8 and body["page"]["has_more"] is True


def test_runs_no_longer_truncate_at_fifty_in_silence(env):
    client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, status="approved")
    for _ in range(3):
        compute_co2e(db, org.id)

    body = client.get("/runs", headers=HDR, params={"limit": 2}).json()
    assert len(body["runs"]) == 2
    assert body["page"]["total_matched"] == 3 and body["page"]["has_more"] is True


# =============================================================================
# 1b. /factors: the catalogue the override dropdown could not reach
# =============================================================================

def test_the_whole_catalogue_is_reachable_and_searchable(env):
    """Capped at 200 with no filter passed from the UI, most of the catalogue could not
    be chosen at all — the rows simply were not in the response."""
    client, db = env
    _org(db)
    for i in range(10):
        _factor(db, category=f"cat{i:02d}")
    _factor(db, category="district_heating")

    page1 = client.get("/factors", headers=HDR, params={"limit": 4}).json()
    assert len(page1["factors"]) == 4
    assert page1["page"]["total_matched"] == 11 and page1["page"]["has_more"] is True

    hit = client.get("/factors", headers=HDR, params={"q": "district"}).json()
    assert hit["page"]["total_matched"] == 1
    assert hit["factors"][0]["cat"] == "district_heating"

    assert client.get("/factors", headers=HDR,
                      params={"q": "nothing-matches-this"}).json()["factors"] == []


# =============================================================================
# 2. Idempotency, and the volume honesty behind it
# =============================================================================

def _instrument_params(**over):
    p = {"instrument_type": "rec", "kg_co2e_per_kwh": 0.0, "coverage_kwh": 1000.0,
         "market": "GB", "start_date": "2025-01-01", "end_date": "2025-12-31"}
    p.update(over)
    return p


def test_a_replayed_instrument_submission_is_refused(env):
    client, db = env
    _org(db)
    first = client.post("/market_instruments", headers=HDR, params=_instrument_params())
    assert first.status_code == 200

    replay = client.post("/market_instruments", headers=HDR, params=_instrument_params())
    assert replay.status_code == 409, \
        "a double-submitted REC declares its coverage twice and drives market Scope 2 down"
    assert isinstance(replay.json()["detail"], str)
    assert db.query(MarketInstrument).count() == 1


def test_a_replay_differing_only_in_prose_is_still_a_replay(env):
    """`description` is excluded from the identity hash on purpose: a retry with a
    corrected typo must not become a second certificate."""
    client, db = env
    _org(db)
    client.post("/market_instruments", headers=HDR, params=_instrument_params(),
                json={"description": "Q1 certificate"})
    again = client.post("/market_instruments", headers=HDR, params=_instrument_params(),
                        json={"description": "Q1 certificate (corrected)"})
    assert again.status_code == 409
    assert db.query(MarketInstrument).count() == 1


def test_an_instrument_differing_in_substance_is_accepted(env):
    client, db = env
    _org(db)
    client.post("/market_instruments", headers=HDR, params=_instrument_params())
    other = client.post("/market_instruments", headers=HDR,
                        params=_instrument_params(start_date="2026-01-01",
                                                  end_date="2026-12-31"))
    assert other.status_code == 200
    assert db.query(MarketInstrument).count() == 2


def _rec(db, org_id, coverage):
    i = MarketInstrument(organisation_id=org_id, instrument_type="rec",
                         kg_co2e_per_kwh=0.0, coverage_kwh=coverage, gwp_set="AR6",
                         market="GB")
    db.add(i); db.commit(); db.refresh(i)
    return i


def test_declared_coverage_within_the_load_is_stated_and_passes(env):
    _client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, quantity=1000.0, status="approved")
    _rec(db, org.id, coverage=600.0)

    run = compute_co2e(db, org.id)
    cov = _statement(db, run)
    assert cov["load_kwh"] == 1000.0 and cov["declared_contractual_kwh"] == 600.0
    assert cov["over_declared"] is False
    assert _check(db, run)["pass"] is True


def test_declared_coverage_above_the_load_is_refused_by_the_gate(env):
    """Two certificates each covering the whole load. Allocation alone never notices:
    the first covers everything and the second simply sits in the queue."""
    _client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, quantity=1000.0, status="approved")
    _rec(db, org.id, coverage=1000.0)
    _rec(db, org.id, coverage=1000.0)

    run = compute_co2e(db, org.id)
    cov = _statement(db, run)
    assert cov["declared_contractual_kwh"] == 2000.0 and cov["load_kwh"] == 1000.0
    assert cov["over_declared"] is True
    assert cov["over_declared_kwh"] == 1000.0

    check = _check(db, run)
    assert check["pass"] is False, \
        "an over-declared renewable claim must not clear the disclosure gate"
    assert readiness_assessment(db, run)["ready"] is False


def test_an_unbounded_contractual_instrument_cannot_be_decided_and_does_not_pass(env):
    """A REC with no coverage_kwh claims the whole inventory with no volume behind it —
    the 100%-renewable shape of this defect. The verdict is None, not False, and an
    undecided claim does not pass: a gate that passes over an absent figure is the
    failure being closed."""
    _client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, quantity=1000.0, status="approved")
    _rec(db, org.id, coverage=None)

    run = compute_co2e(db, org.id)
    cov = _statement(db, run)
    assert cov["determinable"] is False
    assert cov["over_declared"] is None, "unknown is not the same answer as no"
    assert cov["over_declared_kwh"] is None
    assert _check(db, run)["pass"] is False


def test_a_run_with_no_contractual_instrument_has_nothing_to_over_declare(env):
    _client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, quantity=1000.0, status="approved")
    run = compute_co2e(db, org.id)
    assert _statement(db, run)["over_declared"] is False
    assert _check(db, run)["pass"] is True


def test_the_verdict_is_frozen_onto_the_run_not_rederived(env):
    """A filed run's verdict must not change because a certificate was added or
    withdrawn afterwards."""
    _client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, quantity=1000.0, status="approved")
    _rec(db, org.id, coverage=600.0)
    run = compute_co2e(db, org.id)
    assert _statement(db, run)["declared_contractual_kwh"] == 600.0

    _rec(db, org.id, coverage=5000.0)          # bought after the run was filed
    db.refresh(run)
    assert _statement(db, run)["declared_contractual_kwh"] == 600.0


def _statement(db, run):
    import json
    row = db.get(CalculationRun, run.id)
    return json.loads(row.scope2_coverage_statement)


def _check(db, run):
    checks = readiness_assessment(db, run)["checks"]
    return next(c for c in checks
                if c["check"] == "scope2_declared_coverage_within_load")


# =============================================================================
# 3. One error envelope, and 401 for a missing credential
# =============================================================================

def test_a_missing_api_key_is_401_like_a_wrong_one(env):
    client, db = env
    _org(db)
    missing = client.get("/results/summary")
    wrong = client.get("/results/summary", headers={"X-API-Key": "nope"})
    assert missing.status_code == 401 and wrong.status_code == 401
    # The same condition must not produce two body shapes either.
    for r in (missing, wrong):
        assert isinstance(r.json()["detail"], str)
        assert r.json()["error"]["code"] == "unauthenticated"


def test_a_missing_admin_key_is_401(env):
    client, db = env
    _org(db)
    import os
    os.environ["ADMIN_API_KEY"] = "secret"
    try:
        r = client.post("/reference/fx_rates", headers=HDR,
                        params={"base_currency": "EUR", "quote_currency": "GBP",
                                "rate": 0.85, "as_of_date": "2025-01-01"})
        assert r.status_code == 401
        assert isinstance(r.json()["detail"], str)
    finally:
        os.environ.pop("ADMIN_API_KEY", None)


def test_detail_is_a_string_whichever_layer_refused(env):
    """It used to be a string from a handler and an array of objects from request
    validation, so a client needed two parsers for one field."""
    client, db = env
    _org(db)

    from_handler = client.get("/mappings/review", headers=HDR, params={"limit": 0})
    from_validation = client.get("/runs/not-an-integer/lineage", headers=HDR)
    assert from_handler.status_code == 400 and from_validation.status_code == 422
    for r in (from_handler, from_validation):
        assert isinstance(r.json()["detail"], str), r.json()
        err = r.json()["error"]
        assert err["status"] == r.status_code and isinstance(err["message"], str)


def test_the_structured_validation_detail_is_still_available(env):
    """Moved beside `detail`, not thrown away — a client that wants the field list can
    still have it, without the shape of `detail` depending on the layer."""
    client, db = env
    _org(db)
    r = client.get("/runs/not-an-integer/lineage", headers=HDR)
    fields = r.json()["error"]["fields"]
    assert isinstance(fields, list) and fields
    assert {"location", "name", "message", "type"} <= set(fields[0])


def test_a_404_carries_the_same_envelope(env):
    client, db = env
    _org(db)
    r = client.get("/runs/99999/lineage", headers=HDR)
    assert r.status_code == 404
    assert isinstance(r.json()["detail"], str)
    assert r.json()["error"]["code"] == "not_found"


def test_the_pact_host_keeps_its_own_spec_defined_error_object(env):
    """/3/... serves {code, message}, which a conformance runner parses. Re-shaping it
    would fail the run, so those paths are exempt — including when the refusal comes
    from request validation rather than the handler."""
    client, db = env
    _org(db)
    r = client.get("/3/footprints", headers={"Authorization": "Bearer nope"})
    body = r.json()
    assert r.status_code == 401
    assert set(body) == {"code", "message"}, body
    assert "detail" not in body


# =============================================================================
# 4. Narrative and person-identifying values leave the query string
# =============================================================================

def test_a_narrative_query_parameter_is_refused_not_ignored(env):
    """Ignoring it would DROP the note and report success — worse than the logging
    problem being fixed."""
    client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, status="approved")
    run = compute_co2e(db, org.id)
    eng = client.post("/assurance/engagements", headers=HDR,
                      params={"run_id": run.id, "standard": "ISO_14064_3",
                              "level": "limited"}).json()["id"]

    stale = client.post(f"/assurance/engagements/{eng}/findings", headers=HDR,
                        params={"severity": "minor", "description": "in the URL"})
    assert stale.status_code == 400
    assert "description" in stale.json()["detail"]
    assert "request body" in stale.json()["detail"]


def test_the_body_form_works(env):
    client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, status="approved")
    run = compute_co2e(db, org.id)
    eng = client.post("/assurance/engagements", headers=HDR,
                      params={"run_id": run.id, "standard": "ISO_14064_3",
                              "level": "limited"},
                      json={"assuror_name": "Example Assurance LLP"}).json()["id"]

    ok = client.post(f"/assurance/engagements/{eng}/findings", headers=HDR,
                     params={"severity": "minor"},
                     json={"description": "factor vintage query"})
    assert ok.status_code == 200

    fid = ok.json()["id"]
    resolved = client.post(f"/assurance/findings/{fid}/resolve", headers=HDR,
                           json={"resolution_note": "vintage confirmed correct"})
    assert resolved.status_code == 200 and resolved.json()["status"] == "resolved"


def test_a_required_narrative_body_says_what_to_send(env):
    client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, status="approved")
    run = compute_co2e(db, org.id)
    eng = client.post("/assurance/engagements", headers=HDR,
                      params={"run_id": run.id, "standard": "ISO_14064_3",
                              "level": "limited"}).json()["id"]
    r = client.post(f"/assurance/engagements/{eng}/findings", headers=HDR,
                    params={"severity": "minor"})
    assert r.status_code == 422
    assert "description" in r.json()["detail"]


def test_an_unknown_body_key_is_refused_rather_than_dropped(env):
    """A misspelt field must not be accepted and silently stored as nothing."""
    client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, status="approved")
    run = compute_co2e(db, org.id)
    eng = client.post("/assurance/engagements", headers=HDR,
                      params={"run_id": run.id, "standard": "ISO_14064_3",
                              "level": "limited"}).json()["id"]
    r = client.post(f"/assurance/engagements/{eng}/findings", headers=HDR,
                    params={"severity": "minor"},
                    json={"descrption": "typo"})
    assert r.status_code == 422


def test_the_report_narrative_moved_to_the_post_form(env):
    """A GET cannot carry a body, so the narrative moved to a POST of the same path that
    returns an IDENTICAL payload."""
    client, db = env
    org = _org(db)
    f = _factor(db)
    a = _act(db, org.id, factor_id=f.id, quantity=1000.0, status="approved")
    base = compute_co2e(db, org.id)
    a.quantity = 400.0                       # the retrofit
    db.commit()
    proj = compute_co2e(db, org.id)
    ids = {"baseline_run_id": base.id, "project_run_id": proj.id, "leakage_tco2e": 0.0}

    stale = client.get("/reports/iso_14064_2", headers=HDR,
                       params={**ids, "baseline_justification": "in the URL"})
    assert stale.status_code == 400 and "baseline_justification" in stale.json()["detail"]

    JUST = ("The baseline is the metered consumption of the pre-retrofit boiler over the "
            "same twelve months, adjusted for degree days.")
    posted = client.post("/reports/iso_14064_2", headers=HDR, params=ids,
                         json={"baseline_justification": JUST,
                               "project_name": "Boiler retrofit"})
    assert posted.status_code == 200
    assert posted.json()["project"]["baseline_justification"] == JUST
    assert posted.json()["project"]["name"] == "Boiler retrofit"

    # The two forms are one renderer: same inputs, same payload.
    got = client.get("/reports/iso_14064_2", headers=HDR, params=ids).json()
    no_narrative = client.post("/reports/iso_14064_2", headers=HDR, params=ids).json()
    assert got == no_narrative


# =============================================================================
# 3b. A structured refusal keeps its structure
#
# The envelope normalises `detail` to a string, which was the right fix for a field that
# had two shapes. But this codebase refuses with STRUCTURE — a refusal names what blocks
# it, because "no" alone leaves the caller unable to act — and flattening that to a
# sentence would lose exactly what matters on the one path least able to afford it.
#
# The contract: `detail` is the human sentence, ALWAYS a string. `error.context` is the
# structure the handler raised, verbatim. A client reads `detail` to show a person and
# `error.context` to branch on.
# =============================================================================

@contextmanager
def _temporary_route(path, endpoint, methods=("GET",)):
    """Register a route AHEAD of the SPA catch-all, and take it out again.

    `app/main.py` mounts `frontend/dist` at "/" as a catch-all whenever that directory
    exists, and a mount registered earlier shadows every route added later. So a route
    merely APPENDED here 404s for any developer who has run `npm run build` in this tree
    and passes for everyone else — and CI never catches the difference, because the
    frontend and backend jobs build in separate checkouts and the backend suite always
    runs against a tree with no `dist`.

    Inserting at the front makes it resolve either way. Asserting `!= 404` would not:
    that passes for the wrong reason, which is how this class of bug survives.
    """
    app = main_mod.app
    app.add_api_route(path, endpoint, methods=list(methods))
    route = app.router.routes.pop()          # just appended, so it is the last one
    app.router.routes.insert(0, route)
    try:
        yield
    finally:
        app.router.routes.remove(route)


def _frozen_activity_refusal():              # pragma: no cover - driven via the route
    """Stands in for the hard-delete refusal, with that endpoint's own field names, so
    this is the fixture a sibling PR can diff its shape against."""
    main_mod._refuse(409, "activity 7 is frozen by 2 filed runs",
                     action="blocked",
                     blocking_runs=[{"run_id": 3, "frozen_lines": 11},
                                    {"run_id": 5, "frozen_lines": 2}],
                     remedy="mode=redact")


def test_a_structured_refusal_survives_the_envelope(env):
    """Raised through the real app via a real route, so it exercises the exception
    handler rather than the helper in isolation."""
    client, db = env
    _org(db)

    with _temporary_route("/_test/structured-refusal", _frozen_activity_refusal):
        r = client.get("/_test/structured-refusal")
        assert r.status_code == 409, (
            f"expected the refusal, got {r.status_code} — a 404 here means the route was "
            "shadowed by the frontend/dist catch-all mount, not that the envelope broke")
        body = r.json()

        # The human half: still a string, every time, whatever the handler raised.
        assert isinstance(body["detail"], str)
        assert "frozen by 2 filed runs" in body["detail"]

        # The machine half: the structure, intact.
        ctx = body["error"]["context"]
        assert ctx["action"] == "blocked"
        assert ctx["remedy"] == "mode=redact"
        assert [r_["run_id"] for r_ in ctx["blocking_runs"]] == [3, 5]
        assert ctx["blocking_runs"][0]["frozen_lines"] == 11


def test_the_temporary_route_outranks_the_spa_catch_all(env):
    """Pins the fixture itself, because the bug it avoids is invisible without a built
    frontend on disk: the route must sit ahead of every catch-all mount, and must be
    gone again afterwards."""
    client, db = env
    _org(db)
    with _temporary_route("/_test/ordering", _frozen_activity_refusal):
        routes = main_mod.app.router.routes
        ours = next(i for i, rt in enumerate(routes)
                    if getattr(rt, "path", None) == "/_test/ordering")
        # Asserted as "first", not as "before any Mount". `mounts` is empty on a tree
        # with no frontend/dist, and `all()` over an empty list is True — the assertion
        # would pass while proving nothing, in exactly the environment where the bug it
        # guards is invisible. Being first holds in every environment.
        assert ours == 0, "the route must precede every catch-all mount"
        mounts = [i for i, rt in enumerate(routes) if isinstance(rt, Mount)]
        assert all(ours < m for m in mounts)
        assert client.get("/_test/ordering").status_code == 409

    assert all(getattr(rt, "path", None) != "/_test/ordering"
               for rt in main_mod.app.router.routes), "the route outlived its test"


def test_a_plain_refusal_carries_no_context_key(env):
    """Absent structure is absent, not an empty object — a client can tell "this refusal
    has nothing more to say" from "it has structure I failed to read"."""
    client, db = env
    _org(db)
    r = client.get("/mappings/review", headers=HDR, params={"limit": 0})
    assert r.status_code == 400
    assert "context" not in r.json()["error"]


def test_this_pr_uses_the_facility_for_its_own_refusals(env):
    """Not a hook left for someone else to use: the two refusals this PR adds carry the
    structure a caller needs to act on them."""
    client, db = env
    _org(db)

    client.post("/market_instruments", headers=HDR, params=_instrument_params())
    replay = client.post("/market_instruments", headers=HDR, params=_instrument_params())
    assert replay.status_code == 409
    existing = db.query(MarketInstrument).one()
    assert replay.json()["error"]["context"]["conflicting_instrument_id"] == existing.id

    org = db.query(Organisation).one()
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, status="approved")
    run = compute_co2e(db, org.id)
    stale = client.get("/reports/iso_14064_2", headers=HDR,
                       params={"baseline_run_id": run.id,
                               "baseline_justification": "in the URL",
                               "project_name": "Retrofit"})
    assert stale.status_code == 400
    ctx = stale.json()["error"]["context"]
    assert ctx["moved_parameters"] == ["baseline_justification", "project_name"]
    assert "request body" in ctx["remedy"]


def test_the_pact_host_still_gets_a_flat_spec_message(env):
    """/3/... serves {code, message}; a structured refusal there must still render as the
    spec's two keys, not leak our envelope into a conformance run."""
    client, db = env
    _org(db)
    r = client.get("/3/footprints", headers={"Authorization": "Bearer nope"})
    assert set(r.json()) == {"code", "message"}
    assert isinstance(r.json()["message"], str)


# =============================================================================
# 5. The follow-up review: five ways the market-instrument work did not hold
#
# Each of these reproduced before it was fixed. They share one shape — a rule that
# existed but did not reach far enough — and four of the five let the ORIGINAL defect
# back in by another route: a double-counted certificate driving market-based Scope 2
# toward zero.
# =============================================================================

def test_a_case_varied_market_is_the_same_certificate(env):
    """The allocator compares markets as .strip().upper(), so "GB" and "gb" are ONE
    market when coverage is allocated. Identity has to agree, or the two are separate
    certificates going in and one coming out — each declaring the full volume."""
    client, db = env
    _org(db)
    first = client.post("/market_instruments", headers=HDR,
                        params=_instrument_params(market="GB"))
    assert first.status_code == 200
    for variant in ("gb", " GB ", "Gb"):
        r = client.post("/market_instruments", headers=HDR,
                        params=_instrument_params(market=variant))
        assert r.status_code == 409, f"{variant!r} registered a second certificate"
    assert db.query(MarketInstrument).count() == 1


def test_an_instrument_written_before_the_column_existed_is_still_protected(env):
    """A hash lookup cannot see a NULL hash, so matching on the stored hash would have
    protected only rows created after the migration while every instrument a customer
    already held stayed freely duplicable."""
    client, db = env
    _org(db)
    client.post("/market_instruments", headers=HDR, params=_instrument_params())
    legacy = db.query(MarketInstrument).one()
    legacy.submission_hash = None          # exactly what a pre-migration row looks like
    db.commit()

    replay = client.post("/market_instruments", headers=HDR, params=_instrument_params())
    assert replay.status_code == 409, "a pre-existing instrument was duplicable"
    assert db.query(MarketInstrument).count() == 1


def _dated_rec(db, org_id, coverage, start, end):
    i = MarketInstrument(organisation_id=org_id, instrument_type="rec",
                         kg_co2e_per_kwh=0.0, coverage_kwh=coverage, gwp_set="AR6",
                         market="GB", start_date=start, end_date=end)
    db.add(i); db.commit(); db.refresh(i)
    return i


def test_a_declaration_is_measured_against_the_load_it_can_reach(env):
    """A REC dated to January declaring the whole year sits inside the annual total and
    over-declares its month many times over. Comparing with TOTAL load alone misses it."""
    _client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, quantity=100.0, status="approved")   # January
    jan = db.query(ActivityRecord).one()
    jan.date = "2025-01-15"
    feb = ActivityRecord(organisation_id=org.id, date="2025-02-15", category="electricity",
                         subcategory="", description="", quantity=900.0, unit="kWh",
                         geo="GB", factor_id=f.id, mapping_status="approved")
    db.add(feb); db.commit()
    _dated_rec(db, org.id, coverage=1000.0, start="2025-01-01", end="2025-01-31")

    run = compute_co2e(db, org.id)
    cov = _statement(db, run)
    assert cov["declared_contractual_kwh"] == 1000.0
    assert cov["load_kwh"] == 1000.0, "within the annual total, which is why this hid"
    assert cov["over_declared"] is True
    per = {p["instrument_id"]: p for p in cov["per_instrument"]}
    only = next(iter(per.values()))
    assert only["eligible_load_kwh"] == 100.0
    assert only["over_declared_kwh"] == 900.0


# --- the gate has to reach what is PUBLISHED, not only what is assured -------

_RENDERERS = ("sb253_report", "esrs_e1_report", "issb_s2_report",
              "cdp_export", "secr_report", "gri_report")


def _render_all(db, org_id, run):
    from app.reports.cdp import cdp_export
    from app.reports.esrs_e1 import esrs_e1_report
    from app.reports.gri import gri_report
    from app.reports.issb_s2 import issb_s2_report
    from app.reports.sb253 import sb253_report
    from app.reports.secr import secr_report
    out = {}
    for name, fn in (("sb253_report", sb253_report), ("esrs_e1_report", esrs_e1_report),
                     ("issb_s2_report", issb_s2_report), ("cdp_export", cdp_export),
                     ("secr_report", secr_report), ("gri_report", gri_report)):
        out[name] = fn(db, org_id, run_id=run.id)
    return out


def _over_declaration_blockers(report):
    return [b for b in report.get("blockers", []) if "OVER-DECLARED" in str(b)]


def test_every_published_scope2_verdict_blocks_an_over_declared_run(env):
    """The gate had ONE caller — the assurance checklist — so a run declaring twice the
    volume it consumed, and publishing a market-based Scope 2 of zero on that basis,
    passed all six disclosure renderers. The defect was reachable by not asking for
    assurance."""
    _client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, quantity=1000.0, status="approved")
    _rec(db, org.id, coverage=1000.0)
    _rec(db, org.id, coverage=1000.0)          # the double submission
    run = compute_co2e(db, org.id)
    assert _statement(db, run)["over_declared"] is True
    assert run.total_co2e_market == 0.0, "the figure the claim rests on"

    for name, report in _render_all(db, org.id, run).items():
        assert _over_declaration_blockers(report), f"{name} published it anyway"


def test_an_honest_full_coverage_claim_still_publishes(env):
    """The discrimination that matters: one REC covering exactly the load is a genuine
    100%-renewable claim and a market-based zero. It must not be caught."""
    _client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, quantity=1000.0, status="approved")
    _rec(db, org.id, coverage=1000.0)
    run = compute_co2e(db, org.id)
    assert _statement(db, run)["over_declared"] is False
    assert run.total_co2e_market == 0.0

    for name, report in _render_all(db, org.id, run).items():
        assert not _over_declaration_blockers(report), f"{name} blocked an honest claim"


def test_a_run_frozen_before_the_comparison_is_warned_not_blocked(env):
    """Re-evaluating a filed run at render time would retroactively invalidate a filing
    that was correct when made — the cliff scope2_residual_mix_completeness refuses.
    Assurance readiness is stricter on purpose; a published disclosure is not."""
    from app.services.instruments import over_declaration_gate
    _client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, quantity=1000.0, status="approved")
    _rec(db, org.id, coverage=5000.0)
    run = compute_co2e(db, org.id)
    run.scope2_coverage_statement = None       # a run frozen before this existed
    db.commit()

    gate = over_declaration_gate(db, run)
    assert gate["legacy"] is True
    assert gate["blockers"] == [], "history must not be retroactively invalidated"
    assert gate["warnings"], "but the absence has to be stated, not hidden"
    for name, report in _render_all(db, org.id, run).items():
        assert not _over_declaration_blockers(report), f"{name} blocked a legacy run"


# --- my own POST forms dropped the narrative they were added to protect -----

@pytest.mark.parametrize("path,param", [
    ("/reports/sb253", "assurance_provider"),
    ("/reports/iso_14064_2", "baseline_justification"),
])
def test_the_post_report_forms_refuse_a_query_narrative_too(env, path, param):
    """These endpoints exist BECAUSE a GET cannot carry a body. Reading only the body
    meant a provider sent in the query was logged AND dropped from the filing — the
    worse half of the defect, inside the fix for it."""
    client, db = env
    _org(db)
    r = client.post(path, headers=HDR, params={param: "in the URL"})
    assert r.status_code == 400
    assert param in r.json()["detail"]
    assert param in r.json()["error"]["context"]["moved_parameters"]


# --- a check added later is not evidence that an immutable run changed ------

def test_a_check_added_after_conclusion_is_not_run_drift(env):
    """`run_changed_since_conclusion` is a claim about an IMMUTABLE run. Comparing the
    two `ready` booleans made it true whenever this module gained a check — reporting
    that the run drifted when what changed was the code."""
    from app.models import AssuranceEngagement
    from app.services.assurance import engagement_view
    from app.services.calc import _utcnow_iso
    _client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, quantity=1000.0, status="approved")
    _rec(db, org.id, coverage=9000.0)          # fails the NEW check only
    run = compute_co2e(db, org.id)

    # Concluded before the check existed: its snapshot cannot contain that verdict.
    snapshot = {"ready": True, "checks": [
        {"check": "completeness", "pass": True},
        {"check": "traceability", "pass": True}]}
    eng = AssuranceEngagement(
        organisation_id=org.id, run_id=run.id, standard="ISO_14064_3", level="limited",
        status="concluded", opinion="unqualified", materiality_pct=5.0,
        created_at=_utcnow_iso(), concluded_at=_utcnow_iso(),
        readiness_snapshot=_json_dumps(snapshot))
    db.add(eng); db.commit(); db.refresh(eng)

    view = engagement_view(db, eng)
    assert view["run_changed_since_conclusion"] is False, \
        "the run is immutable; only this module changed"
    # Not hidden either — the run was never assessed against the new check.
    assert "scope2_declared_coverage_within_load" in \
        view["readiness_checks_added_since_conclusion"]


def _json_dumps(o):
    import json
    return json.dumps(o)


def test_summary_shows_the_preparer_what_the_renderer_will_refuse(env):
    """summary is where a preparer looks FIRST and it refuses nothing. Carrying only
    the raw statement meant they would read a market figure resting on over-declared
    instruments, see nothing said about it, and meet the refusal later at the renderer.
    It carries the gate's own sentences now, in _residual_mix_block's shape."""
    from app.reports.summary import summary
    _client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, quantity=1000.0, status="approved")
    _rec(db, org.id, coverage=1000.0)
    _rec(db, org.id, coverage=1000.0)
    run = compute_co2e(db, org.id)

    block = summary(db, organisation_id=org.id, run_id=run.id)["scope2"]["declared_volume"]
    assert block["assessable"] is True and block["legacy"] is False
    assert block["statement"]["over_declared"] is True
    assert block["blockers"], "the preparer must see the sentence that will refuse them"
    assert "OVER-DECLARED" in block["blockers"][0]
    # Same shape as the residual-mix block beside it — one question, two halves.
    assert set(block) == {"assessable", "legacy", "statement", "blockers", "warnings"}


def test_summary_distinguishes_unevaluated_from_nothing_to_report(env):
    """A bare null said neither. legacy=true with a warning says the first; an
    assessable statement with no blockers says the second."""
    from app.reports.summary import summary
    _client, db = env
    org = _org(db)
    f = _factor(db)
    _act(db, org.id, factor_id=f.id, quantity=1000.0, status="approved")
    run = compute_co2e(db, org.id)

    clean = summary(db, organisation_id=org.id, run_id=run.id)["scope2"]["declared_volume"]
    assert clean["assessable"] is True and clean["blockers"] == []

    run.scope2_coverage_statement = None          # frozen before the comparison existed
    db.commit()
    legacy = summary(db, organisation_id=org.id, run_id=run.id)["scope2"]["declared_volume"]
    assert legacy["legacy"] is True and legacy["statement"] is None
    assert legacy["blockers"] == [], "history is not retroactively invalidated"
    assert legacy["warnings"], "but the absence is stated rather than left as a null"
