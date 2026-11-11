"""Application logging, request correlation and tenant attribution.

One definition of the whole concern, imported everywhere. Other modules take a logger
from :func:`get_logger` and never call ``logging.getLogger`` with an ad-hoc format of
their own, so every line this service emits has the same shape and the same fields.

What it gives a support engineer
--------------------------------
Every HTTP request produces exactly one structured record carrying the method, the
matched **route template**, the status, the duration, the organisation (tenant) that the
request authenticated as, and a correlation id that is also returned to the caller in the
``X-Request-ID`` response header. A customer who disputes a published figure can quote
that header; the vendor can then find the one log line for that request, including the
tenant it ran as and, when it failed, the traceback.

The privacy rule — read before adding a field
---------------------------------------------
This service accepts narrative and person-identifying free text in QUERY STRINGS
(assurance finding descriptions, assuror names, resolution notes — 19 such parameters
across 16 endpoints at the time of writing). The shipped gunicorn access log
(``--access-logfile -``) already writes those verbatim to stdout, including for requests
that fail authorisation. This logger must not make that worse, so:

* we log the **route template** (``/assurance/findings/{finding_id}/resolve``), never the
  resolved path, and
* we never read ``scope["query_string"]`` at all — not to log it, not to redact it. ASGI
  keeps the query string in its own scope key, so simply never touching that key means no
  code path here can leak one.

Two honest limits on that, because the guarantee is about the query string and not about
every byte of a request:

* **Raw paths.** ``scope["route"]`` is set by FastAPI's router, so it is absent whenever
  the router did not run or did not own the request — a CORS preflight answered by
  ``CORSMiddleware``, a static asset or SPA fallback served by the mounted frontend, or a
  path that matched nothing. Those records carry the request's path instead, flagged
  ``route_matched: false``. A path never contains the query string (ASGI keeps them in
  separate scope keys), so none of the parameters above can appear there, but a path
  *does* carry ids. It is truncated, and every control character in it is escaped, so a
  caller cannot forge a log line through it (see :func:`_inert`).
* **Tracebacks.** The ``traceback`` field on a 500 is the exception's own text, and
  application code may have built that message out of request data — ``int(note)`` on a
  bad value puts the value in the ``ValueError``. This logger does not put it there and
  cannot reliably take it out; the error log already carried the same traceback before
  this module existed. Treat 500 records as needing the same handling as the data itself.

The service's *other* writers of request lines are silenced here rather than left to
contradict all of the above: see :func:`configure_logging` and ``LOG_ACCESS_LOG``.

Configuration (environment)
---------------------------
``LOG_LEVEL``              level for THIS service's loggers, the ``app.*`` tree
                           (``DEBUG``/``INFO``/``WARNING``/``ERROR``/``CRITICAL``;
                           default ``INFO``).
``LOG_FORMAT``             ``json`` (default — one object per line on stdout, for a
                           container log collector) or ``text`` (a compact human line).
``LOG_LEVEL_THIRD_PARTY``  level for everything else: libraries and the web server
                           (default ``WARNING``).
``LOG_ACCESS_LOG``         ``1`` to re-enable the web server's own access log (default
                           off). Leave it off: uvicorn writes that line as
                           ``path?query_string`` verbatim, which publishes exactly the
                           free text described above, for every request including the
                           ones that fail authorisation.

The two levels are separate on purpose, and it is the privacy rule above that makes them
so. A dependency's INFO records routinely contain the full request URL with its query
string attached — ``httpx`` logs exactly that — so a single root-level ``INFO`` would
quietly re-open, through someone else's logger, the leak this module takes care to avoid.
Third-party warnings and errors still come through, formatted like everything else.

An unparseable value for any of the three falls back to its default and says so in a
warning, rather than failing the boot of an otherwise healthy service.
"""
import contextvars
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from starlette.responses import PlainTextResponse

__all__ = [
    "REQUEST_ID_HEADER", "configure_logging", "get_logger", "bind_tenant",
    "current_request_id", "current_org_id", "request_id_from_scope",
    "ObservabilityMiddleware", "unhandled_exception_handler", "install_observability",
]

REQUEST_ID_HEADER = "X-Request-ID"

#: Where the middleware parks the correlation id on the ASGI scope. The scope dict is
#: shared by reference all the way out to Starlette's ServerErrorMiddleware, so the
#: unhandled-exception handler can still read the id after the middleware has unwound.
SCOPE_REQUEST_ID = "observability.request_id"

#: A correlation id is echoed back to the caller and written into logs, so an inbound one
#: is untrusted input: anything outside this shape is discarded and replaced with a fresh
#: id (JSON encoding already neutralises injection, but an unbounded or control-character
#: id would still corrupt downstream log tooling).
_SAFE_REQUEST_ID = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")

