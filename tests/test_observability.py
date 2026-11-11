"""Request logging, correlation ids and the query-string privacy rule.

The engine had no logging at all, so the vendor could not show that a disputed request
had even happened. These tests pin the two properties that make the new log usable as
evidence without turning it into a leak:

* a correlation id round-trips (inbound ``X-Request-ID`` honoured, otherwise generated,
  always returned to the caller and always present on the request's log line), and
* narrative, person-identifying query parameters — which several endpoints still take,
  and which the container's gunicorn access log writes verbatim — never reach a record.
"""
import io
import json
import logging
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import Base
import app.models  # noqa: F401  (registers tables on Base.metadata)
from app import main as main_mod
from app import observability, ratelimit

# A realistic value for one of the narrative query parameters (`resolution_note` on
# POST /assurance/findings/{finding_id}/resolve). It names a person, as the real ones do.
NARRATIVE = "Jane Doe of Acme Ltd admitted the 2024 electricity figure was overstated"


class Captured:
    """The service's own log output, as text and as parsed JSON records."""

    def __init__(self, buffer):
        self._buffer = buffer

    @property
    def text(self):
        return self._buffer.getvalue()

    @property
    def records(self):
        return [json.loads(line) for line in self.text.splitlines() if line.strip()]

    @property
    def requests(self):
        return [r for r in self.records if r.get("event") == "http_request"]

    def one_request(self):
        found = self.requests
        assert len(found) == 1, "expected exactly one request record, got {}:\n{}".format(
            len(found), self.text)
        return found[0]

    def clear(self):
        self._buffer.seek(0)
        self._buffer.truncate(0)


def _saved_levels():
    access = [(n, logging.getLogger(n).disabled, logging.getLogger(n).propagate)
              for n in observability._ACCESS_LOGGERS]
    return logging.getLogger().level, logging.getLogger("app").level, access


def _restore_levels(saved):
    observability.configure_logging(level="INFO", fmt="json")       # back to stdout
    logging.getLogger().setLevel(saved[0])
    logging.getLogger("app").setLevel(saved[1])
    for name, disabled, propagate in saved[2]:
        logging.getLogger(name).disabled = disabled
        logging.getLogger(name).propagate = propagate


@pytest.fixture
def logs():
    buffer = io.StringIO()
    saved = _saved_levels()
    observability.configure_logging(level="INFO", fmt="json", stream=buffer)
    try:
        yield Captured(buffer)
    finally:
        _restore_levels(saved)


def _session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def client():
    Session = _session_factory()

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()
    main_mod.app.dependency_overrides[main_mod.get_db] = override
    yield TestClient(main_mod.app)
    main_mod.app.dependency_overrides.clear()


@pytest.fixture
def tenant(client):
    """An authenticated organisation: (client, headers, org_id)."""
    created = client.post("/organisations", params={"name": "Observability Co"}).json()
    return client, {"X-API-Key": created["api_key"]}, created["id"]


# --- correlation id ----------------------------------------------------------------------
def test_generated_correlation_id_is_returned_and_logged(client, logs):
    response = client.get("/healthz")

    request_id = response.headers["X-Request-ID"]
    uuid.UUID(request_id)                       # a well-formed generated id

    record = logs.one_request()
    assert record["request_id"] == request_id    # the caller's id IS the log line's id
    assert record["method"] == "GET"
    assert record["route"] == "/healthz"
    assert record["status"] == 200
    assert record["duration_ms"] >= 0
    assert record["level"] == "INFO"


def test_inbound_correlation_id_round_trips(client, logs):
    response = client.get("/healthz", headers={"X-Request-ID": "support-case-4471"})

    assert response.headers["X-Request-ID"] == "support-case-4471"
    assert logs.one_request()["request_id"] == "support-case-4471"


@pytest.mark.parametrize("hostile", [
    "with space",
    "x" * 200,                                   # unbounded
    "line\nbreak",                               # would forge a second log line
    "quote\"and\\slash",
    "\x00control",
    "",
])
def test_unsafe_inbound_correlation_ids_are_rejected(hostile):
    assert observability.sanitize_request_id(hostile) is None


def test_overlong_inbound_correlation_id_is_replaced_not_logged(client, logs):
    hostile = "A" * 500
    response = client.get("/healthz", headers={"X-Request-ID": hostile})

    issued = response.headers["X-Request-ID"]
    uuid.UUID(issued)                            # a fresh id, not the caller's
    assert hostile not in logs.text
    assert logs.one_request()["request_id"] == issued


