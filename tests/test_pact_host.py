"""Serving the PACT v3 API.

Built against the Technical Specifications AND against the behaviour of the
official conformance runner, which differs from the prose in several places. The
tests below encode those differences, because the runner is what the network
actually checks — and since April 2025 it is the only path: peer-to-peer testing
was retired and V2 conformance expires 2026-04-01.
"""
import base64
import json

import pytest

from app.models import Organisation, ProductFootprint
from app.services.pact_host import (
    BASE_EVENT_REQUIRED, CLOUDEVENTS_MEDIA_TYPE, ERROR_CODES, EVENT_TYPES,
    PAGINATION_LINK_MIN_VALIDITY_SECONDS, build_response_event, error,
    is_cloudevents_media_type, link_header, parse_basic_auth, request_filters,
    validate_event,
)
from app.services.pact_store import import_footprint

_SEQ = [0]


def _doc(**over) -> dict:
    _SEQ[0] += 1
    d = {
        "id": f"{_SEQ[0]:08d}-da16-4dc1-9185-11d97476c254",
        "specVersion": "3.0.3", "created": "2027-02-01T10:00:00Z", "status": "Active",
        "companyName": "Acme Chemicals GmbH",
        "companyIds": ["urn:pact:company:customcode:buyer-assigned:acme"],
        "productDescription": "Polypropylene homopolymer",
        "productIds": [f"urn:pact:product:customcode:x:PP-{_SEQ[0]}"],
        "productNameCompany": "AcmePP",
        "productClassifications": ["urn:pact:classification:cpc:34110"],
        "pcf": {
            "declaredUnitOfMeasurement": "kilogram", "declaredUnitAmount": "10",
            "productMassPerDeclaredUnit": "10",
            "referencePeriodStart": "2026-01-01T00:00:00Z",
            "referencePeriodEnd": "2027-01-01T00:00:00Z",
            "pcfExcludingBiogenicUptake": "42.0", "pcfIncludingBiogenicUptake": "40.5",
            "fossilCarbonContent": "8.5", "fossilGhgEmissions": "41.2",
            "packagingEmissionsIncluded": False, "exemptedEmissionsPercent": "1.5",
            "ipccCharacterizationFactors": ["AR6"],
            "crossSectoralStandards": ["ISO14067"], "primaryDataShare": "62.5",
            "geographyCountry": "DE",
            "dqi": {"technologicalDQR": "2.0", "geographicalDQR": "1.5",
                    "temporalDQR": "2.5"},
        },
    }
    for k, v in over.items():
        if k == "pcf":
            d["pcf"].update(v)
        else:
            d[k] = v
    return d


# --- auth --------------------------------------------------------------------

def test_basic_auth_is_parsed():
    """The runner sends credentials ONLY this way, despite the prose implying
    form fields."""
    raw = base64.b64encode(b"cid:secret").decode()
    assert parse_basic_auth(f"Basic {raw}") == ("cid", "secret")
    assert parse_basic_auth(f"basic {raw}") == ("cid", "secret")


@pytest.mark.parametrize("header", [
    None, "", "Bearer abc", "Basic !!!notbase64", "Basic " + base64.b64encode(b"nocolon").decode()])
def test_unusable_basic_auth_yields_none(header):
    assert parse_basic_auth(header) is None


def test_a_secret_is_shown_once_and_stored_hashed(db):
    from app.models import PactClient
    from app.services.pact_host import create_client
    o = Organisation(name="HostOrg"); db.add(o); db.commit(); db.refresh(o)
    out = create_client(db, o.id, "Partner Ltd")
    row = db.query(PactClient).one()
    assert out["client_secret"] not in (row.client_secret_hash or "")
    assert len(row.client_secret_hash) == 64


# --- the Link header ----------------------------------------------------------

def test_the_link_header_carries_only_rel_next():
    """The runner takes the FIRST parsed link, not the one whose rel is 'next',
    so a preceding rel='first' would be followed instead."""
    h = link_header("https://x.test", "/3/footprints", {"limit": [10]}, 10)
    assert h.count("rel=") == 1
    assert 'rel="next"' in h