#: Cap on the unmatched-path fallback described in the module docstring.
_MAX_PATH_CHARS = 120

#: Control characters, including the newline. Anything taken from a request and written
#: to a log line has to have these neutralised: a request for ``/%0A<a plausible line>``
#: arrives with a real newline in ``scope["path"]`` (ASGI servers url-decode it), and in
#: a line-oriented format that is a forged log entry from an unauthenticated caller.
#: JSON escapes them on its own; the text format does not, so the escaping is done at the
#: source instead of trusting the formatter.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: Access loggers of the servers this app runs under. They write the full request target,
#: query string included, and cannot be reached by this module's levels (uvicorn's worker
#: gives them their own handlers and sets ``propagate = False``).
_ACCESS_LOGGERS = ("uvicorn.access", "gunicorn.access")

_DEFAULT_LEVEL = "INFO"
_DEFAULT_FORMAT = "json"
_DEFAULT_THIRD_PARTY_LEVEL = "WARNING"

#: This service's logger tree. Every logger from :func:`get_logger` for a module under
#: ``app/`` lands beneath it, so one ``setLevel`` governs our own verbosity without
#: touching anybody else's.
_APP_LOGGER = "app"

# --- per-request state -------------------------------------------------------------------
# The context variable holds a MUTABLE dict, which is load-bearing rather than lazy:
# FastAPI runs *synchronous* dependencies (``current_org`` is one) in a worker thread that
# gets a COPY of the context, so a plain ContextVar.set() inside one never reaches the
# middleware that is waiting on the response. Mutating a dict both sides already hold does
# reach it. Each request installs its own dict, so nothing is shared between requests.
_CURRENT: "contextvars.ContextVar[Optional[Dict[str, Any]]]" = contextvars.ContextVar(
    "observability_request", default=None)

# Marks the handler this module installs, so re-configuring replaces ours and leaves
# anyone else's alone (pytest's caplog, gunicorn's own handlers).
_OURS = "_carbon_observability_handler"


def _inert(text: str) -> str:
    """Escape control characters so `text` cannot forge or break a log line."""
    return _CONTROL_CHARS.sub(lambda m: "\\x{:02x}".format(ord(m.group())), text)


def new_request_id() -> str:
    return str(uuid.uuid4())


def sanitize_request_id(value: Optional[str]) -> Optional[str]:
    """Return `value` if it is a safe correlation id, else None (caller generates one)."""
    if not value:
        return None
    return value if _SAFE_REQUEST_ID.match(value) else None


def bind_request(request_id: str) -> Tuple[Dict[str, Any], Any]:
    """Start a request's logging context. Returns (context, reset-token)."""
    ctx: Dict[str, Any] = {"request_id": request_id, "org_id": None}
    return ctx, _CURRENT.set(ctx)


def bind_tenant(org_id: Any) -> None:
    """Attribute the request in flight to an organisation.

    Called once the caller's API key has been resolved to a tenant, so that every record
    for the request — including the one the middleware writes after the response — says
    which organisation's data was touched. A no-op outside a request.
    """
    ctx = _CURRENT.get()
    if ctx is not None:
        ctx["org_id"] = org_id


def current_request_id() -> Optional[str]:
    ctx = _CURRENT.get()
    return ctx.get("request_id") if ctx else None


def current_org_id() -> Optional[Any]:
    ctx = _CURRENT.get()
    return ctx.get("org_id") if ctx else None


def request_id_from_scope(scope: Dict[str, Any]) -> Optional[str]:
    value = scope.get(SCOPE_REQUEST_ID)
    return value if isinstance(value, str) else None


# --- formatters --------------------------------------------------------------------------
_RESERVED = frozenset(vars(logging.makeLogRecord({})))


def _record_fields(record: logging.LogRecord) -> Dict[str, Any]:
    """Structured fields for a record: those passed as ``extra={"fields": {...}}``, plus
    anything handed to ``extra=`` directly, plus the request context when one is open."""
    fields: Dict[str, Any] = {}
    for key, value in vars(record).items():
        if key not in _RESERVED and key != "fields":
            fields[key] = value
    explicit = getattr(record, "fields", None)
    if isinstance(explicit, dict):
        fields.update(explicit)
    fields.setdefault("request_id", current_request_id())
    fields.setdefault("org_id", current_org_id())
    return {k: v for k, v in fields.items() if v is not None}