# --- the privacy rule --------------------------------------------------------------------
def test_narrative_query_parameter_never_reaches_a_log_record(client, logs):
    """The leak this logger must not reproduce, on a request that fails authorisation."""
    response = client.post("/assurance/findings/1/resolve",
                           params={"resolution_note": NARRATIVE},
                           headers={"X-API-Key": "not-a-real-key"})
    assert response.status_code == 401           # rejected — and still logged

    record = logs.one_request()                  # not vacuous: the request WAS recorded
    assert record["status"] == 401
    assert record["level"] == "WARNING"

    assert NARRATIVE not in logs.text
    assert "Jane Doe" not in logs.text
    assert "resolution_note" not in logs.text


def test_third_party_info_logging_is_not_promoted_to_stdout(logs):
    """A dependency that logs full URLs at INFO (httpx does) must not be able to put a
    query string in our log just because this service turned logging on."""
    logging.getLogger("httpx").info(
        "HTTP Request: POST http://host/assurance/findings/1/resolve?resolution_note=%s",
        NARRATIVE)
    logging.getLogger("httpx").warning("connect timeout")   # warnings still come through

    assert NARRATIVE not in logs.text
    assert "connect timeout" in logs.text


def test_route_template_is_logged_instead_of_the_resolved_path(client, logs):
    client.post("/assurance/findings/4071/resolve", params={"resolution_note": NARRATIVE},
                headers={"X-API-Key": "not-a-real-key"})

    record = logs.one_request()
    assert record["route"] == "/assurance/findings/{finding_id}/resolve"
    assert "/assurance/findings/4071/resolve" not in logs.text   # no resolved path, no id


def test_unmatched_path_is_logged_without_its_query_string(client, logs):
    response = client.get("/no/such/endpoint", params={"resolution_note": NARRATIVE})
    assert response.status_code == 404

    record = logs.one_request()
    assert record["route"] == "/no/such/endpoint"
    assert record["route_matched"] is False      # flagged: high cardinality, not a template
    assert NARRATIVE not in logs.text


# --- tenant attribution ------------------------------------------------------------------
def test_authenticated_request_is_attributed_to_its_tenant(tenant, logs):
    client, headers, org_id = tenant
    logs.clear()                                 # drop the org-creation request

    response = client.get("/runs", headers=headers)
    assert response.status_code == 200

    record = logs.one_request()
    assert record["org_id"] == org_id
    assert record["route"] == "/runs"


def test_request_that_resolved_no_tenant_logs_no_org(client, logs):
    client.get("/healthz")                       # unauthenticated by design

    record = logs.one_request()
    assert "org_id" not in record                # absent, not a misleading zero


def test_rejected_key_logs_the_request_but_no_tenant(client, logs):
    client.get("/runs", headers={"X-API-Key": "not-a-real-key"})

    record = logs.one_request()
    assert record["status"] == 401
    assert "org_id" not in record


# --- outermost registration --------------------------------------------------------------
def test_rate_limited_response_is_correlated_and_logged(client, logs):
    """Proof that the middleware is the OUTERMOST one: a 429 produced by the rate
    limiter still gets a correlation id and still produces a log line."""
    ratelimit.configure(1)
    try:
        assert client.get("/healthz").status_code == 200
        limited = client.get("/healthz")
        assert limited.status_code == 429
        uuid.UUID(limited.headers["X-Request-ID"])
        assert [r["status"] for r in logs.requests] == [200, 429]
    finally:
        ratelimit.configure(0)


# --- unhandled exceptions ----------------------------------------------------------------
def test_unhandled_exception_logs_traceback_and_correlates_the_response(logs):
    def exploding_db():
        raise RuntimeError("connection pool exhausted")

    main_mod.app.dependency_overrides[main_mod.get_db] = exploding_db
    try:
        client = TestClient(main_mod.app, raise_server_exceptions=False)
        response = client.get("/healthz")

        assert response.status_code == 500
        assert response.text == "Internal Server Error"   # unchanged response shape
        request_id = response.headers["X-Request-ID"]     # ... plus the correlation id

        record = logs.one_request()
        assert record["request_id"] == request_id         # ties the report to this line
        assert record["level"] == "ERROR"
        assert record["status"] == 500
        assert record["error_type"] == "RuntimeError"
        assert "RuntimeError: connection pool exhausted" in record["traceback"]
    finally:
        main_mod.app.dependency_overrides.clear()


# --- configuration -----------------------------------------------------------------------
@pytest.fixture
def restore_logging():
    saved = _saved_levels()
    yield
    _restore_levels(saved)