def test_the_rel_value_is_double_quoted():
    """The runner's regex requires it; rel=next unquoted does not match."""
    h = link_header("https://x.test", "/3/footprints", {}, 5)
    assert 'rel="next"' in h and "rel=next;" not in h


def test_the_link_is_absolute_and_carries_the_offset():
    h = link_header("https://x.test", "/3/footprints", {"limit": [2]}, 4)
    assert h.startswith("<https://x.test/3/footprints?")
    assert "offset=4" in h


def test_nothing_follows_the_rel_parameter():
    """Extra RFC 8288 parameters get swallowed by the runner's greedy capture."""
    h = link_header("https://x.test", "/3/footprints", {}, 1)
    assert h.rstrip().endswith('rel="next"')


def test_the_minimum_validity_floor_is_declared():
    assert PAGINATION_LINK_MIN_VALIDITY_SECONDS == 180


# --- the error object ---------------------------------------------------------

@pytest.mark.parametrize("code", ERROR_CODES)
def test_every_declared_error_code_survives(code):
    assert error(code, "m")["code"] == code


def test_a_code_outside_the_enum_is_coerced():
    assert error("Whoops", "m")["code"] == "InternalError"


# --- CloudEvents --------------------------------------------------------------

def test_the_media_type_is_parsed_not_string_compared():
    """The runner appends '; charset=UTF-8'."""
    assert is_cloudevents_media_type(CLOUDEVENTS_MEDIA_TYPE) is True
    assert is_cloudevents_media_type(f"{CLOUDEVENTS_MEDIA_TYPE}; charset=UTF-8") is True
    assert is_cloudevents_media_type("application/json") is False
    assert is_cloudevents_media_type(None) is False


def _event(etype, data, **over):
    e = {"type": etype, "specversion": "1.0", "id": "run-1/13",
         "source": "https://partner.test", "time": "2027-02-01T10:00:00Z",
         "data": data}
    e.update(over)
    return e


@pytest.mark.parametrize("field", BASE_EVENT_REQUIRED)
def test_every_base_envelope_field_is_required(field):
    e = _event(EVENT_TYPES["published"],
               {"pfIds": ["3893bb5d-da16-4dc1-9185-11d97476c254"]})
    e.pop(field)
    v = validate_event(e)
    assert v["valid"] is False and field in v["message"]


def test_an_unknown_event_type_is_rejected():
    v = validate_event(_event("org.wbcsd.pact.Something.9", {}))
    assert v["valid"] is False and "unknown event type" in v["message"]


@pytest.mark.parametrize("etype", sorted(EVENT_TYPES.values()))
def test_every_event_type_string_ends_in_the_version_suffix(etype):
    assert etype.endswith(".3")
    assert etype.startswith("org.wbcsd.pact.ProductFootprint.")


def test_published_event_pf_ids_must_be_uuids():
    """Conformance test 40 posts a GTIN urn and requires a 400 — its published
    documentation describes a different test entirely."""
    v = validate_event(_event(EVENT_TYPES["published"],
                              {"pfIds": ["urn:gtin:4712345060507"]}))
    assert v["valid"] is False
    assert "must be UUIDs" in v["message"]


def test_published_event_accepts_real_uuids():
    v = validate_event(_event(EVENT_TYPES["published"],
                              {"pfIds": ["3893bb5d-da16-4dc1-9185-11d97476c254"]}))
    assert v["valid"] is True


def test_an_empty_pf_ids_array_is_rejected():
    assert validate_event(_event(EVENT_TYPES["published"], {"pfIds": []}))["valid"] is False


def test_a_fulfilled_event_with_no_footprints_is_invalid():
    """pfs has minItems 1 — 'success with zero results' is non-conformant, and the
    spec says to send RequestRejectedEvent instead."""
    v = validate_event(_event(EVENT_TYPES["request_fulfilled"],
                              {"requestEventId": "run-1/13", "pfs": []}))
    assert v["valid"] is False
    assert "minItems 1" in v["message"]
    assert "RequestRejectedEvent instead" in v["message"]