def _timestamp(record: logging.LogRecord) -> str:
    moment = datetime.fromtimestamp(record.created, tz=timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for a container log collector."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        payload.update(_record_fields(record))
        if record.exc_info:
            payload["error_type"] = getattr(record.exc_info[0], "__name__", "Exception")
            payload["traceback"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        # default=str so an unserialisable extra degrades to its repr instead of losing
        # the whole line; sort_keys for stable, diffable output.
        return json.dumps(payload, default=str, sort_keys=True)


class TextFormatter(logging.Formatter):
    """A compact single line for local work: `ts LEVEL logger message key=value ...`."""

    def format(self, record: logging.LogRecord) -> str:
        # Escaped, not merely formatted: this format is line-oriented, so one newline in
        # a logged value would otherwise become a second, forged entry. The traceback is
        # left multi-line on purpose (it is unreadable otherwise), which is one more
        # reason JSON is the default and the format anything machine-read should use.
        fields = _record_fields(record)
        tail = " ".join("{}={}".format(k, _inert(str(fields[k]))) for k in sorted(fields))
        line = "{} {:<7} {} {}".format(
            _timestamp(record), record.levelname, record.name, _inert(record.getMessage()))
        if tail:
            line = "{} {}".format(line, tail)
        if record.exc_info:
            line = "{}\n{}".format(line, self.formatException(record.exc_info))
        return line


# --- configuration -----------------------------------------------------------------------
def _resolve_level(value: Optional[str], env_var: str,
                   default: str) -> Tuple[int, Optional[str]]:
    """(numeric level, rejected-name-or-None)."""
    name = (value or os.environ.get(env_var) or default).strip().upper()
    numeric = logging.getLevelName(name)
    if isinstance(numeric, int):
        return numeric, None
    return logging.getLevelName(default), name


def _resolve_formatter(fmt: Optional[str]) -> Tuple[logging.Formatter, Optional[str]]:
    name = (fmt or os.environ.get("LOG_FORMAT") or _DEFAULT_FORMAT).strip().lower()
    if name == "json":
        return JsonFormatter(), None
    if name == "text":
        return TextFormatter(), None
    return JsonFormatter(), name


def _configure_access_loggers(enabled: bool) -> None:
    """Silence the web server's own access log unless ``LOG_ACCESS_LOG=1``.

    This is the one place that can do it. uvicorn's worker hands ``uvicorn.access`` its
    own handlers and sets ``propagate = False``, so neither the levels above nor the
    handler below can reach it — and what it writes is
    ``"POST /assurance/findings/4071/resolve?resolution_note=<a person's name> HTTP/1.1"``,
    for every request, authorised or not. Without this, the structured line and the leak
    it was written to avoid sit on adjacent lines of the same stdout, which is how the
    documented dev command (``uvicorn app.main:app --reload``) behaved.

    Suppressed here rather than only in the container's CMD, so that a developer, a
    one-off ``uvicorn`` run and a platform that supplies its own start command all get
    the same guarantee.
    """
    for name in _ACCESS_LOGGERS:
        logger = logging.getLogger(name)
        logger.disabled = not enabled
        if not enabled:
            # Belt and braces: with no handlers of its own it would otherwise propagate
            # up to our handler and be published after all.
            logger.propagate = False


def configure_logging(level: Optional[str] = None, fmt: Optional[str] = None,
                      stream: Optional[Any] = None,
                      third_party_level: Optional[str] = None,
                      access_log: Optional[bool] = None) -> logging.Handler:
    """Install this service's logging configuration.

    One handler on the root logger (so every record is formatted alike), but two levels:
    ``LOG_LEVEL`` for the ``app.*`` tree and ``LOG_LEVEL_THIRD_PARTY`` for everything
    else. See the module docstring for why that split is a privacy control and not a
    tuning knob. The server's own access log is silenced for the same reason — see
    :func:`_configure_access_loggers`.

    Idempotent: calling it again replaces the handler this module installed and leaves
    every other handler in place, so re-configuring never duplicates our lines and never
    unhooks someone else's capture (pytest's, gunicorn's).
    """
    numeric, bad_level = _resolve_level(level, "LOG_LEVEL", _DEFAULT_LEVEL)
    third_numeric, bad_third = _resolve_level(
        third_party_level, "LOG_LEVEL_THIRD_PARTY", _DEFAULT_THIRD_PARTY_LEVEL)
    formatter, bad_format = _resolve_formatter(fmt)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _OURS, False):
            root.removeHandler(existing)

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    setattr(handler, _OURS, True)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(third_numeric)
    logging.getLogger(_APP_LOGGER).setLevel(numeric)

    if access_log is None:
        access_log = os.environ.get("LOG_ACCESS_LOG", "").strip().lower() in ("1", "true", "yes")
    _configure_access_loggers(access_log)

    log = get_logger("{}.observability".format(_APP_LOGGER))
    if bad_level:
        log.warning("ignoring unknown LOG_LEVEL %r; using %s", bad_level, _DEFAULT_LEVEL)
    if bad_third:
        log.warning("ignoring unknown LOG_LEVEL_THIRD_PARTY %r; using %s", bad_third,
                    _DEFAULT_THIRD_PARTY_LEVEL)
    if bad_format:
        log.warning("ignoring unknown LOG_FORMAT %r; using %s", bad_format, _DEFAULT_FORMAT)
    return handler


def get_logger(name: str) -> logging.Logger:
    """The one way this codebase obtains a logger."""
    return logging.getLogger(name)


# --- request logging ---------------------------------------------------------------------
def _header(scope: Dict[str, Any], name: bytes) -> Optional[str]:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            try:
                return value.decode("latin-1")
            except Exception:
                return None
    return None


def route_template(scope: Dict[str, Any]) -> Tuple[str, bool]:
    """(label, matched). The label is the matched route's template — never the resolved
    path and never the query string. See the module docstring's privacy rule."""
    route = scope.get("route")
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if template:
        return template, True
    path = scope.get("path") or "-"
    if len(path) > _MAX_PATH_CHARS:
        path = path[:_MAX_PATH_CHARS] + "..."
    return _inert(path), False


class ObservabilityMiddleware:
    """Pure-ASGI request logging and correlation.

    Deliberately not a ``BaseHTTPMiddleware``: that runs the rest of the application in a
    child task, whose context the tenant binding could not mutate back into ours, and it
    buffers response bodies. A raw ASGI middleware keeps the request on one task, so the
    tenant that ``current_org`` resolves deep inside the handler is still known here when
    the response is logged.

    Register it LAST of the HTTP middlewares, so it is the outermost one and therefore
    sees — and stamps a correlation id onto — rate-limited and rejected responses too.
    """

    def __init__(self, app: Any, logger_name: str = "app.request") -> None:
        self.app = app
        self._log = get_logger(logger_name)

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request_id = sanitize_request_id(_header(scope, b"x-request-id")) or new_request_id()
        ctx, token = bind_request(request_id)
        scope[SCOPE_REQUEST_ID] = request_id
        state = scope.setdefault("state", {})
        if isinstance(state, dict):                      # also reachable as request.state
            state["request_id"] = request_id

        started = time.perf_counter()
        status: Dict[str, Optional[int]] = {"code": None}
        encoded_id = request_id.encode("ascii")

        async def send_wrapper(message: Dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                status["code"] = message.get("status")
                headers = list(message.get("headers") or ())
                if not any(k.lower() == b"x-request-id" for k, _ in headers):
                    headers.append((b"x-request-id", encoded_id))
                message = dict(message, headers=headers)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            # Logged here rather than in the exception handler so that there is exactly
            # one record per request, carrying the duration and tenant as well as the
            # traceback. Re-raised unchanged, so the server still handles it as before.
            self._emit(scope, started, 500, ctx, exc=exc)
            raise
        except BaseException:
            # Cancellation (client disconnect, worker shutdown). Still record that the
            # request happened — with no traceback and no invented status.
            self._emit(scope, started, status["code"], ctx, outcome="cancelled")
            raise
        else:
            self._emit(scope, started, status["code"], ctx)
        finally:
            _CURRENT.reset(token)

    def _emit(self, scope: Dict[str, Any], started: float, status: Optional[int],
              ctx: Dict[str, Any], exc: Optional[BaseException] = None,
              outcome: Optional[str] = None) -> None:
        label, matched = route_template(scope)
        fields: Dict[str, Any] = {
            "event": "http_request",
            "method": _inert(scope.get("method") or "-"),
            "route": label,
            "status": status,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "request_id": ctx.get("request_id"),
            "org_id": ctx.get("org_id"),
        }
        if not matched:
            fields["route_matched"] = False          # high-cardinality: do not aggregate
        if outcome:
            fields["outcome"] = outcome
        if exc is not None or (status or 0) >= 500:
            level = logging.ERROR
        elif (status or 0) >= 400:
            level = logging.WARNING
        else:
            level = logging.INFO
        self._log.log(level, "%s %s %s", fields["method"], label,
                      status if status is not None else "-",
                      extra={"fields": fields}, exc_info=exc)


async def unhandled_exception_handler(request: Any, exc: Exception) -> PlainTextResponse:
    """Starlette's own 500 response, plus the correlation id in the header.

    The body is byte-for-byte what Starlette would have returned, because the error
    *contract* is owned elsewhere; the only addition is ``X-Request-ID``, so a support
    request can be tied to the log line that holds the traceback. The middleware has
    already logged the exception, so this handler does not log it again.
    """
    request_id = request_id_from_scope(getattr(request, "scope", {}) or {})
    headers = {REQUEST_ID_HEADER: request_id} if request_id else None
    return PlainTextResponse("Internal Server Error", status_code=500, headers=headers)


def install_observability(app: Any) -> None:
    """Attach request logging, correlation ids and unhandled-exception reporting."""
    app.add_middleware(ObservabilityMiddleware)
    app.add_exception_handler(Exception, unhandled_exception_handler)