def test_level_and_format_come_from_the_environment(monkeypatch, restore_logging):
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    monkeypatch.setenv("LOG_FORMAT", "text")
    buffer = io.StringIO()

    observability.configure_logging(stream=buffer)

    assert logging.getLogger("app").level == logging.WARNING
    log = observability.get_logger("app.test")
    log.info("below the configured level")
    log.warning("at the configured level")

    out = buffer.getvalue()
    assert "below the configured level" not in out
    assert "at the configured level" in out
    assert not out.lstrip().startswith("{")      # text, not JSON


def test_defaults_are_json_at_info(monkeypatch, restore_logging):
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    buffer = io.StringIO()

    observability.configure_logging(stream=buffer)

    assert logging.getLogger("app").level == logging.INFO        # this service: INFO
    assert logging.getLogger().level == logging.WARNING          # everything else: quiet
    observability.get_logger("app.test").info("hello")
    assert json.loads(buffer.getvalue().strip())["msg"] == "hello"


def test_unparseable_level_falls_back_instead_of_failing_boot(monkeypatch, restore_logging):
    monkeypatch.setenv("LOG_LEVEL", "VERBOSE")   # not a level
    monkeypatch.setenv("LOG_FORMAT", "yaml")     # not a format
    buffer = io.StringIO()

    observability.configure_logging(stream=buffer)

    assert logging.getLogger("app").level == logging.INFO
    warnings = [r for r in (json.loads(l) for l in buffer.getvalue().splitlines() if l.strip())
                if r["level"] == "WARNING"]
    assert any("VERBOSE" in r["msg"] for r in warnings)
    assert any("yaml" in r["msg"] for r in warnings)


def test_reconfiguring_does_not_duplicate_lines(restore_logging):
    buffer = io.StringIO()
    observability.configure_logging(level="INFO", fmt="json", stream=buffer)
    observability.configure_logging(level="INFO", fmt="json", stream=buffer)

    observability.get_logger("app.test").info("once")

    assert [json.loads(l)["msg"] for l in buffer.getvalue().splitlines() if l.strip()] == ["once"]

# --- the server's own access log, which this module must silence -------------------------
def test_server_access_log_is_silenced_by_default(restore_logging):
    """uvicorn writes its access line as `path?query_string` verbatim. It has its own
    handlers and propagate=False, so configure_logging is the only thing that can stop
    it — and must, or the leak sits on the line next to the clean record."""
    logging.getLogger("uvicorn.access").disabled = False
    logging.getLogger("uvicorn.access").propagate = True

    observability.configure_logging(stream=io.StringIO())

    for name in observability._ACCESS_LOGGERS:
        assert logging.getLogger(name).disabled is True
        assert logging.getLogger(name).propagate is False


def test_access_log_record_cannot_reach_our_handler(logs):
    # WARNING, not INFO, and deliberately: at INFO the third-party level alone would stop
    # the record, and this test would pass whether or not the access log is suppressed.
    # At WARNING only the suppression itself can keep it out.
    logging.getLogger("uvicorn.access").warning(
        '127.0.0.1:1 - "POST /assurance/findings/1/resolve?resolution_note=%s HTTP/1.1" 401',
        NARRATIVE)

    assert logs.text == ""
    assert NARRATIVE not in logs.text


def test_access_log_can_be_re_enabled_deliberately(monkeypatch, restore_logging):
    monkeypatch.setenv("LOG_ACCESS_LOG", "1")

    observability.configure_logging(stream=io.StringIO())

    assert logging.getLogger("uvicorn.access").disabled is False


# --- log injection -----------------------------------------------------------------------
def test_a_request_path_cannot_forge_a_log_line_in_text_format(client, restore_logging):
    """`GET /%0A<a plausible line>` arrives with a REAL newline in scope["path"], because
    ASGI servers url-decode it. In a line-oriented format that would be a forged entry
    from an unauthenticated caller."""
    buffer = io.StringIO()
    observability.configure_logging(level="INFO", fmt="text", stream=buffer)

    client.get("/%0A2026-01-01T00:00:00Z INFO app.request FORGED-admin-login-ok")

    lines = [l for l in buffer.getvalue().splitlines() if l.strip()]
    assert len(lines) == 1                                  # one request, one line
    assert not any(l.startswith("2026-01-01T00:00:00Z") for l in lines)
    assert "\\x0a" in lines[0]                               # the newline, neutralised


def test_control_characters_in_a_logged_value_are_escaped(restore_logging):
    buffer = io.StringIO()
    observability.configure_logging(level="INFO", fmt="text", stream=buffer)

    observability.get_logger("app.test").info("a\nb\x00c")

    assert len(buffer.getvalue().strip().splitlines()) == 1
    assert "\\x0a" in buffer.getvalue() and "\\x00" in buffer.getvalue()


def test_inert_leaves_ordinary_text_alone():
    assert observability._inert("/assurance/findings/4071/resolve") == \
        "/assurance/findings/4071/resolve"