def test_a_rejected_event_needs_a_code_and_a_message():
    bad = validate_event(_event(EVENT_TYPES["request_rejected"],
                                {"requestEventId": "x", "error": {"code": "NotFound"}}))
    assert bad["valid"] is False
    ok = validate_event(_event(EVENT_TYPES["request_rejected"],
                               {"requestEventId": "x",
                                "error": {"code": "NotFound", "message": "none"}}))
    assert ok["valid"] is True


def test_the_request_event_id_is_echoed_byte_for_byte():
    """The runner encodes routing data into the id and recovers it by splitting on
    '/'. Normalising it leaves the test hanging in PENDING rather than failing."""
    weird = "TestRun-ABC/13"
    out = build_response_event("fulfilled", request_event_id=weird,
                               source="https://us.test", pfs=[{"id": "x"}])
    assert out["data"]["requestEventId"] == weird


def test_a_fulfilled_event_cannot_be_built_empty():
    with pytest.raises(ValueError, match="RequestRejectedEvent instead"):
        build_response_event("fulfilled", request_event_id="x",
                             source="https://us.test", pfs=[])


def test_a_rejected_event_carries_a_default_error():
    out = build_response_event("rejected", request_event_id="x",
                               source="https://us.test")
    assert out["data"]["error"]["code"] and out["data"]["error"]["message"]
    assert out["type"] == EVENT_TYPES["request_rejected"]


def test_status_is_a_scalar_in_the_query_and_an_array_in_the_event():
    """The OpenAPI schema types them differently, so one parser cannot serve both."""
    assert request_filters({"status": ["Active"]})["status"] == "Active"
    assert request_filters({"status": "Active"})["status"] == "Active"
    assert request_filters({})["status"] is None


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
    owner = client.post("/organisations", params={"name": "Owner"}).json()["api_key"]
    hdr = {"X-API-Key": owner}

    # The conformance suite aborts at test case 0 with fewer than two footprints.
    docs = [_doc(), _doc(), _doc(status="Deprecated")]
    for d in docs:
        client.post("/pact/footprints/import", headers=hdr,
                    params={"direction": "published"}, json=d)
    creds = client.post("/pact/clients", headers=hdr,
                        params={"partner_name": "Partner"}).json()
    yield client, hdr, creds, docs
    main_mod.app.dependency_overrides.clear()


def _token(client, creds) -> str:
    raw = base64.b64encode(
        f"{creds['client_id']}:{creds['client_secret']}".encode()).decode()
    r = client.post("/auth/token", headers={"Authorization": f"Basic {raw}"},
                    data={"grant_type": "client_credentials"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_the_token_endpoint_is_not_under_the_version_prefix(env):
    """Routing it under /3 fails the first conformance case and aborts the run."""
    client, _, creds, _ = env
    assert _token(client, creds)
    raw = base64.b64encode(
        f"{creds['client_id']}:{creds['client_secret']}".encode()).decode()
    # Not-served is the property, not a particular status. When frontend/dist has
    # been built the SPA mount answers unmatched paths with 405 rather than 404,
    # so asserting 404 alone made this test pass or fail on whether someone had
    # run `npm run build` — which has nothing to do with PACT routing.
    assert client.post("/3/auth/token", headers={"Authorization": f"Basic {raw}"},
                       data={"grant_type": "client_credentials"}
                       ).status_code in (404, 405)


def test_the_token_response_shape(env):
    client, _, creds, _ = env
    raw = base64.b64encode(
        f"{creds['client_id']}:{creds['client_secret']}".encode()).decode()
    body = client.post("/auth/token", headers={"Authorization": f"Basic {raw}"},
                       data={"grant_type": "client_credentials"}).json()
    assert body["token_type"] == "Bearer"
    assert isinstance(body["expires_in"], int) and body["access_token"]


def test_bad_credentials_return_an_oauth_error(env):
    client, _, creds, _ = env
    raw = base64.b64encode(f"{creds['client_id']}:wrong".encode()).decode()
    r = client.post("/auth/token", headers={"Authorization": f"Basic {raw}"},
                    data={"grant_type": "client_credentials"})
    assert r.status_code == 401
    assert r.json()["error"] and r.json()["error_description"]


def test_credentials_in_the_body_also_work(env):
    """Prose-compatible fallback, alongside the Basic header the runner uses."""
    client, _, creds, _ = env
    r = client.post("/auth/token", data={
        "grant_type": "client_credentials",
        "client_id": creds["client_id"], "client_secret": creds["client_secret"]})
    assert r.status_code == 200


def test_list_footprints_requires_a_bearer_token(env):
    client, _, _, _ = env
    r = client.get("/3/footprints")
    assert r.status_code == 401
    assert r.json()["code"] in ERROR_CODES


def test_list_footprints_returns_a_data_array(env):
    client, _, creds, _ = env
    t = _token(client, creds)
    body = client.get("/3/footprints", headers={"Authorization": f"Bearer {t}"}).json()
    assert isinstance(body["data"], list) and len(body["data"]) == 3


def test_deprecated_footprints_are_included_by_default(env):
    """Filtering them out silently would hide the supersession chain."""
    client, _, creds, _ = env
    t = _token(client, creds)
    body = client.get("/3/footprints", headers={"Authorization": f"Bearer {t}"}).json()
    assert any(d["status"] == "Deprecated" for d in body["data"])
    only_active = client.get("/3/footprints", params={"status": "Active"},
                             headers={"Authorization": f"Bearer {t}"}).json()
    assert all(d["status"] == "Active" for d in only_active["data"])


def test_array_filters_arrive_as_repeated_query_keys(env):
    client, _, creds, docs = env
    t = _token(client, creds)
    a, b = docs[0]["productIds"][0], docs[1]["productIds"][0]
    r = client.get(f"/3/footprints?productId={a}&productId={b}",
                   headers={"Authorization": f"Bearer {t}"})
    assert len(r.json()["data"]) == 2


def test_filter_values_match_case_insensitively(env):
    client, _, creds, docs = env
    t = _token(client, creds)
    upper = docs[0]["productIds"][0].upper()
    r = client.get("/3/footprints", params={"productId": upper},
                   headers={"Authorization": f"Bearer {t}"})
    assert len(r.json()["data"]) == 1


def test_filters_are_and_between_criteria(env):
    client, _, creds, docs = env
    t = _token(client, creds)
    r = client.get("/3/footprints", headers={"Authorization": f"Bearer {t}"}, params={
        "productId": docs[0]["productIds"][0], "geography": "FR"})
    assert r.json()["data"] == []


def test_an_empty_result_is_an_empty_array_not_null(env):
    client, _, creds, _ = env
    t = _token(client, creds)
    body = client.get("/3/footprints", params={"productId": "urn:nope"},
                      headers={"Authorization": f"Bearer {t}"}).json()
    assert body["data"] == []


def test_pagination_never_exceeds_the_limit_and_emits_a_next_link(env):
    client, _, creds, _ = env
    t = _token(client, creds)
    r = client.get("/3/footprints", params={"limit": 2},
                   headers={"Authorization": f"Bearer {t}"})
    assert len(r.json()["data"]) == 2
    link = r.headers.get("Link")
    assert link and 'rel="next"' in link and link.count("rel=") == 1


def test_the_last_page_has_no_next_link(env):
    client, _, creds, _ = env
    t = _token(client, creds)
    r = client.get("/3/footprints", params={"limit": 100},
                   headers={"Authorization": f"Bearer {t}"})
    assert "Link" not in r.headers


def test_a_pagination_link_is_replayable(env):
    """A one-shot cursor is non-conformant."""
    client, _, creds, _ = env
    t = _token(client, creds)
    r = client.get("/3/footprints", params={"limit": 2},
                   headers={"Authorization": f"Bearer {t}"})
    nxt = r.headers["Link"].split(">")[0].lstrip("<")
    path = nxt.split("://", 1)[1].split("/", 1)[1]
    a = client.get(f"/{path}", headers={"Authorization": f"Bearer {t}"})
    b = client.get(f"/{path}", headers={"Authorization": f"Bearer {t}"})
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()


def test_the_odata_filter_syntax_is_rejected(env):
    """Deprecated in v3."""
    client, _, creds, _ = env
    t = _token(client, creds)
    r = client.get("/3/footprints?$filter=created%20ge%20'2027-01-01'",
                   headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 400
    assert r.json()["code"] == "BadRequest"


def test_get_footprint_by_id(env):
    client, _, creds, docs = env
    t = _token(client, creds)
    r = client.get(f"/3/footprints/{docs[0]['id']}",
                   headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 200
    assert r.json()["data"]["id"] == docs[0]["id"]


def test_an_unknown_footprint_id_returns_a_json_error_body(env):
    """A bare 404 with no body fails the assertion."""
    client, _, creds, _ = env
    t = _token(client, creds)
    r = client.get("/3/footprints/00000000-0000-0000-0000-000000000000",
                   headers={"Authorization": f"Bearer {t}"})
    assert r.status_code == 404
    assert r.json()["code"] == "NotFound" and r.json()["message"]


def test_events_require_the_cloudevents_media_type(env):
    client, _, creds, _ = env
    t = _token(client, creds)
    r = client.post("/3/events", headers={"Authorization": f"Bearer {t}"},
                    json=_event(EVENT_TYPES["published"], {"pfIds": []}))
    assert r.status_code == 400
    assert "structured content mode" in r.json()["message"]


def test_events_accept_the_media_type_with_a_charset(env):
    client, _, creds, docs = env
    t = _token(client, creds)
    r = client.post("/3/events",
                    headers={"Authorization": f"Bearer {t}",
                             "Content-Type": f"{CLOUDEVENTS_MEDIA_TYPE}; charset=UTF-8"},
                    content=json.dumps(_event(EVENT_TYPES["published"],
                                              {"pfIds": [docs[0]["id"]]})))
    assert r.status_code == 200


def test_a_request_created_event_is_answered_with_a_correlated_callback(env):
    client, _, creds, docs = env
    t = _token(client, creds)
    ev = _event(EVENT_TYPES["request_created"],
                {"productId": [docs[0]["productIds"][0]]})
    r = client.post("/3/events",
                    headers={"Authorization": f"Bearer {t}",
                             "Content-Type": CLOUDEVENTS_MEDIA_TYPE},
                    content=json.dumps(ev))
    assert r.status_code == 200
    cb = r.json()["callback"]
    assert cb["would_send"] == "fulfilled"
    assert cb["request_event_id"] == "run-1/13"     # byte-for-byte
    assert cb["footprints_matched"] == 1


def test_an_unmatched_request_sends_rejected_not_empty_fulfilled(env):
    client, _, creds, _ = env
    t = _token(client, creds)
    ev = _event(EVENT_TYPES["request_created"], {"productId": ["urn:nope"]})
    r = client.post("/3/events",
                    headers={"Authorization": f"Bearer {t}",
                             "Content-Type": CLOUDEVENTS_MEDIA_TYPE},
                    content=json.dumps(ev))
    cb = r.json()["callback"]
    assert cb["would_send"] == "rejected"
    assert "not conformant" in cb["note"]


def test_events_require_a_bearer_token(env):
    client, _, _, _ = env
    r = client.post("/3/events", headers={"Content-Type": CLOUDEVENTS_MEDIA_TYPE},
                    content=json.dumps(_event(EVENT_TYPES["published"], {"pfIds": []})))
    assert r.status_code == 401


def test_openid_discovery_advertises_the_token_endpoint(env):
    client, _, _, _ = env
    body = client.get("/.well-known/openid-configuration").json()
    assert body["token_endpoint"].endswith("/auth/token")
    assert "client_credentials" in body["grant_types_supported"]


def test_a_partner_only_sees_the_owning_organisations_footprints(env):
    client, _, creds, _ = env
    other = client.post("/organisations", params={"name": "Other"}).json()["api_key"]
    client.post("/pact/footprints/import", headers={"X-API-Key": other},
                params={"direction": "published"}, json=_doc())
    t = _token(client, creds)
    body = client.get("/3/footprints", headers={"Authorization": f"Bearer {t}"}).json()
    assert len(body["data"]) == 3       # not the other org's fourth