def test_unmatched_path_label_is_escaped_at_the_source():
    """Pins the escaping where the label is built, not only in the text formatter. The
    two are independent layers and the formatter's covers for this one end to end, so
    without this test the source escaping could be deleted with the suite still green."""
    label, matched = observability.route_template(
        {"type": "http", "path": "/\n2026-01-01T00:00:00Z INFO forged"})

    assert matched is False
    assert "\n" not in label
    assert label == "/\\x0a2026-01-01T00:00:00Z INFO forged"


def test_unmatched_path_label_is_truncated():
    label, matched = observability.route_template({"type": "http", "path": "/" + "a" * 500})

    assert matched is False
    assert len(label) <= observability._MAX_PATH_CHARS + 3
    assert label.endswith("...")


# --- the second credential path ----------------------------------------------------------
class _StubQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *a, **k):
        return self

    def first(self):
        return self._result


class _StubSession:
    """Enough of a Session for _engagement_for_reader; an engagement needs a run row and
    a run needs more besides, and none of that is what this test is about."""

    def __init__(self, engagement, org=None):
        self._engagement, self._org = engagement, org

    def get(self, model, pk):
        return self._engagement

    def query(self, model):
        return _StubQuery(self._org)


def test_assurance_reader_attributes_the_tenant_on_both_credentials():
    """_engagement_for_reader is a SECOND credential path that does not go through
    current_org, so it has to bind the tenant itself — otherwise the log line for an
    assurance read says nothing about whose data was read."""
    from app.main import _engagement_for_reader, _hash_key

    engagement = SimpleNamespace(id=1, organisation_id=7,
                                 access_token_hash=_hash_key("assuror-token"))

    ctx, token = observability.bind_request("rid")
    try:
        _, role = _engagement_for_reader(_StubSession(engagement), 1,
                                         x_api_key=None, x_assurance_token="assuror-token")
        assert role == "assuror"
        assert ctx["org_id"] == 7              # the org whose data the assuror read
    finally:
        observability._CURRENT.reset(token)

    owner_engagement = SimpleNamespace(id=1, organisation_id=7, access_token_hash=None)
    ctx, token = observability.bind_request("rid")
    try:
        _, role = _engagement_for_reader(
            _StubSession(owner_engagement, org=SimpleNamespace(id=7)), 1,
            x_api_key="owner-key", x_assurance_token=None)
        assert role == "owner"
        assert ctx["org_id"] == 7
    finally:
        observability._CURRENT.reset(token)


# --- browser-readable correlation id -----------------------------------------------------
def test_correlation_id_is_readable_by_the_browser_client(client):
    """A browser cannot read a response header unless CORS exposes it; without this the
    product's own SPA could not show the id a user is meant to quote."""
    response = client.get("/healthz", headers={"Origin": "http://localhost:5173"})

    exposed = response.headers.get("access-control-expose-headers", "")
    assert "X-Request-ID" in exposed
    assert response.headers["X-Request-ID"]


# --- documented limits -------------------------------------------------------------------
def test_preflight_logs_a_sanitised_path_and_still_no_query_string(client, logs):
    """A CORS preflight is answered by CORSMiddleware, so the router never runs and there
    is no route template. The record falls back to the path — which still cannot contain
    a query string, because ASGI keeps those in a separate scope key."""
    client.options("/assurance/findings/4071/resolve",
                   params={"resolution_note": NARRATIVE},
                   headers={"Origin": "http://localhost:5173",
                            "Access-Control-Request-Method": "POST"})

    record = logs.one_request()
    assert record["route"] == "/assurance/findings/4071/resolve"
    assert record["route_matched"] is False
    assert NARRATIVE not in logs.text
    assert "resolution_note" not in logs.text


def test_500_record_carries_the_exception_message_a_documented_limit(logs):
    """Pins a known limit rather than a guarantee: the traceback is the exception's own
    text, and application code may have built that message out of request data. This
    logger does not put it there and cannot reliably take it out — the error log already
    carried the same traceback before this module existed. 500 records therefore need the
    same handling as the data itself."""
    app = FastAPI()

    @app.get("/boom")
    def boom(resolution_note: str = ""):
        int(resolution_note)                   # the ValueError embeds the value

    observability.install_observability(app)
    TestClient(app, raise_server_exceptions=False).get(
        "/boom", params={"resolution_note": NARRATIVE})

    record = logs.one_request()
    assert record["error_type"] == "ValueError"
    assert NARRATIVE in record["traceback"]    # the limit, asserted so nobody claims else
    assert record["route"] == "/boom"          # the request line itself stays clean
    assert NARRATIVE not in record["msg"]

