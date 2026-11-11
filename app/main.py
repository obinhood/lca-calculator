import hashlib
import json as _json
from io import BytesIO as io_BytesIO
import math
import secrets
from typing import Optional

import pandas as pd
from fastapi import (FastAPI, UploadFile, File, Depends, Query, Header, HTTPException,
                     Request, Response)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.orm import Session

from .database import SessionLocal
from .models import (
    Organisation, ActivityRecord, EmissionFactor, ReportingPeriod, CalculationRun,
    MarketInstrument, FxRate, PriceIndex, factor_visible_to,
)
from .schemas import (
    CbamGoodBody, ConcludeBody, ConsolidationBody, DeliverabilityLinkBody,
    DisposeFindingBody, EngagementBody, EntityBody, FindingBody, Iso140642Body,
    LcaItemBody, MarketInstrumentBody, NatureImpactBody, PactClientBody,
    ResolveFindingBody, Sb253Body, Scope3DeclarationBody,
)
from .services import applicability
from .services.ingestion import parse_csv
from .services.qa import check_records
from .services.resolver import auto_map_activity
from .services.calc import compute_co2e, ReportingPeriodError, _parse_iso_date
from .services.gwp import SUPPORTED_GWP_SETS
from .services.uncertainty import (
    propagate, DEFAULT_CORRELATION, DEFAULT_ITERATIONS,
)
from .reports.summary import summary
from .reports.secr import secr_report
from .reports.sb253 import sb253_report
from .reports.esrs_e1 import esrs_e1_report
from .reports.cbam import cbam_declaration
from .reports.issb_s2 import issb_s2_report
from .reports.gri import gri_report
from .reports.cdp import cdp_export
from .reports.sbti import sbti_report
from .services.neutrality import neutrality_assessment
from .reports.framework_guidance import (
    FRAMEWORKS, list_frameworks, with_guidance,
)

app = FastAPI(title="Carbon Footprint MVP", version="0.3.0")

# Browser SPA (frontend/) runs on a different origin in dev; restrict to
# localhost by default, override with ALLOWED_ORIGINS for deployments.
import os as _os

# Structured application logging + request correlation. app/observability.py owns the
# whole concern — configuration, the correlation id, the tenant id — and records the
# matched ROUTE TEMPLATE, never the query string: a number of endpoints still take
# narrative, person-identifying free text as query parameters, and that text must not
# reach a log line. Configured here, before anything in this module can log.
from .observability import (
    REQUEST_ID_HEADER as _REQUEST_ID_HEADER,
    bind_tenant as _bind_tenant,
    configure_logging as _configure_logging,
    install_observability as _install_observability,
)
_configure_logging()

from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=_os.environ.get(
        "ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
    # A browser cannot read a response header unless it is exposed. Without this the
    # product's own SPA could not show a user the correlation id to quote in a support
    # report — which is the whole point of returning it.
    expose_headers=[_REQUEST_ID_HEADER],
)

# Per-key API rate limiting — OPT-IN via RATE_LIMIT_PER_MINUTE (absent => pass-through, so
# dev and tests are never throttled). Keyed by X-API-Key, falling back to client IP.
import time as _time
from .ratelimit import configure_from_env as _rl_configure_from_env, get_limiter as _rl_get
_rl_configure_from_env()


@app.middleware("http")
async def _rate_limit_mw(request, call_next):
    limiter = _rl_get()
    if limiter is not None:
        key = request.headers.get("x-api-key") or (
            request.client.host if request.client else "anon")
        allowed, retry_after = limiter.check(key, _time.monotonic())
        if not allowed:
            return JSONResponse(
                _error_body(429, f"rate limit exceeded — retry in {retry_after}s"),
                status_code=429, headers={"Retry-After": str(retry_after)})
    return await call_next(request)

# Request logging + correlation, registered LAST of the HTTP middlewares, which makes it
# the OUTERMOST one: it stamps a correlation id onto — and emits one log line for — even
# the 429s and 401s produced by the layers below.
_install_observability(app)

# Schema is managed by alembic (scripts/init_db.py runs `upgrade head` + seeds).
# A create_all here would create unstamped tables and diverge from the migration
# chain, so it was removed deliberately.

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_UPLOAD_ROWS = 50_000


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _require_body(body, *fields: str):
    """A narrative body that MUST be present, refused with what to send.

    Declared `Optional` in the signature on purpose: a required Pydantic parameter is
    validated before the handler runs, which would pre-empt the guard above and answer a
    stale client with a bare "Field required" instead of saying where its parameter went.
    """
    if body is None:
        raise HTTPException(
            status_code=422,
            detail="a JSON request body is required, carrying: " + ", ".join(fields))
    return body


def _reject_narrative_query_params(request: Request, *names: str) -> None:
    """Refuse a request still sending a narrative value in the QUERY STRING.

    These parameters moved into the request body. Quietly ignoring a stale client's
    query value would DROP the assurance note or the person's name it carried, and the
    write would report success with the narrative missing — a worse outcome than the
    logging problem being fixed. So it is refused, loudly, naming what to send instead.
    """
    sent = sorted(n for n in names if n in request.query_params)
    if not sent:
        return
    _refuse(400,
            "moved to the request body and no longer read from the URL: "
            + ", ".join(sent)
            + ". These carry narrative or person-identifying text, which the production "
              "access log records verbatim — including on requests that fail "
              "authorisation, with no deletion path. Resend them as JSON in the request "
              "body.",
            moved_parameters=sent, remedy="send these keys in the JSON request body")


def _page_args(limit=None, offset=None):
    """Normalise and validate a collection window, as a 400 on refusal.

    One gate for every paginated endpoint, so `limit` means the same thing (1..1000,
    never clamped) wherever a caller meets it.
    """
    from .services.pagination import PaginationError, params, validate
    limit, offset = params(limit, offset)
    try:
        validate(limit, offset)
    except PaginationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return limit, offset


def current_org(x_api_key: Optional[str] = Header(None),
                db: Session = Depends(get_db)) -> Organisation:
    """Resolve the calling organisation from its API key — the ONLY way any
    org-scoped endpoint identifies a tenant (org names are not credentials).
    A revoked key is rejected even though its hash still matches.

    A MISSING key is 401, not 422. Declared as `Header(...)` it was request-validation's
    problem, so omitting the header entirely produced a 422 with an array-shaped body
    while a WRONG key produced a 401 with a string — the same failure, unauthenticated,
    reported two different ways. A client could not write one branch for "your
    credential did not work".
    """
    if not x_api_key:
        raise HTTPException(status_code=401,
                            detail="missing API key — send it in the X-API-Key header")
    org = db.query(Organisation).filter(
        Organisation.api_key_hash == _hash_key(x_api_key)).first()
    if org is None or org.api_key_revoked:
        raise HTTPException(status_code=401, detail="invalid or revoked API key")
    # Attribute every log line for this request to the tenant it authenticated as.
    _bind_tenant(org.id)
    return org


# --- ONE error envelope, for every error this API returns --------------------
#
# `detail` was polymorphic: a STRING for everything a handler raised, and an ARRAY OF
# OBJECTS for anything FastAPI's own request validation rejected. A client could not read
# it without first testing its type, so clients that did not test rendered
# "[object Object]" to their users, and clients that did had to carry two parsers for one
# field.
#
# `detail` is now ALWAYS a string. Its MESSAGE CONTENT is unchanged for everything the
# handlers raise — the frontend surfaces it verbatim (frontend/src/api.ts) — and the
# structured material that used to displace it moves beside it, under `error.fields`,
# where a client can reach it without the shape of `detail` depending on which layer did
# the refusing.

_ERROR_CODES = {400: "bad_request", 401: "unauthenticated", 403: "forbidden",
                404: "not_found", 405: "method_not_allowed", 409: "conflict",
                413: "payload_too_large", 415: "unsupported_media_type",
                422: "validation_error", 429: "rate_limited",
                500: "internal_error", 503: "unavailable"}


def _error_body(status: int, message: str, code: Optional[str] = None,
                fields: Optional[list] = None, context=None) -> dict:
    """The one body shape. `detail` for humans and for the existing clients that read
    it; `error` for a machine that wants the status, a stable code, the offending fields
    and — on a refusal that has something to say — the structure behind it."""
    err = {"status": status, "code": code or _ERROR_CODES.get(status, "error"),
           "message": message}
    if fields is not None:
        err["fields"] = fields
    if context is not None:
        err["context"] = context
    return {"detail": message, "error": err}


def _refuse(status: int, message: str, **context):
    """Raise a refusal that says why AND carries what the caller needs to act on it.

    A refusal in this codebase names what blocks it — which filed runs froze the row you
    tried to delete, which instrument your submission duplicates — because the caller
    cannot do anything with "no". That structure survives into `error.context` verbatim,
    while `detail` stays the human sentence.

    `detail` is deliberately NOT the object. Making it one would give the field a third
    shape and force every existing client, the frontend included, to reach through
    `detail["message"]` — a worse break than the two-shaped `detail` this replaced. A
    client reads `detail` to show a person; it reads `error.context` to branch on.
    """
    raise HTTPException(status_code=status, detail={"message": message, **context})


def _detail_parts(detail):
    """(human string, structured context or None) from whatever a handler raised."""
    if isinstance(detail, str):
        return detail, None
    if isinstance(detail, dict):
        msg = detail.get("message")
        if not isinstance(msg, str):
            msg = _json.dumps(detail, default=str, sort_keys=True)
        return msg, detail
    if isinstance(detail, (list, tuple)):
        return _json.dumps(detail, default=str), list(detail)
    return str(detail), None


def _is_pact_path(request: Request) -> bool:
    """The PACT host routes (/3/...) serve a SPEC-DEFINED error object, {code, message},
    which a conformance runner parses. It is not ours to re-shape, so those paths keep
    it — including when the refusal comes from request validation rather than the
    handler, which previously leaked FastAPI's 422 array into a PACT response."""
    return request.url.path.startswith("/3/")


@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError):
    fields = []
    for e in exc.errors():
        loc = list(e.get("loc") or [])
        fields.append({
            "location": str(loc[0]) if loc else "request",
            "name": ".".join(str(x) for x in loc[1:]) or None,
            "message": str(e.get("msg", "")),
            "type": str(e.get("type", "")),
        })
    message = "; ".join(f"{f['name'] or f['location']}: {f['message']}"
                        for f in fields) or "invalid request"
    if _is_pact_path(request):
        from .services.pact_host import error as _pact_error
        return JSONResponse(_pact_error("BadRequest", message), status_code=400)
    return JSONResponse(_error_body(422, message, fields=fields), status_code=422)


@app.exception_handler(StarletteHTTPException)
async def _on_http_exception(request: Request, exc: StarletteHTTPException):
    # A structured refusal keeps its structure. Flattening it to a string here would
    # lose exactly what the caller needs on the one path this codebase least wants to
    # lose information on — see _refuse.
    message, context = _detail_parts(exc.detail)
    if _is_pact_path(request):
        from .services.pact_host import error as _pact_error
        code = {400: "BadRequest", 401: "Unauthorized", 403: "AccessDenied",
                404: "NotFound"}.get(exc.status_code, "InternalError")
        return JSONResponse(_pact_error(code, message), status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))
    return JSONResponse(_error_body(exc.status_code, message, context=context),
                        status_code=exc.status_code,
                        headers=getattr(exc, "headers", None))


@app.get("/healthz")
def healthz(db: Session = Depends(get_db)):
    """Liveness + DB readiness for load balancers / orchestrators.

    A trivial `SELECT 1` proves the connection pool can actually reach the database, not just
    that the process is up. Returns 503 (not 200) when the DB is unreachable so a deploy
    target stops routing to a broken instance. Unauthenticated by design — no tenant data.
    """
    from sqlalchemy import text
    try:
        db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return JSONResponse(
        {"status": "ok" if db_ok else "degraded", "database": db_ok, "version": app.version},
        status_code=200 if db_ok else 503)


def _fx_rates(db: Session, as_of_year: Optional[int] = None) -> dict:
    """{(base, quote): (rate, year)} from the append-only FX reference table.

    Applicability thresholds are stated in the regime's own currency. Without a rate the
    comparison is not approximated — it is refused (see applicability._convert), because a
    EUR threshold silently tested against a GBP turnover is simply a wrong legal answer.

    The rate is picked for the year the FINANCIALS describe, not the newest row on file:
    converting 2026 turnover at a 2021 rate can flip a definite verdict, and the rate year
    is returned so the answer can say which one it used.
    """
    from .models import FxRate
    best: dict = {}
    # Append-only table: corrections INSERT a new row, so a later id for the same year
    # supersedes. Among years, prefer the one closest to (and not after) as_of_year.
    for r in db.query(FxRate).order_by(FxRate.id).all():
        key = (r.base_currency.upper(), r.quote_currency.upper())
        prev = best.get(key)
        if prev is None:
            best[key] = (r.rate, r.year); continue
        if as_of_year is None:
            if r.year >= prev[1]:
                best[key] = (r.rate, r.year)
            continue
        # closest at-or-before as_of_year, else the earliest available after it
        def score(y):
            return (0, as_of_year - y) if y <= as_of_year else (1, y - as_of_year)
        if score(r.year) <= score(prev[1]):
            best[key] = (r.rate, r.year)
    return best


def _check_sector(sector: Optional[str]) -> None:
    """An unrecognised sector is rejected, not stored. A free-text sector would be
    silently dropped by the relevance prior — the org would believe it had declared a
    sector while no sector challenge ever ran against its Scope 3 screening."""
    from .services import sectors
    if sector is not None and not sectors.is_valid(sector):
        raise HTTPException(
            status_code=400,
            detail=f"unknown sector {sector!r} — choose one of "
                   f"{', '.join(sorted(sectors.SECTORS))} (see GET /sectors)")


@app.get("/applicability")
def get_applicability(org: Organisation = Depends(current_org),
                      db: Session = Depends(get_db)):
    """Which disclosure regimes COMPEL a filing from this entity, and which merely apply.

    Indicative only. Every test that was applied is returned with its citation and the
    distance from the threshold, so the answer can be checked rather than trusted. An
    input the entity has not supplied yields 'cannot determine' — never 'not required'.
    """
    from .services.applicability_rules import RULES
    from .services.calc import _parse_iso_date
    d = _parse_iso_date(org.financials_as_of or "")
    return JSONResponse(applicability.evaluate(
        org, RULES, rates=_fx_rates(db, d.year if d else None)))


@app.get("/applicability/vocabulary")
def get_applicability_vocabulary():
    """Jurisdiction and listing-market codes accepted by POST /organisations/profile."""
    return {"jurisdictions": applicability.JURISDICTIONS,
            "listing_markets": applicability.LISTING_MARKETS}


@app.get("/sectors")
def list_sectors():
    """The sector taxonomy, with the Scope 3 categories each sector must defend excluding.

    Open (no key): this is reference data a caller needs BEFORE registering.
    """
    from .services import sectors
    return {
        "sectors": sectors.catalogue(),
        "what_sector_does": [
            "Routes the Scope 3 relevance challenge: excluding a category that dominates "
            "in your sector requires evidence specific to your entity (GHGP Scope 3 Ch.6 "
            "makes sector guidance a relevance criterion). You can still exclude it — you "
            "just have to say why the sector pattern does not hold for you.",
        ],
        "what_sector_does_not_do": [
            "It does not change any emission figure. Emissions are activity data x "
            "emission factor; no sector multiplier, uplift or estimate is applied to a "
            "measured number anywhere in this platform.",
            "Where a sector DOES key a factor — spend-based EEIO — it is the SUPPLIER's "
            "sector on the transaction, not your own.",
            "It does not decide which disclosure frameworks you must file. That is driven "
            "by size, listing status and jurisdiction, which this platform does not yet "
            "model.",
        ],
    }


@app.post("/organisations/profile")
def update_organisation_profile(
        sector: Optional[str] = None,
        employees: Optional[int] = Query(None, description="average FTE over the year"),
        annual_turnover: Optional[float] = Query(None, description="NET turnover/revenue"),
        balance_sheet_total: Optional[float] = Query(None, description="gross assets"),
        financials_currency: Optional[str] = Query(None, description="ISO 4217, e.g. EUR"),
        financials_as_of: Optional[str] = Query(None, description="ISO date the figures describe"),
        jurisdictions: Optional[str] = Query(
            None, description="comma-separated codes where the entity operates, e.g. EU,UK"),
        listed_markets: Optional[str] = Query(
            None, description="comma-separated markets the entity is listed on; "
                              "pass an empty string to record 'unlisted'"),
        org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """Set the entity profile: sector, size, where it operates and whether it is listed.

    Sector routes the Scope 3 relevance challenge (frozen per run). The size, jurisdiction
    and listing fields decide which regimes COMPEL a filing — a live question about the
    entity today, so they are deliberately not frozen onto past runs.

    Every field is optional and only supplied fields are written, so a caller can fill the
    profile in incrementally. An absent field stays absent rather than being reset — and
    an absent field makes an applicability answer 'cannot determine', never 'not required'.
    """
    from .services import sectors
    if sector is not None:
        _check_sector(sector)
        org.sector = sector
    if employees is not None:
        if employees < 0 or employees > 50_000_000:
            raise HTTPException(status_code=400,
                                detail="employees must be between 0 and 50,000,000")
        org.employees = employees
    for name_, val in (("annual_turnover", annual_turnover),
                       ("balance_sheet_total", balance_sheet_total)):
        if val is None:
            continue
        if not math.isfinite(val) or val < 0:
            raise HTTPException(status_code=400,
                                detail=f"{name_} must be a finite number >= 0")
        setattr(org, name_, val)
    if financials_currency is not None:
        from .services.units import _CURRENCIES
        code = financials_currency.strip().upper()
        if code not in _CURRENCIES:
            raise HTTPException(
                status_code=400,
                detail=f"financials_currency must be a known ISO 4217 code "
                       f"({', '.join(sorted(_CURRENCIES))})")
        org.financials_currency = code
    if financials_as_of is not None:
        from .services.calc import _parse_iso_date
        if _parse_iso_date(financials_as_of) is None:
            raise HTTPException(status_code=400,
                                detail="financials_as_of must be an ISO date (YYYY-MM-DD)")
        org.financials_as_of = financials_as_of
    if jurisdictions is not None:
        codes = [c.strip().upper() for c in jurisdictions.split(",") if c.strip()]
        unknown = [c for c in codes if c not in applicability.JURISDICTIONS]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown jurisdiction(s) {unknown} — choose from "
                       f"{', '.join(sorted(applicability.JURISDICTIONS))}")
        org.jurisdictions = _json.dumps(codes)
    if listed_markets is not None:
        markets = [c.strip().upper() for c in listed_markets.split(",") if c.strip()]
        unknown = [c for c in markets if c not in applicability.LISTING_MARKETS]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown market(s) {unknown} — choose from "
                       f"{', '.join(sorted(applicability.LISTING_MARKETS))}")
        org.listed_markets = _json.dumps(markets)

    # A monetary figure with no currency cannot be tested against any threshold, so it is
    # rejected at the boundary rather than silently stored as an untestable number.
    if (org.annual_turnover is not None or org.balance_sheet_total is not None) \
            and not org.financials_currency:
        raise HTTPException(
            status_code=400,
            detail="financials_currency is required when annual_turnover or "
                   "balance_sheet_total is set — a monetary figure with no currency "
                   "cannot be tested against any threshold")

    db.commit(); db.refresh(org)
    return {
        "id": org.id,
        "sector": org.sector,
        "sector_label": sectors.label(org.sector),
        "dominant_scope3_categories": sectors.dominant_categories(org.sector),
        "employees": org.employees,
        "annual_turnover": org.annual_turnover,
        "balance_sheet_total": org.balance_sheet_total,
        "financials_currency": org.financials_currency,
        "financials_as_of": org.financials_as_of,
        "jurisdictions": _json.loads(org.jurisdictions or "[]"),
        "listed_markets": _json.loads(org.listed_markets or "[]"),
        "note": "Existing calculation runs keep the sector frozen at their run time; "
                "recalculate to apply a new sector to a run's Scope 3 screening. Size, "
                "jurisdiction and listing apply immediately — see GET /applicability."}


@app.post("/organisations")
def register_organisation(name: str = Query(...), sector: Optional[str] = None,
                          x_registration_token: Optional[str] = Header(None),
                          db: Session = Depends(get_db)):
    """Register an organisation. The API key is returned ONCE — store it safely.

    Gated: if REGISTRATION_TOKEN is configured, registration requires a matching
    X-Registration-Token (prevents open squatting/abuse). Left open only when no
    token is configured (dev)."""
    import hmac
    reg_token = _os.environ.get("REGISTRATION_TOKEN")
    if reg_token:
        if not x_registration_token or not hmac.compare_digest(x_registration_token, reg_token):
            raise HTTPException(status_code=401, detail="registration requires a valid X-Registration-Token")
    if db.query(Organisation).filter(Organisation.name == name).first():
        raise HTTPException(status_code=409, detail=f"organisation {name!r} already exists")
    _check_sector(sector)
    key = secrets.token_urlsafe(32)
    org = Organisation(name=name, sector=sector, api_key_hash=_hash_key(key))
    db.add(org); db.commit(); db.refresh(org)
    return {"id": org.id, "name": org.name, "api_key": key,
            "note": "Store this key now; it is not retrievable later."}


@app.post("/organisations/rotate_key")
def rotate_api_key(org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """Rotate the calling org's API key. The old key stops working immediately;
    the new key is returned ONCE."""
    from .services.calc import _utcnow_iso
    new_key = secrets.token_urlsafe(32)
    org.api_key_hash = _hash_key(new_key)
    org.api_key_revoked = False
    org.key_rotated_at = _utcnow_iso()
    db.commit()
    return {"id": org.id, "api_key": new_key,
            "note": "New key — the previous key is now invalid. Store this now."}


@app.post("/organisations/revoke_key")
def revoke_api_key(confirm_org_name: str = Query(...),
                   org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """Revoke the calling org's API key (self-service kill switch). Requires the
    org name as confirmation; the org's data is retained but its key is disabled
    until an admin re-issues one via rotate on a restored key."""
    if confirm_org_name != org.name:
        raise HTTPException(status_code=400, detail="confirm_org_name does not match")
    org.api_key_revoked = True
    db.commit()
    return {"id": org.id, "revoked": True,
            "note": "Key disabled. Contact an administrator to re-enable access."}


@app.post("/activities/upload_csv")
async def upload_activities(file: UploadFile = File(...),
                            org: Organisation = Depends(current_org),
                            db: Session = Depends(get_db)):
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"upload exceeds {MAX_UPLOAD_BYTES} bytes")
    try:
        df = parse_csv(content, filename=file.filename)
    except Exception:
        raise HTTPException(status_code=400, detail="unable to parse CSV file")
    if len(df) > MAX_UPLOAD_ROWS:
        raise HTTPException(status_code=413, detail=f"upload exceeds {MAX_UPLOAD_ROWS} rows")
    df, issues = check_records(df)

    # Idempotency: an identical file already ingested for this org would silently
    # double-count every activity in it on a retry/double-click.
    upload_hash = hashlib.sha256(content).hexdigest()
    if db.query(ActivityRecord).filter(
            ActivityRecord.organisation_id == org.id,
            ActivityRecord.upload_hash == upload_hash).first():
        raise HTTPException(status_code=409,
                            detail="this exact file was already uploaded for this organisation")

    recs = []
    for _, r in df.iterrows():
        recs.append(ActivityRecord(
            organisation_id=org.id,
            date=str(r["date"]),
            category=(r["category"] or "").strip().lower(),
            subcategory=(str(r["subcategory"]) if r["subcategory"] is not None else "").strip(),
            description=r["description"],
            quantity=float(r["quantity"]) if pd.notna(r["quantity"]) else None,
            unit=(r["unit"] or "").strip(),
            geo=(r["geo"] or "GB").strip(),
            source_file=r["source_file"],
            upload_hash=upload_hash,
            # Optional consumption window. Absent (the normal case) leaves both NULL and
            # the record is attributed wholly by `date`, exactly as before.
            coverage_start=_coverage_cell(r, "coverage_start"),
            coverage_end=_coverage_cell(r, "coverage_end"),
            provenance="process",
        ))
    db.add_all(recs); db.commit()

    # Mapping policy (Gap 6): exact matches bind automatically; coarser matches
    # become suggestions in the review queue; nothing coarse binds silently.
    # needs_review rows are RE-proposed so a later, better catalog entry (e.g. a
    # new exact factor) upgrades or refreshes stale suggestions.
    statuses = {"auto": 0, "needs_review": 0, "unmapped": 0}
    try:
        for a in db.query(ActivityRecord).filter(
                ActivityRecord.organisation_id == org.id,
                ActivityRecord.factor_id.is_(None),
                ActivityRecord.mapping_status.in_(["unmapped", "needs_review", None])).all():
            statuses[auto_map_activity(db, a)] += 1
        db.commit()
    except Exception:
        db.rollback()   # activities stay ingested (unmapped); mapping can be retried
        return JSONResponse(status_code=207, content={
            "records_ingested": len(recs), "organisation_id": org.id,
            "mapping": None, "issues": issues + [
                "automatic mapping failed; activities are ingested but unmapped — retry upload or map via review queue"],
        })

    return JSONResponse({"records_ingested": len(recs), "organisation_id": org.id,
                         "mapping": statuses, "issues": issues})


# A realistic FULL-YEAR demo dataset for a mid-sized UK company across two sites — inlined so
# /demo/seed works in the container without shipping a sample file. Every row uses a category
# /subcategory/unit combination present in the demo factor catalogue so it AUTO-MAPS (nothing
# lands in the review queue), and monthly energy carries real seasonality so the dashboard and
# reports look like an actual inventory rather than a toy.
def _demo_activities():
    rows = []
    # Monthly electricity (kWh) — HQ + workshop; summer dip, winter peak.
    hq_elec =  [4200, 3900, 3600, 3200, 2900, 2700, 2650, 2700, 3000, 3400, 3900, 4300]
    ws_elec =  [2600, 2500, 2400, 2300, 2200, 2150, 2100, 2150, 2250, 2400, 2550, 2700]
    # Monthly gas (kWh) — heating-driven, near zero in summer.
    hq_gas =   [6800, 6200, 4900, 3100, 1500,  600,  400,  450, 1300, 3300, 5200, 6600]
    for m in range(12):
        d = f"2025-{m + 1:02d}-15"
        rows.append((d, "electricity", "", f"HQ office electricity — {d[:7]}", hq_elec[m], "kWh", "GB"))
        rows.append((d, "electricity", "", f"Workshop electricity — {d[:7]}", ws_elec[m], "kWh", "GB"))
        rows.append((d, "gas", "", f"HQ gas heating — {d[:7]}", hq_gas[m], "kWh", "GB"))
    # Quarterly standby-generator diesel (L) — Scope 1 fuel.
    for q, (d, litres) in enumerate([("2025-02-10", 180), ("2025-05-12", 120),
                                     ("2025-08-11", 95), ("2025-11-09", 210)]):
        rows.append((d, "diesel", "", f"Standby generator refuel Q{q + 1}", litres, "L", "GB"))
    # Business travel (Scope 3) — flights, rail, road.
    rows += [
        ("2025-02-18", "flight", "short_haul_economy", "London–Paris return, 2 pax", 1720, "pkm", "GB"),
        ("2025-04-22", "flight", "short_haul_economy", "London–Munich return, 3 pax", 5100, "pkm", "GB"),
        ("2025-06-09", "flight", "long_haul_economy", "London–New York return, 2 pax", 22200, "pkm", "GB"),
        ("2025-09-30", "flight", "long_haul_economy", "London–Singapore return, 1 pax", 21500, "pkm", "GB"),
        ("2025-03-01", "train", "average", "Commuter and intercity rail Q1", 8400, "pkm", "GB"),
        ("2025-06-02", "train", "average", "Commuter and intercity rail Q2", 7900, "pkm", "GB"),
        ("2025-09-01", "train", "average", "Commuter and intercity rail Q3", 6800, "pkm", "GB"),
        ("2025-12-01", "train", "average", "Commuter and intercity rail Q4", 8100, "pkm", "GB"),
        ("2025-03-12", "car", "average", "Pool car mileage Q1", 4200, "km", "GB"),
        ("2025-06-13", "car", "average", "Pool car mileage Q2", 3900, "km", "GB"),
        ("2025-09-15", "car", "average", "Pool car mileage Q3", 3600, "km", "GB"),
        ("2025-12-11", "car", "average", "Pool car mileage Q4", 4400, "km", "GB"),
    ]
    # Waste to landfill (kg), quarterly.
    for q, (d, kg) in enumerate([("2025-03-25", 1450), ("2025-06-24", 1310),
                                 ("2025-09-23", 1180), ("2025-12-19", 1520)]):
        rows.append((d, "waste", "landfill_msw", f"General waste to landfill Q{q + 1}", kg, "kg", "GB"))
    # Purchased goods & services (Scope 3, spend-based GBP) — shows the spend method and a
    # lower data-quality tier alongside the activity-based rows.
    rows += [
        ("2025-02-28", "spend", "professional_services", "Legal and audit fees", 48000, "GBP", "GB"),
        ("2025-07-31", "spend", "professional_services", "Consultancy retainer", 32000, "GBP", "GB"),
        ("2025-04-15", "spend", "it_equipment", "Laptop and server refresh", 26500, "GBP", "GB"),
        ("2025-10-20", "spend", "it_equipment", "Networking hardware", 9800, "GBP", "GB"),
        ("2025-05-30", "spend", "construction", "Workshop mezzanine fit-out", 74000, "GBP", "GB"),
    ]
    return rows


_DEMO_ACTIVITIES = _demo_activities()


@app.post("/demo/seed")
def seed_demo_data(org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """One-click demo: load a small sample activity set (if the org is empty) and run a
    calculation, so every report has data to show. Idempotent — safe to click again.

    Relies on the demo emission-factor catalog (seeded at startup by scripts.init_db); the
    auto-mapper binds each activity to a factor exactly as an upload would.
    """
    existing = db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == org.id).count()
    seeded = 0
    if existing == 0:
        recs = [ActivityRecord(
            organisation_id=org.id, date=d, category=cat, subcategory=sub, description=desc,
            quantity=float(qty), unit=unit, geo=geo, source_file="demo",
            upload_hash="demo_seed", provenance="process")
            for (d, cat, sub, desc, qty, unit, geo) in _DEMO_ACTIVITIES]
        db.add_all(recs); db.commit()
        seeded = len(recs)
        try:
            for a in db.query(ActivityRecord).filter(
                    ActivityRecord.organisation_id == org.id,
                    ActivityRecord.factor_id.is_(None),
                    ActivityRecord.mapping_status.in_(["unmapped", "needs_review", None])).all():
                auto_map_activity(db, a)
            db.commit()
        except Exception:
            db.rollback()

    # Everything the demo used to omit: a reporting period, an hourly day with
    # certificates, series keys, and a Scope 3 screen. Without them the demo showed a
    # spend-and-energy calculator and none of the rest of the product — and since the
    # intensity gates landed, an unscoped run would make SECR, CDP, ESRS E1 and EcoVadis
    # all refuse, which is a worse advert than no demo at all.
    from .services.demo_seed import seed_all
    # Screen-then-compute, which is the realistic workflow: a first pass shows which
    # Scope 3 categories actually carry lines, the screen is declared against that, and
    # the final run freezes the screen. Guessing the set instead produced a category
    # declared "included" with no lines — the demo shipping the defect the product
    # exists to catch.
    first_pass = compute_co2e(db, org.id, gwp_set="AR6")
    extras = seed_all(db, org.id, run=first_pass)

    # Always (re)compute so a run exists for the reports to read — now SCOPED to the
    # demo reporting period, which is what every intensity ratio needs.
    run = compute_co2e(db, org.id, gwp_set="AR6",
                       reporting_period_id=extras["reporting_period"]["id"])
    return JSONResponse({
        "seeded_activities": seeded,
        "already_had_activities": existing > 0,
        "run_id": run.id,
        "total_co2e_kg": run.total_co2e,
        "also_seeded": extras,
        "next": "Open the Dashboard to see the run, then the Reports tab to generate "
                "disclosures. Hourly Scope 2, period-over-period screening and the "
                "Scope 3 screen now have data too.",
    })


@app.get("/mappings/review")
def list_review_queue(request: Request, limit: Optional[int] = None,
                      offset: Optional[int] = None,
                      org: Organisation = Depends(current_org),
                      db: Session = Depends(get_db)):
    """The activities awaiting a human mapping decision.

    SHAPE CHANGE: was a bare JSON array truncated at 500 rows with NOTHING in the
    response saying it had been truncated — an org with 900 unmapped rows cleared 500 of
    them believing the queue was empty. Now an object carrying `activities` and the
    standard `page` block, so a short page is distinguishable from a finished queue.
    """
    from .services.pagination import next_link_headers, page_block, page_of_query
    limit, offset = _page_args(limit, offset)
    page = page_of_query(db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == org.id,
        ActivityRecord.mapping_status == "needs_review").order_by(ActivityRecord.id),
        limit, offset)
    out = []
    for a in page["rows"]:
        sf = a.suggested_factor
        out.append({
            "activity_id": a.id, "date": a.date, "category": a.category,
            "subcategory": a.subcategory, "description": a.description,
            "quantity": a.quantity, "unit": a.unit, "geo": a.geo,
            "mapping_basis": a.mapping_basis, "mapping_confidence": a.mapping_confidence,
            "suggested_factor": None if sf is None else {
                "id": sf.id, "source": sf.source, "version": sf.version,
                "category": sf.category, "subcategory": sf.subcategory,
                "geography": sf.geography, "unit": sf.unit, "value": sf.value,
            },
        })
    return JSONResponse({"activities": out, "page": page_block(page, limit, offset)},
                        headers=next_link_headers(request, page))


def _get_own_activity(db: Session, org: Organisation, activity_id: int) -> ActivityRecord:
    a = db.get(ActivityRecord, activity_id)
    if a is None or a.organisation_id != org.id:
        raise HTTPException(status_code=404, detail="activity not found for this organisation")
    return a


def _visible_factor(db: Session, org: Organisation, factor_id: int) -> EmissionFactor:
    """A factor this tenant may bind: platform reference data, or their own.

    Binding is a read of the value: a line item built on a factor carries that
    factor's number into the tenant's own run and lineage. So an id naming another
    tenant's PACT footprint must not resolve here, and must 404 exactly as a
    nonexistent id does — otherwise the response distinguishes "someone else's" from
    "no such row" and becomes an existence oracle over the table.
    """
    f = db.query(EmissionFactor).filter(
        EmissionFactor.id == factor_id, factor_visible_to(org.id)).first()
    if f is None:
        raise HTTPException(status_code=404, detail="emission factor not found")
    return f


@app.post("/mappings/{activity_id}/approve")
def approve_mapping(activity_id: int, org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
    a = _get_own_activity(db, org, activity_id)
    if a.mapping_status != "needs_review" or a.suggested_factor_id is None:
        raise HTTPException(status_code=400, detail="activity is not awaiting review")
    from .services.mapping_audit import record as _audit
    prev_factor, prev_status = a.factor_id, a.mapping_status
    a.factor_id = a.suggested_factor_id
    a.mapping_status = "approved"
    _audit(db, a, "approved", from_factor_id=prev_factor,
           from_status=prev_status, commit=False)
    db.commit()
    return {"activity_id": a.id, "factor_id": a.factor_id, "mapping_status": a.mapping_status}


@app.post("/mappings/{activity_id}/override")
def override_mapping(activity_id: int, factor_id: int = Query(...),
                     org: Organisation = Depends(current_org),
                     db: Session = Depends(get_db)):
    a = _get_own_activity(db, org, activity_id)
    factor = _visible_factor(db, org, factor_id)
    from .services.mapping_audit import record as _audit
    prev_factor, prev_status = a.factor_id, a.mapping_status
    a.factor_id = factor.id
    a.mapping_status = "overridden"
    a.mapping_confidence = 1.0   # human decision
    _audit(db, a, "overridden", from_factor_id=prev_factor,
           from_status=prev_status, commit=False)
    db.commit()
    return {"activity_id": a.id, "factor_id": a.factor_id, "mapping_status": a.mapping_status}


@app.post("/reporting_periods")
def create_reporting_period(label: str = Query(...),
                            start_date: Optional[str] = None, end_date: Optional[str] = None,
                            org: Organisation = Depends(current_org),
                            db: Session = Depends(get_db)):
    # Validate dates at the boundary: a malformed start/end silently mis-scopes or
    # zeroes a period-scoped run's footprint (the run still reports "complete").
    for nm, val in (("start_date", start_date), ("end_date", end_date)):
        if val is not None and _parse_iso_date(val) is None:
            raise HTTPException(status_code=400,
                                detail=f"{nm} must be an ISO date (YYYY-MM-DD)")
    if start_date and end_date and _parse_iso_date(start_date) > _parse_iso_date(end_date):
        raise HTTPException(status_code=400, detail="start_date must be on or before end_date")
    period = ReportingPeriod(organisation_id=org.id, label=label,
                             start_date=start_date, end_date=end_date, frozen=False)
    db.add(period); db.commit(); db.refresh(period)
    return {"id": period.id, "organisation_id": org.id, "label": period.label}


@app.post("/reporting_periods/{period_id}/freeze")
def freeze_reporting_period(period_id: int, org: Organisation = Depends(current_org),
                            db: Session = Depends(get_db)):
    period = db.get(ReportingPeriod, period_id)
    if period is None or period.organisation_id != org.id:
        raise HTTPException(status_code=404, detail="reporting period not found for this organisation")
    period.frozen = True
    db.commit()
    return {"id": period.id, "frozen": True}


@app.post("/market_instruments")
def create_market_instrument(request: Request, instrument_type: str = Query(...),
                             kg_co2e_per_kwh: float = Query(...),
                             coverage_kwh: Optional[float] = None,
                             gwp_set: str = Query("AR6"),
                             market: Optional[str] = None,
                             start_date: Optional[str] = None,
                             end_date: Optional[str] = None,
                             body: Optional[MarketInstrumentBody] = None,
                             org: Organisation = Depends(current_org),
                             db: Session = Depends(get_db)):
    from sqlalchemy.exc import IntegrityError
    _reject_narrative_query_params(request, "description")
    description = body.description if body else None
    allowed = {"supplier_specific", "ppa", "rec", "residual_mix"}
    contractual = {"supplier_specific", "ppa", "rec"}
    if instrument_type not in allowed:
        raise HTTPException(status_code=400, detail=f"instrument_type must be one of {sorted(allowed)}")
    gwp_set = (gwp_set or "").strip().upper()
    if gwp_set not in SUPPORTED_GWP_SETS:
        raise HTTPException(status_code=400,
                            detail=f"gwp_set must be one of {list(SUPPORTED_GWP_SETS)}")
    # Finiteness BEFORE any write: inf/nan would poison every future market total.
    if not math.isfinite(kg_co2e_per_kwh) or kg_co2e_per_kwh < 0:
        raise HTTPException(status_code=400, detail="kg_co2e_per_kwh must be a finite number >= 0")
    if coverage_kwh is not None and (not math.isfinite(coverage_kwh) or coverage_kwh <= 0):
        raise HTTPException(status_code=400, detail="coverage_kwh must be a finite number > 0")
    # Real certificates have a vintage: contractual instruments must be dated so a
    # single-year REC can't silently blanket an org's entire history.
    if instrument_type in contractual:
        if not (start_date and end_date):
            raise HTTPException(status_code=400,
                                detail="contractual instruments (rec/ppa/supplier_specific) require start_date and end_date")
        if _parse_iso_date(start_date) is None or _parse_iso_date(end_date) is None:
            raise HTTPException(status_code=400, detail="dates must be ISO format YYYY-MM-DD")
    # Idempotency, in the house style of /activities/upload_csv (hashes the file) and
    # /credits (refuses a duplicate registry serial). A market instrument submitted twice
    # is NOT a harmless retry: each copy declares its own coverage_kwh, the pool consumes
    # them cumulatively, and the published market-based Scope 2 falls by the duplicate's
    # worth — to zero where the claim is 100% renewable, while the disclosure gate
    # passed. There is no serial-number field on this model, so the system cannot tell
    # two identical certificates apart from one sent twice; it refuses, and says how to
    # declare a genuinely separate one.
    from .services.instruments import find_duplicate, submission_hash as _sub_hash
    market_norm = market.strip() if market else None
    fields = dict(instrument_type=instrument_type, kg_co2e_per_kwh=kg_co2e_per_kwh,
                  coverage_kwh=coverage_kwh, gwp_set=gwp_set, market=market_norm,
                  start_date=start_date, end_date=end_date)
    submission_hash = _sub_hash(org.id, **fields)
    # Matched on the FIELDS, not on the stored hash. A row written before the hash
    # column existed carries NULL and a hash lookup cannot see it, so the check would
    # have protected only instruments created after the migration while every one
    # already in the database stayed freely duplicable. The hash's unique index still
    # closes the race a query cannot.
    clash = find_duplicate(db, MarketInstrument, org.id, **fields)
    if clash is not None:
        # Names the instrument it duplicates: "no" alone leaves the caller unable to tell
        # a replay apart from a genuine second certificate.
        _refuse(409,
                "an identical market instrument is already registered for this "
                "organisation — a second copy would declare the same coverage twice and "
                "understate market-based Scope 2. If this is a genuinely separate "
                "certificate, submit it with its own coverage window, or raise the "
                "existing instrument's coverage_kwh to the combined volume.",
                conflicting_instrument_id=clash.id,
                remedy="distinct coverage window, or raise coverage_kwh on "
                       f"instrument {clash.id}")
    inst = MarketInstrument(organisation_id=org.id, instrument_type=instrument_type,
                            kg_co2e_per_kwh=kg_co2e_per_kwh, coverage_kwh=coverage_kwh,
                            gwp_set=gwp_set, market=market_norm,
                            start_date=start_date,
                            end_date=end_date, description=description,
                            submission_hash=submission_hash)
    db.add(inst)
    try:
        db.commit()
    except IntegrityError:
        # Two identical submissions racing each other both read "no existing row" above
        # and both reach here; the unique index is what actually decides it.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="an identical market instrument is already registered for this "
                   "organisation — a second copy would declare the same coverage twice "
                   "and understate market-based Scope 2.")
    db.refresh(inst)
    return {"id": inst.id, "organisation_id": org.id, "instrument_type": inst.instrument_type,
            "kg_co2e_per_kwh": inst.kg_co2e_per_kwh, "coverage_kwh": inst.coverage_kwh,
            "gwp_set": inst.gwp_set}


@app.post("/calculate/run")
def run_calculation(gwp_set: str = Query("AR6"),
                    reporting_period_id: Optional[int] = None,
                    include_financed: Optional[bool] = None,
                    financed_as_of: Optional[str] = None,
                    financed_include_scope3: bool = True,
                    org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
    # Normalise + validate the GWP set: an unknown value (e.g. "ar6" typo) would
    # otherwise silently bucket every activity as gwp_mismatch (total = 0, status
    # complete) or crash a per-gas run with an opaque 500.
    gwp_set = (gwp_set or "").strip().upper()
    if gwp_set not in SUPPORTED_GWP_SETS:
        raise HTTPException(status_code=400,
                            detail=f"gwp_set must be one of {list(SUPPORTED_GWP_SETS)}")
    if financed_as_of is not None and _parse_iso_date(financed_as_of) is None:
        raise HTTPException(status_code=400, detail="financed_as_of must be an ISO date")
    try:
        run = compute_co2e(db, org.id, gwp_set=gwp_set, reporting_period_id=reporting_period_id,
                           include_financed=include_financed, financed_as_of=financed_as_of,
                           financed_include_scope3=financed_include_scope3)
    except ReportingPeriodError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(summary(db, organisation_id=org.id, run_id=run.id))


@app.get("/results/summary")
def get_summary(run_id: Optional[int] = None,
                org: Organisation = Depends(current_org),
                db: Session = Depends(get_db)):
    return JSONResponse(summary(db, organisation_id=org.id, run_id=run_id))


@app.get("/runs")
def list_runs(request: Request, limit: Optional[int] = None, offset: Optional[int] = None,
              org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """This organisation's runs, newest first.

    SHAPE CHANGE: was a bare JSON array capped at 50 with nothing saying so — an org
    with 60 runs saw 50 and had no way to learn the other 10 existed. Now an object
    carrying `runs` and the standard `page` block.
    """
    from .services.pagination import next_link_headers, page_block, page_of_query
    limit, offset = _page_args(limit, offset)
    q = db.query(CalculationRun).filter(CalculationRun.organisation_id == org.id)\
        .order_by(CalculationRun.id.desc())
    page = page_of_query(q, limit, offset)
    body = {"runs": [{"id": r.id, "created_at": r.created_at, "gwp_set": r.gwp_set,
                      "status": r.status, "total_co2e": r.total_co2e,
                      "total_co2e_market": r.total_co2e_market,
                      "mapped": r.mapped, "total_activities": r.total_activities}
                     for r in page["rows"]],
            "page": page_block(page, limit, offset)}
    return JSONResponse(body, headers=next_link_headers(request, page))


@app.get("/runs/{run_id}/lineage")
def get_run_lineage(run_id: int, request: Request, limit: Optional[int] = None,
                    offset: Optional[int] = None,
                    org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
    """Full lineage for one immutable run: every line item with its FROZEN
    calculation detail (factor id/version, unit conversion, per-gas GWPs,
    market allocation, spend normalization, DQ) joined to its source activity.
    The assurer drill-down: any figure -> source record -> pinned factor.

    PAGINATED (default 100 line items). This response was unbounded: a large run
    returned 65.5 MB in one body and peaked 746 MB RSS serialising it. `page.has_more`
    and the `Link: rel="next"` header walk the rest; `run` and `exclusions` repeat on
    every page so a page is self-describing.
    """
    import json as _json
    from .models import CalculationRun, EmissionLineItem
    run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                          CalculationRun.organisation_id == org.id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found for this organisation")
    from .services.pagination import next_link_headers, page_block, page_of_query
    limit, offset = _page_args(limit, offset)
    q = db.query(EmissionLineItem, ActivityRecord)\
        .join(ActivityRecord, ActivityRecord.id == EmissionLineItem.activity_id)\
        .filter(EmissionLineItem.run_id == run.id)\
        .order_by(EmissionLineItem.id)
    page = page_of_query(q, limit, offset)
    rows = page["rows"]
    body = {
        "run": {"id": run.id, "created_at": run.created_at, "gwp_set": run.gwp_set,
                "status": run.status, "total_co2e": run.total_co2e,
                "total_co2e_market": run.total_co2e_market,
                "total_biogenic_co2e": run.total_biogenic_co2e,
                "reporting_period_id": run.reporting_period_id},
        "exclusions": _json.loads(run.notes or "[]"),
        "page": page_block(page, limit, offset),
        "line_items": [{
            "id": li.id, "scope": li.scope, "method": li.method, "co2e": li.co2e,
            "detail": _json.loads(li.details or "{}"),
            "activity": {"id": a.id, "date": a.date, "category": a.category,
                         "subcategory": a.subcategory, "description": a.description,
                         "quantity": a.quantity, "unit": a.unit, "geo": a.geo,
                         "source_file": a.source_file},
        } for li, a in rows],
    }
    return JSONResponse(body, headers=next_link_headers(request, page))


@app.get("/runs/{run_id}/uncertainty")
def get_run_uncertainty(run_id: int,
                        method: str = "location",
                        correlation: str = DEFAULT_CORRELATION,
                        iterations: int = DEFAULT_ITERATIONS,
                        confidence: float = 0.95,
                        top_n: int = 10,
                        include_crosswalk: bool = False,
                        org: Organisation = Depends(current_org),
                        db: Session = Depends(get_db)):
    """Monte Carlo propagation of the run's frozen per-line pedigree sigmas.

    The interval an ESRS E1 / ISO 14064-1 / CDP uncertainty disclosure asks for,
    derived from distributions the calculation already froze onto every line. Reads
    only the frozen run, so re-running it on a filed run returns bit-identical
    numbers years later; ``reproducibility.input_fingerprint`` proves which inputs
    produced them.

    ``correlation`` defaults to ``by_factor`` — lines sharing an emission factor
    share that factor's error. The response also carries the ``independent`` and
    ``perfect`` bounds so the reader sees how much the answer rests on that choice.
    """
    run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                          CalculationRun.organisation_id == org.id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found for this organisation")
    result = propagate(db, run.id, method=method, correlation=correlation,
                       iterations=iterations, confidence=confidence, top_n=top_n,
                       include_crosswalk=include_crosswalk)
    # A refusal caused by a bad PARAMETER is a client error; a refusal caused by the
    # run's own data (no lines, no quantified uncertainty) is a legitimate 200 answer
    # that says so — the caller asked a valid question and this is the true reply.
    if not result.get("available") and "run_id" not in result:
        raise HTTPException(status_code=400, detail=result.get("reason", "invalid request"))
    return JSONResponse(result)


@app.get("/reports/summary.txt")
def get_plain_report(run_id: Optional[int] = None,
                     org: Organisation = Depends(current_org),
                     db: Session = Depends(get_db)):
    s = summary(db, organisation_id=org.id, run_id=run_id)
    lines = [f"Total (location-based): {s['total_co2e']:.2f} kgCO2e"]
    if s.get("run"):
        lines.append(f"Total (market-based):   {s.get('total_co2e_market', 0.0):.2f} kgCO2e")
        lines.append(f"(run #{s['run']['id']}, {s['run']['gwp_set']}, {s['run']['created_at']})")
    lines.append("\nBy scope:")
    for row in s["by_scope"]:
        lines.append(f"  Scope {row['scope']}: {row['co2e']:.2f} kgCO2e")
    # This renderer PRINTS the scope split, so it must print the caveat when part of that
    # split was guessed from an unrecognised category — otherwise the plain-text report is
    # the one place the assumption becomes invisible.
    _sa = s.get("scope_assumptions")
    if _sa:
        lines.append("  ASSUMED SCOPE 3 (unrecognised category, activity count): "
                     + ", ".join(f"{c}={n}" for c, n in sorted(
                         _sa["assumed_scope3_by_category"].items())))
        lines.append("  " + _sa["note"])
    lines.append("\nBy category:")
    for row in s["by_category"]:
        lines.append(f"  {row.get('category','?')}: {row.get('co2e',0.0):.2f} kgCO2e")
    cov = s.get("coverage")
    if cov:
        lines.append(f"\nCoverage: {cov['coverage_pct']}% ({cov['coverage_basis']}); "
                     f"{cov['activities_calculated']}/{cov['activities_total']} activities")
        if cov.get("warning"):
            lines.append("WARNING: " + cov["warning"])
    if s.get("partial"):
        lines.append(f"PARTIAL RUN — excluded: {s.get('partial_reasons')}")
    if s.get("notes"):
        lines.append("\nNotes: " + s["notes"])
    return PlainTextResponse("\n".join(lines))


def require_admin(x_admin_key: Optional[str] = Header(None)) -> None:
    """Reference data (FX/CPI) is GLOBAL — every tenant's spend calculations
    depend on it, so writes need the platform admin credential, not any org's
    API key. Disabled entirely when no admin key is configured.

    A MISSING admin key is 401, for the same reason a missing API key is: declared
    `Header(...)` it was request validation's refusal, so omitting the header produced a
    422 with an array body while sending a wrong one produced a 401 with a string.
    """
    import hmac
    import os
    admin_key = os.environ.get("ADMIN_API_KEY")
    if not admin_key:
        raise HTTPException(status_code=503,
                            detail="reference-data administration disabled "
                                   "(ADMIN_API_KEY not configured)")
    if not x_admin_key:
        raise HTTPException(status_code=401,
                            detail="missing admin key — send it in the X-Admin-Key header")
    if not hmac.compare_digest(x_admin_key, admin_key):
        raise HTTPException(status_code=401, detail="invalid admin key")


@app.post("/reference/fx_rates")
def add_fx_rate(base_currency: str = Query(...), quote_currency: str = Query(...),
                year: int = Query(...), rate: float = Query(...),
                _: None = Depends(require_admin), db: Session = Depends(get_db)):
    """Append-only: corrections insert a NEW row (latest wins); history preserved."""
    from .services.calc import _utcnow_iso
    if not math.isfinite(rate) or rate <= 0:
        raise HTTPException(status_code=400, detail="rate must be a finite number > 0")
    row = FxRate(base_currency=base_currency.upper(), quote_currency=quote_currency.upper(),
                 year=year, rate=rate, recorded_at=_utcnow_iso())
    db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "base_currency": row.base_currency,
            "quote_currency": row.quote_currency, "year": row.year, "rate": row.rate}


@app.post("/reference/residual_mix_rates")
def add_residual_mix_rate(market: str = Query(...), year: int = Query(...),
                          status: str = Query("published"),
                          kg_co2e_per_kwh: Optional[float] = Query(None),
                          gas_basis: str = Query("co2e"),
                          publisher: str = Query(...),
                          gwp_set: Optional[str] = Query(None),
                          publication: Optional[str] = Query(None),
                          source_url: Optional[str] = Query(None),
                          published_at: Optional[str] = Query(None),
                          _: None = Depends(require_admin), db: Session = Depends(get_db)):
    """A PUBLISHED residual-mix rate for one market and year (append-only).

    Admin-guarded like every other reference table: this is GLOBAL data and every
    tenant's market-based Scope 2 figure depends on it, so it needs the platform
    credential rather than an org key. Corrections INSERT a new row — never edit in
    place, which the run gate detects and blocks (RM-B5).

    `status='not_published'` records an ATTESTED absence for a market where no residual
    mix exists — a fact an assurer needs on file, not an empty query result.
    """
    from .services.calc import _utcnow_iso, _parse_iso_date
    from .services.residual_mix import market_key
    from .models import ResidualMixRate
    mkey = market_key(market)
    if not mkey:
        raise HTTPException(status_code=400, detail="market must be a non-blank key")
    if status not in ("published", "not_published"):
        raise HTTPException(status_code=400,
                            detail="status must be published | not_published")
    if not (1990 <= year <= 2100):
        raise HTTPException(status_code=400, detail="year must be between 1990 and 2100")
    if gas_basis not in ("co2", "co2e"):
        raise HTTPException(status_code=400, detail="gas_basis must be co2 | co2e")
    if not (publisher or "").strip():
        raise HTTPException(status_code=400,
                            detail="publisher is required — a rate without a named "
                                   "publisher is an assertion, not reference data; "
                                   "record it as a MarketInstrument instead")
    if status == "published":
        if kg_co2e_per_kwh is None or not math.isfinite(kg_co2e_per_kwh) \
                or kg_co2e_per_kwh <= 0:
            raise HTTPException(status_code=400,
                                detail="a published residual mix needs a finite "
                                       "kg_co2e_per_kwh > 0")
    else:
        if kg_co2e_per_kwh is not None:
            raise HTTPException(status_code=400,
                                detail="status=not_published cannot carry a rate")
        if len((publication or "").strip()) < 20:
            raise HTTPException(status_code=400,
                                detail="an asserted absence must carry its attestation "
                                       "(min 20 chars in `publication`)")
    if published_at and _parse_iso_date(published_at) is None:
        raise HTTPException(status_code=400, detail="published_at must be an ISO date")
    row = ResidualMixRate(
        market=mkey, year=year, status=status, kg_co2e_per_kwh=kg_co2e_per_kwh,
        gas_basis=gas_basis, publisher=publisher.strip(),
        gwp_set=(gwp_set.strip().upper() if gwp_set else None),
        publication=publication, source_url=source_url, published_at=published_at,
        recorded_at=_utcnow_iso())
    db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "market": row.market, "year": row.year, "status": row.status,
            "kg_co2e_per_kwh": row.kg_co2e_per_kwh, "publisher": row.publisher}


@app.get("/reference/residual_mix_rates")
def list_residual_mix_rates(market: Optional[str] = Query(None),
                            year: Optional[int] = Query(None),
                            org: Organisation = Depends(current_org),
                            db: Session = Depends(get_db)):
    """Read the residual-mix series. Ships WITH the writer on purpose: without it the
    gate's "no residual mix on file for DE 2025" is a dead end a preparer cannot verify
    or act on."""
    from .services.residual_mix import market_key
    from .models import ResidualMixRate
    q = db.query(ResidualMixRate)
    if market:
        q = q.filter(ResidualMixRate.market == market_key(market))
    if year:
        q = q.filter(ResidualMixRate.year == year)
    rows = q.order_by(ResidualMixRate.market, ResidualMixRate.year,
                      ResidualMixRate.id.desc()).all()
    return {"count": len(rows), "rates": [{
        "id": r.id, "market": r.market, "year": r.year, "status": r.status,
        "kg_co2e_per_kwh": r.kg_co2e_per_kwh, "gas_basis": r.gas_basis,
        "gwp_set": r.gwp_set, "publisher": r.publisher, "publication": r.publication,
        "source_url": r.source_url, "published_at": r.published_at,
        "recorded_at": r.recorded_at,
    } for r in rows], "note": "Append-only: the newest row for a (market, year) wins; "
                              "corrections are INSERTs, never edits."}


@app.post("/reference/price_indices")
def add_price_index(currency: str = Query(...), year: int = Query(...),
                    index_value: float = Query(...),
                    _: None = Depends(require_admin), db: Session = Depends(get_db)):
    """Append-only: corrections insert a NEW row (latest wins); history preserved."""
    from .services.calc import _utcnow_iso
    if not math.isfinite(index_value) or index_value <= 0:
        raise HTTPException(status_code=400, detail="index_value must be a finite number > 0")
    row = PriceIndex(currency=currency.upper(), year=year, index_value=index_value,
                     recorded_at=_utcnow_iso())
    db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "currency": row.currency, "year": row.year,
            "index_value": row.index_value}


@app.post("/cbam/goods")
def add_cbam_good(request: Request, cn_code: str = Query(...),
                  quantity_tonnes: float = Query(...),
                  origin_country: str = Query(...), import_date: str = Query(...),
                  actual_direct_t_per_t: Optional[float] = None,
                  actual_indirect_t_per_t: Optional[float] = None,
                  actual_verified: bool = False,
                  carbon_price_paid_eur_per_t: Optional[float] = None,
                  body: Optional[CbamGoodBody] = None,
                  org: Organisation = Depends(current_org),
                  db: Session = Depends(get_db)):
    from .models import CbamGood
    _reject_narrative_query_params(request, "description", "installation")
    description = body.description if body else None
    installation = body.installation if body else None
    if not math.isfinite(quantity_tonnes) or quantity_tonnes <= 0:
        raise HTTPException(status_code=400, detail="quantity_tonnes must be a finite number > 0")
    for name, v in (("actual_direct_t_per_t", actual_direct_t_per_t),
                    ("actual_indirect_t_per_t", actual_indirect_t_per_t),
                    ("carbon_price_paid_eur_per_t", carbon_price_paid_eur_per_t)):
        if v is not None and (not math.isfinite(v) or v < 0):
            raise HTTPException(status_code=400, detail=f"{name} must be a finite number >= 0")
    if _parse_iso_date(import_date) is None:
        raise HTTPException(status_code=400, detail="import_date must be ISO format YYYY-MM-DD")
    good = CbamGood(organisation_id=org.id, cn_code=cn_code.strip(),
                    description=description, quantity_tonnes=quantity_tonnes,
                    origin_country=origin_country.upper(), import_date=import_date,
                    installation=installation,
                    actual_direct_t_per_t=actual_direct_t_per_t,
                    actual_indirect_t_per_t=actual_indirect_t_per_t,
                    actual_verified=actual_verified,
                    carbon_price_paid_eur_per_t=carbon_price_paid_eur_per_t)
    db.add(good); db.commit(); db.refresh(good)
    return {"id": good.id, "cn_code": good.cn_code, "quantity_tonnes": good.quantity_tonnes}


@app.get("/reports/cbam")
def get_cbam_declaration(year: int = Query(...),
                         ets_price_eur_per_t: Optional[float] = None,
                         org: Organisation = Depends(current_org),
                         db: Session = Depends(get_db)):
    """CBAM annual declaration payload with fail-closed gates."""
    return JSONResponse(with_guidance(cbam_declaration(db, org.id, year,
                                         ets_price_eur_per_t=ets_price_eur_per_t)))


# A typo'd good_category puts a line outside INDIRECT_IN_OBLIGATION, silently dropping
# indirect emissions from the obligation for cement/fertilisers/electricity — an
# understatement, so the category is closed-vocabulary at the boundary.
CBAM_GOOD_CATEGORIES = {"iron_steel", "aluminium", "cement", "fertilisers",
                        "hydrogen", "electricity"}


def _check_cbam_category(good_category: str) -> None:
    if good_category not in CBAM_GOOD_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"good_category must be one of "
                   f"{', '.join(sorted(CBAM_GOOD_CATEGORIES))}")


def _check_origin_country(origin_country: Optional[str]) -> None:
    code = (origin_country or "").strip()
    if code and (len(code) != 2 or not code.isalpha()):
        raise HTTPException(status_code=400,
                            detail="origin_country must be an ISO 3166-1 alpha-2 code "
                                   "(e.g. CN), or omitted for a country-agnostic row")


@app.post("/reference/cbam_defaults")
def add_cbam_default(cn_code_prefix: str = Query(...), good_category: str = Query(...),
                     direct_t_co2e_per_t: float = Query(...),
                     indirect_t_co2e_per_t: float = Query(...),
                     valid_year: int = Query(...),
                     origin_country: Optional[str] = Query(
                         None, description="ISO country code; omit for a country-agnostic "
                                           "fallback row"),
                     _: None = Depends(require_admin), db: Session = Depends(get_db)):
    """Append-only, admin-gated (global reference data, same doctrine as FX/CPI)."""
    from .models import CbamDefaultValue
    from .services.calc import _utcnow_iso
    for name, v in (("direct_t_co2e_per_t", direct_t_co2e_per_t),
                    ("indirect_t_co2e_per_t", indirect_t_co2e_per_t)):
        if not math.isfinite(v) or v < 0:
            raise HTTPException(status_code=400, detail=f"{name} must be a finite number >= 0")
    # An empty/short prefix would match (hijack) every CN code.
    if not cn_code_prefix.strip().isdigit() or len(cn_code_prefix.strip()) < 2:
        raise HTTPException(status_code=400,
                            detail="cn_code_prefix must be numeric, at least 2 digits")
    _check_cbam_category(good_category)
    _check_origin_country(origin_country)
    row = CbamDefaultValue(cn_code_prefix=cn_code_prefix.strip(),
                           origin_country=(origin_country or "").strip().upper() or None,
                           good_category=good_category, valid_year=valid_year,
                           direct_t_co2e_per_t=direct_t_co2e_per_t,
                           indirect_t_co2e_per_t=indirect_t_co2e_per_t,
                           recorded_at=_utcnow_iso())
    db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "cn_code_prefix": row.cn_code_prefix,
            "origin_country": row.origin_country}


@app.post("/reference/cbam_benchmarks")
def add_cbam_benchmark(cn_code_prefix: str = Query(...), good_category: str = Query(...),
                       benchmark_t_co2e_per_t: float = Query(...),
                       valid_year: int = Query(...),
                       basis: Optional[str] = Query(
                           None, description="e.g. process-related, or including precursors"),
                       _: None = Depends(require_admin), db: Session = Depends(get_db)):
    """EU production benchmarks — the basis of the CBAM free-allocation adjustment.

    Append-only, admin-gated. A wrong benchmark moves the certificate count directly, so
    this is reference data with the same doctrine as FX/CPI and the default values.
    """
    from .models import CbamBenchmark
    from .services.calc import _utcnow_iso
    if not math.isfinite(benchmark_t_co2e_per_t) or benchmark_t_co2e_per_t < 0:
        raise HTTPException(status_code=400,
                            detail="benchmark_t_co2e_per_t must be a finite number >= 0")
    if not cn_code_prefix.strip().isdigit() or len(cn_code_prefix.strip()) < 2:
        raise HTTPException(status_code=400,
                            detail="cn_code_prefix must be numeric, at least 2 digits")
    _check_cbam_category(good_category)
    row = CbamBenchmark(cn_code_prefix=cn_code_prefix.strip(), good_category=good_category,
                        benchmark_t_co2e_per_t=benchmark_t_co2e_per_t, basis=basis,
                        valid_year=valid_year, recorded_at=_utcnow_iso())
    db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "cn_code_prefix": row.cn_code_prefix,
            "benchmark_t_co2e_per_t": row.benchmark_t_co2e_per_t}


@app.post("/lca/assessments")
def create_lca_assessment(name: str = Query(...), standard: str = Query(...),
                          functional_unit: str = Query(...),
                          functional_unit_quantity: float = 1.0, gwp_set: str = "AR6",
                          org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import LcaAssessment
    from .services.lca import STANDARDS
    from .services.calc import _utcnow_iso
    if standard not in STANDARDS:
        raise HTTPException(status_code=400, detail=f"standard must be one of {sorted(STANDARDS)}")
    if not math.isfinite(functional_unit_quantity) or functional_unit_quantity <= 0:
        raise HTTPException(status_code=400, detail="functional_unit_quantity must be finite > 0")
    a = LcaAssessment(organisation_id=org.id, name=name, standard=standard,
                      functional_unit=functional_unit,
                      functional_unit_quantity=functional_unit_quantity,
                      gwp_set=gwp_set, created_at=_utcnow_iso())
    db.add(a); db.commit(); db.refresh(a)
    return {"id": a.id, "name": a.name, "standard": a.standard}


def _own_assessment(db: Session, org: Organisation, assessment_id: int):
    from .models import LcaAssessment
    a = db.query(LcaAssessment).filter(LcaAssessment.id == assessment_id,
                                       LcaAssessment.organisation_id == org.id).first()
    if a is None:
        raise HTTPException(status_code=404, detail="assessment not found for this organisation")
    return a


@app.post("/lca/assessments/{assessment_id}/items")
def add_lca_item(assessment_id: int, request: Request, stage: str = Query(...),
                 quantity: float = Query(...), unit: str = Query(...),
                 factor_id: int = Query(...), allocation_factor: float = 1.0,
                 body: Optional[LcaItemBody] = None,
                 org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import LcaItem
    _reject_narrative_query_params(request, "description")
    description = body.description if body else None
    from .services.lca import valid_stage
    a = _own_assessment(db, org, assessment_id)
    if not valid_stage(a.standard, stage):
        raise HTTPException(status_code=400,
                            detail=f"invalid stage {stage!r} for {a.standard} "
                                   f"(EN standards require a module code like A1-A3, C3, B6)")
    if not (0.0 <= allocation_factor <= 1.0):
        raise HTTPException(status_code=400, detail="allocation_factor must be in [0, 1]")
    factor = _visible_factor(db, org, factor_id)
    it = LcaItem(assessment_id=a.id, stage=stage, description=description, quantity=quantity,
                 unit=unit, factor_id=factor_id, allocation_factor=allocation_factor)
    db.add(it); db.commit(); db.refresh(it)
    return {"id": it.id, "stage": it.stage, "factor_id": factor_id}


@app.get("/reports/lca/{assessment_id}")
def get_lca_report(assessment_id: int, org: Organisation = Depends(current_org),
                   db: Session = Depends(get_db)):
    from .services.lca import compute_assessment
    a = _own_assessment(db, org, assessment_id)
    payload = compute_assessment(db, a)
    payload["framework"] = payload["framework"]  # keep as-is; guidance maps on prefix
    return JSONResponse(with_guidance(payload))


@app.get("/reports/rics/{assessment_id}")
def get_rics_report(assessment_id: int, gia_unit: Optional[str] = Query(None),
                    org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
    """RICS Whole Life Carbon groupings (upfront / embodied / operational / whole-life,
    Module D separate) over an en_15978 building assessment, absolute and per unit area.

    Honest scope: GWP-fossil only (biogenic separate, not RICS sequestration accounting),
    and the carbon arithmetic in RICS groupings — not a full RICS-compliant WLCA. See the
    payload's not_covered.
    """
    from .reports.rics import rics_report
    return JSONResponse(with_guidance(
        rics_report(db, org.id, assessment_id, gia_unit=gia_unit)))


@app.get("/reports/pef/{assessment_id}")
def get_pef_report(assessment_id: int,
                   org: Organisation = Depends(current_org),
                   db: Session = Depends(get_db)):
    """A PEF-shaped single-category coverage map over one LCA assessment.

    Honest scope: the platform computes 1 of PEF's 16 EF 3.1 impact categories (Climate
    change), and only its GWP-fossil sub-indicator — NOT a PEF profile (no normalisation,
    weighting, single score, PEFCR compliance or verification). See the payload's
    pef_profile_status / not_produced.
    """
    from .reports.pef import pef_report
    return JSONResponse(with_guidance(pef_report(db, org.id, assessment_id)))


@app.get("/reports/epd/{assessment_id}")
def get_epd_report(assessment_id: int, pcr_reference: Optional[str] = Query(None),
                   programme_operator: Optional[str] = Query(None),
                   org: Organisation = Depends(current_org),
                   db: Session = Depends(get_db)):
    """An ISO 14025 / EN 15804 EPD-shaped GWP declaration over one LCA assessment.

    Honest scope: the quantitative GWP core a verifier would check, not a verified EPD,
    and the GWP indicator only (the further EN 15804+A2 impact categories are not
    produced). See the payload's verification_status / not_covered.
    """
    from .reports.epd import epd_report
    return JSONResponse(with_guidance(
        epd_report(db, org.id, assessment_id, pcr_reference=pcr_reference,
                   programme_operator=programme_operator)))


@app.post("/finance/positions")
def add_financed_position(investee_name: str = Query(...), asset_class: str = Query(...),
                          currency: str = Query(...), outstanding_amount: float = Query(...),
                          attribution_denominator: float = Query(...),
                          investee_scope1_tco2e: float = 0.0, investee_scope2_tco2e: float = 0.0,
                          investee_scope3_tco2e: Optional[float] = None,
                          investee_revenue_millions: Optional[float] = None,
                          data_quality_score: int = 5, as_of_date: Optional[str] = None,
                          org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import FinancedPosition
    from .services.pcaf import ASSET_CLASSES
    from .services.calc import _utcnow_iso
    if asset_class not in ASSET_CLASSES:
        raise HTTPException(status_code=400, detail=f"asset_class must be one of {sorted(ASSET_CLASSES)}")
    if not (1 <= data_quality_score <= 5):
        raise HTTPException(status_code=400, detail="data_quality_score must be 1..5 (PCAF)")
    for name, v in (("outstanding_amount", outstanding_amount),
                    ("attribution_denominator", attribution_denominator),
                    ("investee_scope1_tco2e", investee_scope1_tco2e),
                    ("investee_scope2_tco2e", investee_scope2_tco2e)):
        if not math.isfinite(v) or v < 0:
            raise HTTPException(status_code=400, detail=f"{name} must be a finite number >= 0")
    if attribution_denominator <= 0:
        raise HTTPException(status_code=400, detail="attribution_denominator must be > 0")
    p = FinancedPosition(organisation_id=org.id, investee_name=investee_name,
                         asset_class=asset_class, currency=currency.upper(),
                         outstanding_amount=outstanding_amount,
                         attribution_denominator=attribution_denominator,
                         investee_scope1_tco2e=investee_scope1_tco2e,
                         investee_scope2_tco2e=investee_scope2_tco2e,
                         investee_scope3_tco2e=investee_scope3_tco2e,
                         investee_revenue_millions=investee_revenue_millions,
                         data_quality_score=data_quality_score, as_of_date=as_of_date,
                         created_at=_utcnow_iso())
    db.add(p); db.commit(); db.refresh(p)
    return {"id": p.id, "investee_name": p.investee_name, "asset_class": p.asset_class}


@app.get("/reports/pcaf")
def get_pcaf_report(include_scope3: bool = True, as_of: Optional[str] = None,
                    org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .services.pcaf import portfolio_financed
    return JSONResponse(with_guidance(portfolio_financed(db, org.id, include_scope3=include_scope3,
                                                         as_of=as_of)))


# --- GHG Protocol Land Sector & Removals: inventory removals ------------------

@app.post("/removals")
def create_removal(removal_category: str = Query(...), method: str = Query(...),
                   scope: str = Query(...), quantity_tco2e: float = Query(...),
                   quantification_method: str = Query(...),
                   reporting_period_id: Optional[int] = None,
                   record_kind: str = "removal", reverses_record_id: Optional[int] = None,
                   entity_id: Optional[int] = None, storage_medium: Optional[str] = None,
                   expected_durability_years: Optional[int] = None,
                   monitoring_method: Optional[str] = None,
                   monitoring_period_years: Optional[int] = None,
                   reversal_accounting: Optional[str] = None,
                   attribute_retained: bool = True,
                   credit_registry: Optional[str] = None, credit_serial_if_sold: Optional[str] = None,
                   uncertainty_pct: Optional[float] = None, buffer_pct: Optional[float] = None,
                   vintage_year: Optional[int] = None, as_of_date: Optional[str] = None,
                   org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """Record an inventory carbon removal (GHG Protocol Land Sector & Removals).

    The org's OWN within-boundary sequestration — NOT a purchased offset credit. It is
    reported separately from gross emissions and never netted into the total. Permanence
    metadata is disclosed as recorded; a land-based removal without monitoring and
    reversal accounting blocks the disclosure (it is not reportable).
    """
    from .models import RemovalRecord
    from .services.calc import _utcnow_iso
    if removal_category not in ("technological", "land_based"):
        raise HTTPException(status_code=400, detail="removal_category must be technological|land_based")
    if record_kind not in ("removal", "reversal"):
        raise HTTPException(status_code=400, detail="record_kind must be removal|reversal")
    if scope not in ("1", "3"):
        raise HTTPException(status_code=400, detail="scope must be '1' (own ops) or '3' (value chain)")
    if quantification_method not in ("stock_difference", "gain_loss", "metered"):
        raise HTTPException(status_code=400,
                            detail="quantification_method must be stock_difference|gain_loss|metered")
    if not math.isfinite(quantity_tco2e) or quantity_tco2e <= 0:
        raise HTTPException(status_code=400, detail="quantity_tco2e must be finite > 0")
    if as_of_date is not None and _parse_iso_date(as_of_date) is None:
        raise HTTPException(status_code=400, detail="as_of_date must be an ISO date")
    r = RemovalRecord(organisation_id=org.id, entity_id=entity_id,
                      reporting_period_id=reporting_period_id, record_kind=record_kind,
                      reverses_record_id=reverses_record_id, removal_category=removal_category,
                      method=method, scope=scope, quantity_tco2e=quantity_tco2e,
                      quantification_method=quantification_method, storage_medium=storage_medium,
                      expected_durability_years=expected_durability_years,
                      monitoring_method=monitoring_method,
                      monitoring_period_years=monitoring_period_years,
                      reversal_accounting=reversal_accounting, attribute_retained=attribute_retained,
                      credit_registry=credit_registry, credit_serial_if_sold=credit_serial_if_sold,
                      uncertainty_pct=uncertainty_pct, buffer_pct=buffer_pct,
                      vintage_year=vintage_year, as_of_date=as_of_date, created_at=_utcnow_iso())
    db.add(r); db.commit(); db.refresh(r)
    return {"id": r.id, "removal_category": r.removal_category, "record_kind": r.record_kind,
            "quantity_tco2e": r.quantity_tco2e,
            "note": "Recompute the run to freeze it into the inventory."}


@app.get("/reports/removals")
def get_removals_report(run_id: Optional[int] = None,
                        org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """The inventory-removals statement for a run (gross / removals / net + gate)."""
    from .reports.summary import _resolve_run
    from .services.removals import removals_completeness
    run = _resolve_run(db, org.id, run_id)
    if run is None:
        return JSONResponse({"framework": "GHG Protocol Land Sector & Removals",
                             "disclosure_ready": False, "blockers": ["no calculation run exists"]})
    g = removals_completeness(db, run)
    ready = bool(g.get("assessable")) and not g.get("blockers")
    return JSONResponse(with_guidance({
        "framework": "GHG Protocol Land Sector & Removals",
        "disclosure_ready": ready, "blockers": g.get("blockers", []),
        "warnings": g.get("warnings", []),
        "gross_emissions_kg": run.total_co2e,
        "inventory_removals_kg": run.total_removals_co2e,
        "reversed_kg": run.removals_reversed_co2e,
        "net_removals_kg": g.get("net_removals_kg"),
        "net_emissions_after_removals_kg": (
            (run.total_co2e or 0.0) - g.get("net_removals_kg", 0.0)
            if run.total_removals_co2e is not None else None),
        "note": "Removals are reported SEPARATELY from gross emissions (never netted); "
                "net is derived. Distinct from purchased offset credits.",
    }))


# --- GHG Protocol Ch.3: the organisational boundary ---------------------------

@app.post("/organisations/consolidation")
def set_consolidation_approach(request: Request,
                               body: Optional[ConsolidationBody] = None,
                               approach: str = Query(...),
                               org: Organisation = Depends(current_org),
                               db: Session = Depends(get_db)):
    """Set the org's GHGP Ch.3 consolidation approach and the reason for the choice.

    The approach decides what share of each entity's emissions is consolidated, so it
    is a determinant of every reported figure — recompute after changing it.
    """
    from .services.boundary import APPROACHES
    from .services.ghgp import is_boilerplate, MIN_JUSTIFICATION_CHARS
    _reject_narrative_query_params(request, "reason")
    reason = _require_body(body, "reason").reason
    if approach not in APPROACHES:
        raise HTTPException(status_code=400, detail=f"approach must be one of {list(APPROACHES)}")
    if is_boilerplate(reason):
        raise HTTPException(
            status_code=400,
            detail=f"a reason for the choice is required (>= {MIN_JUSTIFICATION_CHARS} chars, "
                   f"not boilerplate) — GHG Protocol Ch.3 asks a company to state and justify "
                   f"its chosen consolidation approach")
    org.consolidation_approach = approach
    org.consolidation_approach_reason = reason
    db.commit()
    return {"consolidation_approach": approach,
            "note": "Recompute the run — the approach changes the reported figures, and a "
                    "change of boundary triggers a GHG Protocol Ch.5 base-year recalculation "
                    "assessment."}


@app.post("/entities")
def create_entity(request: Request, name: str = Query(...),
                  accounting_category: str = Query(...),
                  equity_share_pct: Optional[float] = None,
                  financial_control: Optional[bool] = None,
                  joint_financial_control: Optional[bool] = None,
                  operational_control: Optional[bool] = None,
                  in_consolidated_accounting_group: Optional[bool] = None,
                  entity_ref: Optional[str] = None,
                  effective_from: Optional[str] = None, effective_to: Optional[str] = None,
                  body: Optional[EntityBody] = None,
                  org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """Declare an operation inside the organisational boundary (GHGP Ch.3).

    The control facts are ASSERTED judgements, independent of ownership %: the same 20%
    associate is consolidated at 100% or 0% purely on whether operational control is
    asserted. NULL means "not asserted" (which blocks disclosure), never "no".
    """
    from .models import ReportingEntity
    from .services.boundary import ACCOUNTING_CATEGORIES
    from .services.calc import _utcnow_iso
    _reject_narrative_query_params(request, "equity_share_basis", "control_rationale")
    equity_share_basis = body.equity_share_basis if body else None
    control_rationale = body.control_rationale if body else None
    if accounting_category not in ACCOUNTING_CATEGORIES:
        raise HTTPException(status_code=400,
                            detail=f"accounting_category must be one of {list(ACCOUNTING_CATEGORIES)}")
    if equity_share_pct is not None and (
            not math.isfinite(equity_share_pct) or not 0 <= equity_share_pct <= 100):
        raise HTTPException(status_code=400, detail="equity_share_pct must be finite in [0, 100]")
    if financial_control and joint_financial_control:
        raise HTTPException(status_code=400,
                            detail="an entity cannot have both sole and joint financial control")
    for nm, v in (("effective_from", effective_from), ("effective_to", effective_to)):
        if v is not None and _parse_iso_date(v) is None:
            raise HTTPException(status_code=400, detail=f"{nm} must be an ISO date")
    if db.query(ReportingEntity).filter(ReportingEntity.organisation_id == org.id,
                                        ReportingEntity.name == name).first():
        raise HTTPException(status_code=409, detail=f"entity {name!r} already exists")
    e = ReportingEntity(organisation_id=org.id, name=name, entity_ref=entity_ref,
                        accounting_category=accounting_category,
                        equity_share_pct=equity_share_pct, equity_share_basis=equity_share_basis,
                        financial_control=financial_control,
                        joint_financial_control=joint_financial_control,
                        operational_control=operational_control,
                        control_rationale=control_rationale,
                        in_consolidated_accounting_group=in_consolidated_accounting_group,
                        effective_from=effective_from, effective_to=effective_to,
                        created_at=_utcnow_iso())
    db.add(e); db.commit(); db.refresh(e)
    return {"id": e.id, "name": e.name, "accounting_category": e.accounting_category,
            "note": "Attribute activities to it via POST /activities/entity, then recompute."}


def _coverage_cell(row, key):
    """An optional ISO date from an upload row; None when absent or unparseable.

    Never guesses: a malformed window is dropped to None so the record is attributed by
    `date` as before, rather than silently prorated on a date nobody can verify.
    """
    from .services.calc import _parse_iso_date
    try:
        v = row.get(key)
    except Exception:
        return None
    if v is None or (isinstance(v, float) and v != v):      # NaN
        return None
    v = str(v).strip()
    return v if (v and _parse_iso_date(v)) else None


@app.post("/activities/coverage_window")
def set_activity_coverage_window(
        coverage_start: str = Query(...), coverage_end: str = Query(...),
        category: Optional[str] = Query(None), source_file: Optional[str] = Query(None),
        org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """Declare the CONSUMPTION WINDOW a set of activities covers.

    A record carries a single `date`, so an invoice spanning 15 Dec - 15 Jan was
    attributed WHOLLY to whichever fiscal year that date fell in. With a window declared,
    a period-scoped run prorates the quantity by the overlapping share (inclusive calendar
    days, frozen onto the line) so the emissions land in the year they occurred.

    Recompute afterwards: the window is part of the activity fingerprint, so an existing
    run becomes STALE rather than silently changing.
    """
    from .services.calc import _parse_iso_date
    cs, ce = _parse_iso_date(coverage_start), _parse_iso_date(coverage_end)
    if cs is None or ce is None:
        raise HTTPException(status_code=400,
                            detail="coverage_start and coverage_end must be ISO dates")
    if ce < cs:
        raise HTTPException(status_code=400,
                            detail="coverage_end must not precede coverage_start")
    q = db.query(ActivityRecord).filter(ActivityRecord.organisation_id == org.id)
    if category:
        q = q.filter(ActivityRecord.category == category.strip().lower())
    if source_file:
        q = q.filter(ActivityRecord.source_file == source_file)
    rows = q.all()
    for a in rows:
        a.coverage_start, a.coverage_end = cs.isoformat(), ce.isoformat()
    db.commit()
    return {"updated": len(rows), "coverage_start": cs.isoformat(),
            "coverage_end": ce.isoformat(),
            "note": "Recompute the run: the window is part of the activity fingerprint, "
                    "so existing runs are now STALE rather than silently changed."}


@app.post("/activities/entity")
def attribute_activities_to_entity(entity_id: int = Query(...),
                                   category: Optional[str] = None,
                                   source_file: Optional[str] = None,
                                   org: Organisation = Depends(current_org),
                                   db: Session = Depends(get_db)):
    """Attribute the org's activities to an entity (bulk, by category/source file)."""
    from .models import ReportingEntity
    e = db.query(ReportingEntity).filter(ReportingEntity.id == entity_id,
                                         ReportingEntity.organisation_id == org.id).first()
    if e is None:
        raise HTTPException(status_code=404, detail="entity not found for this organisation")
    q = db.query(ActivityRecord).filter(ActivityRecord.organisation_id == org.id)
    if category is not None:
        q = q.filter(ActivityRecord.category == category)
    if source_file is not None:
        q = q.filter(ActivityRecord.source_file == source_file)
    rows = q.all()
    for a in rows:
        a.entity_id = e.id
    db.commit()
    return {"updated": len(rows), "entity_id": e.id, "note": "Recompute the run to apply."}


# --- GHG Protocol Scope 3: the 15-category screen ----------------------------

@app.post("/scope3/declarations")
def upsert_scope3_declaration(
        request: Request,
        reporting_period_id: int = Query(...), category: int = Query(..., ge=1, le=15),
        status: str = Query(...),
        screening_estimate_tco2e: Optional[float] = None,
        materiality_threshold_pct: Optional[float] = None,
        screening_method: Optional[str] = None,
        criteria_json: Optional[str] = None,
        calculation_tools: Optional[str] = None,
        minimum_boundary_met: Optional[bool] = None,
        gross_exposure_total: Optional[float] = None,
        gross_exposure_currency: Optional[str] = None,
        screened_at: Optional[str] = None,
        temporal_basis: Optional[str] = None,
        body: Optional[Scope3DeclarationBody] = None,
        basis_units_sold: Optional[float] = None,
        basis_lifetime_years: Optional[float] = None,
        basis_per_unit_annual_co2e_kg: Optional[float] = None,
        org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """Declare one Scope 3 category for a reporting period.

    The evidence a status requires is enforced HERE, at the boundary — an
    unjustified exclusion is rejected rather than surfacing later as a blocker.
    """
    import json as _json
    from .models import Scope3CategoryDeclaration
    from .services.ghgp import (
        STORABLE_STATUSES, SEVEN_CRITERIA, is_boilerplate, GHGP_STANDARD_VERSION,
        temporal_bases_for, TEMPORAL_BASES,
        MIN_JUSTIFICATION_CHARS,
    )
    from .services.calc import _utcnow_iso
    _reject_narrative_query_params(request, "justification", "method_description",
                                   "declared_by")
    justification = body.justification if body else None
    method_description = body.method_description if body else None
    declared_by = body.declared_by if body else None
    if status not in STORABLE_STATUSES:
        raise HTTPException(status_code=400,
                            detail=f"status must be one of {list(STORABLE_STATUSES)}")
    period = db.get(ReportingPeriod, reporting_period_id)
    if period is None or period.organisation_id != org.id:
        raise HTTPException(status_code=404, detail="reporting period not found for this organisation")

    if status in ("not_applicable", "not_material", "not_measured") and is_boilerplate(justification):
        raise HTTPException(
            status_code=400,
            detail=f"excluding a category requires a real justification (>= "
                   f"{MIN_JUSTIFICATION_CHARS} chars, not boilerplate). 'We have not "
                   f"measured it' is a disclosure of incompleteness, not a justification.")
    if status == "included" and is_boilerplate(method_description):
        raise HTTPException(status_code=400,
                            detail="an INCLUDED category requires a method_description (ESRS AR 46(h))")
    crit = None
    if status == "not_material":
        if screening_estimate_tco2e is None or materiality_threshold_pct is None:
            raise HTTPException(status_code=400,
                                detail="NOT MATERIAL requires screening_estimate_tco2e and "
                                       "materiality_threshold_pct — it must be screened, not asserted")
        try:
            crit = _json.loads(criteria_json or "{}")
        except ValueError:
            raise HTTPException(status_code=400, detail="criteria_json must be valid JSON")
        absent = [k for k in SEVEN_CRITERIA if k not in crit or crit.get(k) is None]
        if absent:
            raise HTTPException(status_code=400,
                                detail=f"NOT MATERIAL must be screened against all seven relevance "
                                       f"criteria; missing/null: {absent}")
    elif criteria_json:
        try:
            crit = _json.loads(criteria_json)
        except ValueError:
            raise HTTPException(status_code=400, detail="criteria_json must be valid JSON")

    now = _utcnow_iso()
    d = db.query(Scope3CategoryDeclaration).filter(
        Scope3CategoryDeclaration.organisation_id == org.id,
        Scope3CategoryDeclaration.reporting_period_id == reporting_period_id,
        Scope3CategoryDeclaration.category == category).first()
    if d is None:
        d = Scope3CategoryDeclaration(organisation_id=org.id,
                                      reporting_period_id=reporting_period_id,
                                      category=category, created_at=now)
        db.add(d)
    d.status = status
    d.justification = justification
    d.screening_estimate_tco2e = screening_estimate_tco2e
    d.materiality_threshold_pct = materiality_threshold_pct
    d.screening_method = screening_method
    d.criteria = _json.dumps(crit) if crit is not None else None
    d.method_description = method_description
    d.calculation_tools = calculation_tools
    d.minimum_boundary_met = minimum_boundary_met
    if gross_exposure_total is not None and (
            not math.isfinite(gross_exposure_total) or gross_exposure_total <= 0):
        raise HTTPException(status_code=400,
                            detail="gross_exposure_total must be a finite number > 0")
    # --- Temporal basis (Cats 2/11/12) ---------------------------------------------
    # Validated at the boundary against the CATEGORY'S OWN vocabulary: a Cat 11 lifetime
    # token is not offerable on Cat 2, where a conforming figure has no lifetime at all.
    _vocab = temporal_bases_for(category)
    if temporal_basis is not None:
        if not _vocab:
            raise HTTPException(
                status_code=400,
                detail=f"category {category} has no temporal_basis vocabulary — the field "
                       f"applies only to categories {sorted(TEMPORAL_BASES)}")
        if temporal_basis not in _vocab:
            raise HTTPException(
                status_code=400,
                detail=f"unknown temporal_basis '{temporal_basis}' for category {category}; "
                       f"expected one of {sorted(_vocab)}")
    _entails = bool(temporal_basis and _vocab.get(temporal_basis, (0, 0, 0))[1])
    _nums = {"basis_units_sold": basis_units_sold,
             "basis_lifetime_years": basis_lifetime_years,
             "basis_per_unit_annual_co2e_kg": basis_per_unit_annual_co2e_kg}
    for _k, _v in _nums.items():
        if _v is None:
            continue
        if not math.isfinite(_v) or _v <= 0:
            raise HTTPException(status_code=400,
                                detail=f"{_k} must be a finite number > 0")
        if not _entails:
            # Mirrors ck_s3decl_basis_entailment: these numbers mean nothing except as
            # the arithmetic claim `sold_units_full_lifetime` makes, so accepting them
            # elsewhere would store an unverifiable, unused assertion.
            raise HTTPException(
                status_code=400,
                detail=f"{_k} is only meaningful with temporal_basis "
                       f"'sold_units_full_lifetime'")
    d.temporal_basis = temporal_basis
    d.basis_units_sold = basis_units_sold
    d.basis_lifetime_years = basis_lifetime_years
    d.basis_per_unit_annual_co2e_kg = basis_per_unit_annual_co2e_kg
    d.gross_exposure_total = gross_exposure_total
    d.gross_exposure_currency = gross_exposure_currency
    d.screened_at = screened_at or now[:10]
    d.declared_by = declared_by
    d.standard_version = GHGP_STANDARD_VERSION
    d.updated_at = now
    db.commit(); db.refresh(d)
    return {"id": d.id, "category": d.category, "status": d.status,
            "note": "Recompute the run so this screen is frozen onto it."}


@app.post("/activities/ghgp-categories")
def bulk_assign_ghgp_category(category: str = Query(...), ghgp_category: int = Query(..., ge=1, le=15),
                              subcategory: Optional[str] = None,
                              org: Organisation = Depends(current_org),
                              db: Session = Depends(get_db)):
    """Bulk-assign a GHGP category to the org's activities matching a free-text
    category (so `unassigned` Scope 3 lines can be resolved without re-uploading)."""
    q = db.query(ActivityRecord).filter(ActivityRecord.organisation_id == org.id,
                                        ActivityRecord.category == category)
    if subcategory is not None:
        q = q.filter(ActivityRecord.subcategory == subcategory)
    rows = q.all()
    for a in rows:
        a.ghgp_category = ghgp_category
    db.commit()
    return {"updated": len(rows), "category": category, "ghgp_category": ghgp_category,
            "note": "Recompute the run to apply."}


@app.get("/reports/scope3_inventory")
def get_scope3_inventory(run_id: Optional[int] = None,
                         org: Organisation = Depends(current_org),
                         db: Session = Depends(get_db)):
    """The 15-category Scope 3 inventory + completeness statement (ESRS AR 46(i))."""
    from .reports.scope3 import scope3_inventory_report
    return JSONResponse(scope3_inventory_report(db, org.id, run_id=run_id))


@app.get("/reports/ecovadis")
def get_ecovadis_readiness(run_id: Optional[int] = None,
                           baseline_run_id: Optional[int] = None,
                           intensity_denominator: Optional[float] = None,
                           denominator_unit: str = "revenue",
                           has_environmental_policy: bool = False,
                           iso_14001_certified: bool = False,
                           published_sustainability_report: bool = False,
                           org: Organisation = Depends(current_org),
                           db: Session = Depends(get_db)):
    """EcoVadis Environment-theme readiness: evidence pack + gap list.

    NOT a score or medal (only EcoVadis issues those) and NOT the Labour/Ethics/
    Procurement themes. Policy/ISO-14001/report flags are self-attested.
    """
    from .reports.ecovadis import ecovadis_readiness
    if intensity_denominator is not None and (
            not math.isfinite(intensity_denominator) or intensity_denominator <= 0):
        raise HTTPException(status_code=400,
                            detail="intensity_denominator must be finite > 0")
    return JSONResponse(with_guidance(ecovadis_readiness(
        db, org.id, run_id=run_id, baseline_run_id=baseline_run_id,
        intensity_denominator=intensity_denominator, denominator_unit=denominator_unit,
        has_environmental_policy=has_environmental_policy,
        iso_14001_certified=iso_14001_certified,
        published_sustainability_report=published_sustainability_report)))


@app.get("/reports/sfdr_pai")
def get_sfdr_pai_report(portfolio_value_millions: Optional[float] = None,
                        include_scope3: bool = True,
                        portfolio_value_currency: str = "EUR",
                        fx_year: Optional[int] = None,
                        org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """PAI 1/2/3 over the PCAF portfolio.

    ``portfolio_value_currency`` is the currency of ``portfolio_value_millions``: PAI 2 is
    stated per EUR million, so a non-EUR value needs a loaded FX rate (``fx_year`` picks
    the rate year; the portfolio's latest as-of year is used otherwise). Without one the
    indicator is refused, never relabelled as EUR.
    """
    from .reports.sfdr_pai import sfdr_pai_report
    if portfolio_value_millions is not None and (
            not math.isfinite(portfolio_value_millions) or portfolio_value_millions <= 0):
        raise HTTPException(status_code=400, detail="portfolio_value_millions must be finite > 0")
    return JSONResponse(with_guidance(sfdr_pai_report(db, org.id,
                                                      portfolio_value_millions=portfolio_value_millions,
                                                      include_scope3=include_scope3,
                                                      portfolio_value_currency=portfolio_value_currency,
                                                      fx_year=fx_year)))


# --- Nature (TNFD LEAP + SBTN) -----------------------------------------------

@app.post("/nature/sites")
def create_nature_site(name: str = Query(...), country: Optional[str] = None,
                       biome: Optional[str] = None, latitude: Optional[float] = None,
                       longitude: Optional[float] = None, area_hectares: float = 0.0,
                       in_protected_area: bool = False, in_kba: bool = False,
                       water_stress: str = "unknown",
                       org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import NatureSite
    from .services.nature import WATER_STRESS_LEVELS
    from .services.calc import _utcnow_iso
    if water_stress not in WATER_STRESS_LEVELS:
        raise HTTPException(status_code=400,
                            detail=f"water_stress must be one of {list(WATER_STRESS_LEVELS)}")
    if not math.isfinite(area_hectares) or area_hectares < 0:
        raise HTTPException(status_code=400, detail="area_hectares must be finite >= 0")
    if latitude is not None and (not math.isfinite(latitude) or not -90 <= latitude <= 90):
        raise HTTPException(status_code=400, detail="latitude must be finite in [-90, 90]")
    if longitude is not None and (not math.isfinite(longitude) or not -180 <= longitude <= 180):
        raise HTTPException(status_code=400, detail="longitude must be finite in [-180, 180]")
    s = NatureSite(organisation_id=org.id, name=name, country=country, biome=biome,
                   latitude=latitude, longitude=longitude, area_hectares=area_hectares,
                   in_protected_area=in_protected_area, in_kba=in_kba,
                   water_stress=water_stress, created_at=_utcnow_iso())
    db.add(s); db.commit(); db.refresh(s)
    return {"id": s.id, "name": s.name, "water_stress": s.water_stress}


def _own_site(db: Session, org: Organisation, site_id: int):
    from .models import NatureSite
    s = db.query(NatureSite).filter(NatureSite.id == site_id,
                                    NatureSite.organisation_id == org.id).first()
    if s is None:
        raise HTTPException(status_code=404, detail="nature site not found for this organisation")
    return s


@app.post("/nature/sites/{site_id}/impacts")
def add_nature_impact(site_id: int, request: Request, kind: str = Query(...),
                      driver: str = Query(...), materiality: str = "low",
                      metric_value: Optional[float] = None, metric_unit: Optional[str] = None,
                      body: Optional[NatureImpactBody] = None,
                      org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import NatureImpactDependency
    _reject_narrative_query_params(request, "description")
    description = body.description if body else None
    from .services.nature import valid_driver, MATERIALITY, IMPACT_DRIVERS, DEPENDENCY_SERVICES
    s = _own_site(db, org, site_id)
    if kind not in ("impact", "dependency"):
        raise HTTPException(status_code=400, detail="kind must be 'impact' or 'dependency'")
    if not valid_driver(kind, driver):
        allowed = IMPACT_DRIVERS if kind == "impact" else DEPENDENCY_SERVICES
        raise HTTPException(status_code=400,
                            detail=f"driver for a {kind} must be one of {list(allowed)}")
    if materiality not in MATERIALITY:
        raise HTTPException(status_code=400, detail=f"materiality must be one of {list(MATERIALITY)}")
    if metric_value is not None and not math.isfinite(metric_value):
        raise HTTPException(status_code=400, detail="metric_value must be a finite number")
    it = NatureImpactDependency(site_id=s.id, kind=kind, driver=driver, materiality=materiality,
                                description=description, metric_value=metric_value,
                                metric_unit=metric_unit)
    db.add(it); db.commit(); db.refresh(it)
    return {"id": it.id, "site_id": s.id, "kind": it.kind, "driver": it.driver}


@app.post("/nature/targets")
def create_nature_target(realm: str = Query(...), name: str = Query(...),
                         baseline_value: float = Query(...), baseline_unit: str = Query(...),
                         target_value: float = Query(...), target_year: int = Query(...),
                         baseline_year: Optional[int] = None, validated: bool = False,
                         org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import NatureTarget
    from .services.nature import REALMS
    from .services.calc import _utcnow_iso
    if realm not in REALMS:
        raise HTTPException(status_code=400, detail=f"realm must be one of {list(REALMS)}")
    for nm, v in (("baseline_value", baseline_value), ("target_value", target_value)):
        if not math.isfinite(v):
            raise HTTPException(status_code=400, detail=f"{nm} must be a finite number")
    if not 2000 <= target_year <= 2100:
        raise HTTPException(status_code=400, detail="target_year must be in [2000, 2100]")
    t = NatureTarget(organisation_id=org.id, realm=realm, name=name,
                     baseline_value=baseline_value, baseline_unit=baseline_unit,
                     baseline_year=baseline_year, target_value=target_value,
                     target_year=target_year, validated=validated, created_at=_utcnow_iso())
    db.add(t); db.commit(); db.refresh(t)
    return {"id": t.id, "realm": t.realm, "name": t.name}


@app.get("/reports/tnfd")
def get_tnfd_report(org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .services.nature import leap_assessment
    return JSONResponse(with_guidance(leap_assessment(db, org.id)))


@app.get("/reports/sbtn")
def get_sbtn_report(org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .services.nature import sbtn_report
    return JSONResponse(with_guidance(sbtn_report(db, org.id)))


@app.post("/assurance/engagements")
def create_engagement(request: Request, run_id: int = Query(...),
                      standard: str = Query(...), level: str = Query(...),
                      period_label: Optional[str] = None, materiality_pct: float = 5.0,
                      body: Optional[EngagementBody] = None,
                      org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import AssuranceEngagement, CalculationRun
    _reject_narrative_query_params(request, "assuror_name")
    assuror_name = body.assuror_name if body else None
    from .services.calc import _utcnow_iso
    from .services.assurance_standards import (
        VALID_STANDARDS, standard_permitted, run_period_start,
    )
    if standard not in VALID_STANDARDS:
        raise HTTPException(status_code=400, detail="standard must be ISAE_3410|ISO_14064_3|ISSA_5000")
    if level not in ("limited", "reasonable"):
        raise HTTPException(status_code=400, detail="level must be limited|reasonable")
    if not (0 < materiality_pct <= 100):
        raise HTTPException(status_code=400, detail="materiality_pct must be in (0, 100]")
    run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                          CalculationRun.organisation_id == org.id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run_id not found for this organisation")
    # ISAE 3410 is withdrawn with effect from 2026-12-15 and cannot govern a period
    # beginning on or after it. Checked against the run's OWN period, not today: an
    # engagement over FY2025 remains an ISAE 3410 engagement whenever it is opened.
    # An unknown period warns rather than refuses — most runs are not period-scoped,
    # and blocking them all would be a far larger error than the one being prevented.
    period_start = run_period_start(db, run)
    verdict = standard_permitted(standard, period_start)
    if not verdict["permitted"]:
        raise HTTPException(status_code=400, detail=verdict["reason"])
    eng = AssuranceEngagement(organisation_id=org.id, run_id=run_id, standard=standard,
                              level=level, assuror_name=assuror_name,
                              period_label=period_label, materiality_pct=materiality_pct,
                              status="planned", created_at=_utcnow_iso())
    db.add(eng); db.commit(); db.refresh(eng)
    out = {"id": eng.id, "run_id": run_id, "standard": standard, "level": level}
    if verdict.get("warning"):
        out["warning"] = verdict["warning"]
    return out


def _own_engagement(db: Session, org: Organisation, engagement_id: int):
    from .models import AssuranceEngagement
    eng = db.query(AssuranceEngagement).filter(
        AssuranceEngagement.id == engagement_id,
        AssuranceEngagement.organisation_id == org.id).first()
    if eng is None:
        raise HTTPException(status_code=404, detail="engagement not found for this organisation")
    return eng


@app.post("/assurance/engagements/{engagement_id}/findings")
def add_finding(engagement_id: int, request: Request,
                body: Optional[FindingBody] = None,
                severity: str = Query(...), line_item_id: Optional[int] = None,
                org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import AssuranceFinding, EmissionLineItem
    _reject_narrative_query_params(request, "description")
    description = _require_body(body, "description").description
    from .services.calc import _utcnow_iso
    eng = _own_engagement(db, org, engagement_id)
    if eng.status == "concluded":
        raise HTTPException(status_code=409, detail="engagement is concluded; reopen not supported")
    if severity not in ("observation", "minor", "material"):
        raise HTTPException(status_code=400, detail="severity must be observation|minor|material")
    if line_item_id is not None:
        li = db.query(EmissionLineItem).filter(EmissionLineItem.id == line_item_id,
                                               EmissionLineItem.run_id == eng.run_id).first()
        if li is None:
            raise HTTPException(status_code=404, detail="line_item_id not in this engagement's run")
    if eng.status == "planned":
        eng.status = "in_progress"
    f = AssuranceFinding(engagement_id=eng.id, line_item_id=line_item_id, severity=severity,
                         description=description, status="open", created_at=_utcnow_iso())
    db.add(f); db.commit(); db.refresh(f)
    return {"id": f.id, "severity": f.severity, "status": f.status}


@app.post("/assurance/findings/{finding_id}/resolve")
def resolve_finding(finding_id: int, request: Request,
                    body: Optional[ResolveFindingBody] = None,
                    org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import AssuranceFinding, AssuranceEngagement
    _reject_narrative_query_params(request, "resolution_note")
    resolution_note = _require_body(body, "resolution_note").resolution_note
    row = db.query(AssuranceFinding, AssuranceEngagement).join(
        AssuranceEngagement, AssuranceEngagement.id == AssuranceFinding.engagement_id)\
        .filter(AssuranceFinding.id == finding_id,
                AssuranceEngagement.organisation_id == org.id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="finding not found for this organisation")
    f, eng = row
    # A concluded engagement's findings ledger is frozen — mutating it after the
    # opinion is issued would silently doctor the audit trail behind it.
    if eng.status == "concluded":
        raise HTTPException(status_code=409, detail="engagement is concluded; findings are frozen")
    f.status = "resolved"; f.resolution_note = resolution_note
    db.commit()
    return {"id": f.id, "status": f.status}


@app.post("/assurance/engagements/{engagement_id}/conclude")
def conclude_engagement(engagement_id: int, request: Request,
                        opinion: str = Query(...),
                        body: Optional[ConcludeBody] = None,
                        org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import CalculationRun, AssuranceFinding
    _reject_narrative_query_params(request, "opinion_note")
    opinion_note = body.opinion_note if body else None
    from .services.assurance import readiness_assessment
    from .services.calc import _utcnow_iso
    eng = _own_engagement(db, org, engagement_id)
    if opinion not in ("unqualified", "qualified", "adverse", "disclaimer"):
        raise HTTPException(status_code=400, detail="opinion must be unqualified|qualified|adverse|disclaimer")
    if eng.status == "concluded":
        raise HTTPException(status_code=409, detail="engagement already concluded")
    import json as _json
    run = db.get(CalculationRun, eng.run_id)
    readiness = readiness_assessment(db, run)
    # An unqualified conclusion cannot overstate the assurance obtained.
    if opinion == "unqualified":
        open_material = db.query(AssuranceFinding).filter(
            AssuranceFinding.engagement_id == eng.id,
            AssuranceFinding.status == "open",
            AssuranceFinding.severity == "material").count()
        if not readiness["ready"] or open_material:
            raise HTTPException(status_code=409,
                                detail="cannot issue unqualified: readiness checklist "
                                       "failing and/or open material findings — use "
                                       "qualified/adverse/disclaimer")
    eng.status = "concluded"; eng.opinion = opinion; eng.opinion_note = opinion_note
    eng.concluded_at = _utcnow_iso()
    eng.readiness_snapshot = _json.dumps(readiness)   # freeze the checklist as-issued
    db.commit()
    return {"id": eng.id, "opinion": opinion, "status": eng.status}


@app.post("/assurance/engagements/{engagement_id}/grant_access")
def grant_assurance_access(engagement_id: int,
                           org: Organisation = Depends(current_org),
                           db: Session = Depends(get_db)):
    """Mint a read-only token so an external assuror can view the engagement +
    the run's lineage WITHOUT an org key."""
    import secrets
    from .services.calc import _utcnow_iso  # noqa: F401
    eng = _own_engagement(db, org, engagement_id)
    token = secrets.token_urlsafe(32)
    eng.access_token_hash = _hash_key(token)
    db.commit()
    return {"engagement_id": eng.id, "assurance_token": token,
            "note": "Read-only. Provide as X-Assurance-Token. Shown once."}


def _engagement_for_reader(db: Session, engagement_id: int,
                           x_api_key: Optional[str], x_assurance_token: Optional[str]):
    """Resolve an engagement for either the owning org (X-API-Key) or an
    assuror holding the engagement's read-only token (X-Assurance-Token)."""
    import hmac
    from .models import AssuranceEngagement
    eng = db.get(AssuranceEngagement, engagement_id)
    # Check credentials against the engagement only if it exists — a nonexistent
    # id and an unauthorized one return the SAME 401, so a credential-less caller
    # cannot enumerate valid engagement ids (no existence oracle).
    if eng is not None:
        if x_assurance_token and eng.access_token_hash and \
                hmac.compare_digest(_hash_key(x_assurance_token), eng.access_token_hash):
            # Not current_org's path, so bind the tenant here too — otherwise the log
            # line for an external assuror's read says nothing about whose data it read.
            _bind_tenant(eng.organisation_id)
            return eng, "assuror"
        if x_api_key:
            o = db.query(Organisation).filter(
                Organisation.api_key_hash == _hash_key(x_api_key)).first()
            if o and eng.organisation_id == o.id:
                _bind_tenant(o.id)
                return eng, "owner"
    raise HTTPException(status_code=401, detail="valid X-API-Key (owner) or X-Assurance-Token required")


@app.get("/assurance/engagements/{engagement_id}")
def get_engagement(engagement_id: int, x_api_key: Optional[str] = Header(None),
                   x_assurance_token: Optional[str] = Header(None),
                   db: Session = Depends(get_db)):
    from .services.assurance import engagement_view
    eng, role = _engagement_for_reader(db, engagement_id, x_api_key, x_assurance_token)
    return JSONResponse(engagement_view(db, eng, include_owner_fields=(role == "owner")))


@app.get("/assurance/engagements/{engagement_id}/lineage")
def get_engagement_lineage(engagement_id: int, request: Request,
                           limit: Optional[int] = None, offset: Optional[int] = None,
                           x_api_key: Optional[str] = Header(None),
                           x_assurance_token: Optional[str] = Header(None),
                           db: Session = Depends(get_db)):
    """The run's frozen lineage, readable by the assuror via the engagement token.

    PAGINATED (default 100 line items). This is the response measured at 65.5 MB with a
    746 MB RSS peak: it serialised every line item of the run into one body, and the
    assuror — the one caller guaranteed to open it on a large inventory — was the one who
    paid for it. `page.has_more` and `Link: rel="next"` walk the rest.
    """
    import json as _json
    from .models import EmissionLineItem, CalculationRun
    from .services.pagination import next_link_headers, page_block, page_of_query
    eng, _role = _engagement_for_reader(db, engagement_id, x_api_key, x_assurance_token)
    run = db.get(CalculationRun, eng.run_id)
    limit, offset = _page_args(limit, offset)
    page = page_of_query(db.query(EmissionLineItem, ActivityRecord)
                         .join(ActivityRecord,
                               ActivityRecord.id == EmissionLineItem.activity_id)
                         .filter(EmissionLineItem.run_id == run.id)
                         .order_by(EmissionLineItem.id), limit, offset)
    body = {
        "engagement_id": eng.id, "run_id": run.id,
        "page": page_block(page, limit, offset),
        "line_items": [{
            "id": li.id, "scope": li.scope, "method": li.method, "co2e": li.co2e,
            "detail": _json.loads(li.details or "{}"),
            "activity": {"id": a.id, "date": a.date, "category": a.category,
                         "quantity": a.quantity, "unit": a.unit, "source_file": a.source_file},
        } for li, a in page["rows"]],
    }
    return JSONResponse(body, headers=next_link_headers(request, page))


def _csv_str(value) -> str:
    """A CSV cell as a clean string; blank/NaN become "" rather than the text "nan".

    pandas represents an empty cell as NaN, which is TRUTHY, so ``value or ""``
    yields "nan" and sails through every emptiness check downstream.
    """
    if value is None:
        return ""
    s = str(value).strip()
    return "" if s.lower() in ("nan", "nat", "none") else s


# --- Mapping audit trail -----------------------------------------------------

@app.get("/mappings/audit")
def get_mapping_audit(request: Request, activity_id: Optional[int] = None,
                      limit: Optional[int] = None, offset: Optional[int] = None,
                      org: Organisation = Depends(current_org),
                      db: Session = Depends(get_db)):
    """The append-only journal of factor-binding decisions, oldest first.

    PAGINATED (default 100 events); `summary` counts the WHOLE journal, not the page, so
    a reader cannot mistake one window for the complete record.
    """
    from .services.mapping_audit import history_page, summary
    from .services.pagination import next_link_headers, page_block
    limit, offset = _page_args(limit, offset)
    page = history_page(db, org.id, activity_id, limit=limit, offset=offset)
    return JSONResponse({"summary": summary(db, org.id),
                         "page": page_block(page, limit, offset),
                         "events": page["rows"]},
                        headers=next_link_headers(request, page))


@app.get("/mappings/audit/as_at")
def get_binding_as_at(activity_id: int, at: str,
                      org: Organisation = Depends(current_org),
                      db: Session = Depends(get_db)):
    """What an activity was bound to at a moment in time.

    The question an assuror actually asks, and the one an in-place status column
    cannot answer.
    """
    from .models import ActivityRecord as _AR
    from .services.mapping_audit import binding_as_at
    owned = db.query(_AR).filter(_AR.id == activity_id,
                                 _AR.organisation_id == org.id).first()
    if owned is None:
        raise HTTPException(status_code=404,
                            detail="activity not found for this organisation")
    return JSONResponse(binding_as_at(db, activity_id, at))


# --- Versioned classification crosswalks -------------------------------------

@app.post("/crosswalks")
def register_crosswalk(from_scheme: str = Query(...), to_scheme: str = Query(...),
                       source: str = Query(...), table_version: str = Query(...),
                       licence: Optional[str] = None, url: Optional[str] = None,
                       _: None = Depends(require_admin),
                       db: Session = Depends(get_db)):
    """Register a versioned concordance table between two classification schemes.

    ADMIN-GATED. A concordance is published reference data (Eurostat, UN) shared by
    every tenant, and `resolve` picks the newest registration when no version is
    pinned — so a tenant able to register one could silently redirect another
    tenant's unversioned lookups to a table of their own. Same gate as FX, CPI,
    residual mix and the CBAM tables, for the same reason.

    A tenant's OWN crosswalk (chart-of-accounts -> UNSPSC) is a different object and
    needs a tenant column before it can be offered; it is not this endpoint.
    """
    from .services.crosswalk import register
    out = register(db, from_scheme=from_scheme, to_scheme=to_scheme, source=source,
                   table_version=table_version, licence=licence, url=url)
    if not out["registered"] and not out.get("idempotent"):
        raise HTTPException(status_code=400, detail=out["reason"])
    return JSONResponse(out)


@app.post("/crosswalks/{crosswalk_id}/mappings")
async def add_crosswalk_mappings(crosswalk_id: int, request: Request,
                                 _: None = Depends(require_admin),
                                 db: Session = Depends(get_db)):
    """Add concordance rows. Body is a JSON array of
    {from_code, to_code, partial?, note?}.

    ADMIN-GATED. This handler resolved the caller's organisation and then never used
    it: any tenant key could add rows to any tenant's table. Adding candidates widens
    the measured dispersion of every hop through it, so one POST moved another
    tenant's next published uncertainty band by a factor of six.
    """
    from .services.crosswalk import add_mappings
    try:
        body = _json.loads(await request.body())
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be a JSON array")
    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail="body must be a JSON array")
    out = add_mappings(db, crosswalk_id, body)
    if out.get("reason"):
        raise HTTPException(status_code=404, detail=out["reason"])
    return JSONResponse(out)


@app.post("/activities/crosswalk_chain")
async def declare_crosswalk_chain(request: Request,
                                  activity_ids: Optional[str] = None,
                                  category: Optional[str] = None,
                                  source_file: Optional[str] = None,
                                  org: Organisation = Depends(current_org),
                                  db: Session = Depends(get_db)):
    """Declare the classification chain a set of activities was mapped through.

    Body is a JSON array of hops: {from_scheme, from_code, to_scheme,
    table_version?}. PREPARER-DECLARED and never written by the engine — an
    inferred chain would be indistinguishable from one the preparer stood behind.

    Once declared, the chain's measured variance is frozen onto each line at the
    next calculation and widens that line's Monte Carlo band when the propagation
    is asked to include it.
    """
    from .services.crosswalk import chain_uncertainty
    try:
        hops = _json.loads(await request.body())
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be a JSON array of hops")
    if not isinstance(hops, list) or not hops:
        raise HTTPException(status_code=400,
                            detail="body must be a non-empty JSON array of hops")
    if not (activity_ids or category or source_file):
        raise HTTPException(
            status_code=400,
            detail="supply activity_ids, category or source_file — declaring one "
                   "chain across an entire organisation would defeat the purpose")

    q = db.query(ActivityRecord).filter(ActivityRecord.organisation_id == org.id)
    if activity_ids:
        try:
            ids = [int(x) for x in activity_ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400,
                                detail="activity_ids must be comma-separated integers")
        q = q.filter(ActivityRecord.id.in_(ids))
    if category:
        q = q.filter(ActivityRecord.category == category)
    if source_file:
        q = q.filter(ActivityRecord.source_file == source_file)

    rows = q.all()
    for a in rows:
        a.crosswalk_chain = _json.dumps(hops)
    db.commit()
    # Show the caller what they just committed to, resolved now.
    preview = chain_uncertainty(db, hops, org.id)
    return JSONResponse({
        "activities_updated": len(rows),
        "activity_ids": [a.id for a in rows],
        "chain": preview,
        "note": "The chain is frozen onto each line at the NEXT calculation, so a "
                "later concordance revision cannot move an already-filed interval.",
    })


@app.get("/crosswalks/resolve")
def resolve_crosswalk(from_scheme: str, from_code: str, to_scheme: str,
                      table_version: Optional[str] = None,
                      org: Organisation = Depends(current_org),
                      db: Session = Depends(get_db)):
    """Resolve one code to its candidate set, with the measured uncertainty.

    Uncertainty is the dispersion of the candidate set's own factor values, not a
    pedigree score: a one-to-one hop is exactly zero, which no fixed 1-5 score can
    express.
    """
    from .services.crosswalk import hop_uncertainty
    return JSONResponse(hop_uncertainty(
        db, from_scheme=from_scheme, from_code=from_code, to_scheme=to_scheme,
        table_version=table_version, org_id=org.id))


@app.post("/crosswalks/chain")
async def resolve_crosswalk_chain(request: Request,
                                  org: Organisation = Depends(current_org),
                                  db: Session = Depends(get_db)):
    """Combine a chain of hops. Body is a JSON array of
    {from_scheme, from_code, to_scheme, table_version?}."""
    from .services.crosswalk import chain_uncertainty
    try:
        hops = _json.loads(await request.body())
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be a JSON array")
    if not isinstance(hops, list) or not hops:
        raise HTTPException(status_code=400,
                            detail="body must be a non-empty JSON array of hops")
    return JSONResponse(chain_uncertainty(db, hops, org.id))


# --- PACT v3 HOST side: serving the network ----------------------------------
# NOTE ON ROUTING: the token endpoint is deliberately NOT under /3. Everything
# else is /3/..., but the spec puts the token at {auth-base}/auth/token and the
# conformance runner defaults auth-base to base — routing it under /3 fails the
# first test case and aborts the whole run.

@app.post("/pact/clients")
def create_pact_client(request: Request,
                       body: Optional[PactClientBody] = None,
                       org: Organisation = Depends(current_org),
                       db: Session = Depends(get_db)):
    """Mint client credentials for one data recipient.

    Separate from the tenant X-API-Key: a partner must be revocable without
    touching the owner's own access.
    """
    from .services.pact_host import create_client
    _reject_narrative_query_params(request, "partner_name")
    return JSONResponse(create_client(
        db, org.id, body.partner_name if body else None))


@app.post("/pact/clients/{client_id}/revoke")
def revoke_pact_client(client_id: str,
                       org: Organisation = Depends(current_org),
                       db: Session = Depends(get_db)):
    """Revoke one partner's client credentials and invalidate its live tokens.

    The promise that a partner is revocable without touching the owner's own access had
    no route behind it, and `PactClient.revoked` was written by nothing. Scoped to the
    calling organisation: one tenant cannot revoke another's partner.
    """
    from .services.pact_host import revoke_client
    out = revoke_client(db, org.id, client_id)
    if not out["revoked"]:
        raise HTTPException(status_code=404, detail=out["reason"])
    return JSONResponse(out)


@app.post("/auth/token")
async def pact_token(request: Request, db: Session = Depends(get_db)):
    """OAuth2 client-credentials grant (PACT v3 section 5.5.1).

    Credentials arrive as HTTP Basic. The prose implies form fields named
    client_id/client_secret, but the spec's own example and the conformance runner
    send Authorization: Basic base64(id:secret) with only grant_type in the body —
    a host reading the body alone fails immediately. Both are accepted.
    """
    from .services.pact_host import issue_token, parse_basic_auth
    form = {}
    try:
        form = dict(await request.form())
    except Exception:
        pass
    if (form.get("grant_type") or "").strip() not in ("client_credentials", ""):
        return JSONResponse({"error": "unsupported_grant_type",
                             "error_description": "only client_credentials is supported"},
                            status_code=400)
    creds = parse_basic_auth(request.headers.get("authorization"))
    if creds is None:
        cid, secret = form.get("client_id"), form.get("client_secret")
        if not cid or not secret:
            return JSONResponse(
                {"error": "invalid_client",
                 "error_description": "supply credentials via HTTP Basic, or as "
                                      "client_id/client_secret in the body"},
                status_code=401)
        creds = (cid, secret)
    out = issue_token(db, creds[0], creds[1])
    if not out["ok"]:
        return JSONResponse({"error": out["error"],
                             "error_description": out["error_description"]},
                            status_code=401)
    return JSONResponse({"access_token": out["access_token"],
                         "token_type": out["token_type"],
                         "expires_in": out["expires_in"]})


@app.get("/.well-known/openid-configuration")
def pact_openid_configuration(request: Request):
    """Discovery. SHOULD, not MUST — the two conformance cases for it are the only
    non-mandatory ones."""
    base = str(request.base_url).rstrip("/")
    return JSONResponse({"issuer": base, "token_endpoint": f"{base}/auth/token",
                         "grant_types_supported": ["client_credentials"],
                         "token_endpoint_auth_methods_supported": ["client_secret_basic"]})


@app.get("/3/footprints")
def pact_list_footprints(request: Request, limit: int = 100, offset: int = 0,
                         db: Session = Depends(get_db)):
    """ListFootprints. Array filters arrive as REPEATED query keys.

    Filtering is OR within a criterion and AND between criteria, with values
    compared case-insensitively. Deprecated footprints are included unless
    status=Active is explicitly requested.
    """
    from .services.pact_host import (
        error, link_header, list_footprints, resolve_token,
    )
    from .services.pact_store import footprint_view
    auth = resolve_token(db, request.headers.get("authorization"))
    if not auth["ok"]:
        return JSONResponse(error(auth["code"], auth["message"]), status_code=401)
    if limit < 1 or limit > 1000:
        return JSONResponse(error("BadRequest", "limit must be 1..1000"),
                            status_code=400)
    if offset < 0:
        return JSONResponse(error("BadRequest", "offset must be >= 0"),
                            status_code=400)

    q = request.query_params
    status = q.get("status")
    if status and status.strip().lower() not in ("active", "deprecated"):
        return JSONResponse(error("BadRequest",
                                  "status must be Active or Deprecated"),
                            status_code=400)
    # The v2 OData $filter is deprecated in v3 and must not be honoured.
    if any(k.startswith("$") for k in q.keys()):
        return JSONResponse(error("BadRequest",
                                  "the OData $filter syntax was deprecated in v3; use "
                                  "the discrete query parameters"),
                            status_code=400)

    page = list_footprints(
        db, auth["organisation_id"], limit=limit, offset=offset,
        product_ids=q.getlist("productId"), company_ids=q.getlist("companyId"),
        geographies=q.getlist("geography"),
        classifications=q.getlist("classification"),
        status=status, valid_on=q.get("validOn"),
        valid_after=q.get("validAfter"), valid_before=q.get("validBefore"))

    body = {"data": [footprint_view(r, include_document=True)["document"]
                     for r in page["rows"]]}
    headers = {}
    if page["has_more"]:
        keep = {k: q.getlist(k) for k in q.keys() if k not in ("offset",)}
        base = str(request.base_url).rstrip("/")
        headers["Link"] = link_header(base, "/3/footprints", keep,
                                      page["next_offset"])
    return JSONResponse(body, headers=headers)


@app.get("/3/footprints/{footprint_id}")
def pact_get_footprint(footprint_id: str, request: Request,
                       db: Session = Depends(get_db)):
    """GetFootprint by PACT id."""
    from .models import ProductFootprint
    from .services.pact_host import error, resolve_token
    auth = resolve_token(db, request.headers.get("authorization"))
    if not auth["ok"]:
        return JSONResponse(error(auth["code"], auth["message"]), status_code=401)
    row = db.query(ProductFootprint).filter(
        ProductFootprint.organisation_id == auth["organisation_id"],
        ProductFootprint.direction == "published",
        ProductFootprint.pf_id == footprint_id).first()
    if row is None:
        return JSONResponse(error("NotFound", f"no footprint with id {footprint_id}"),
                            status_code=404)
    return JSONResponse({"data": _json.loads(row.document)})


@app.post("/3/events")
async def pact_events(request: Request, db: Session = Depends(get_db)):
    """Events endpoint, CloudEvents structured content mode.

    Anything invalid is a 4xx with an Error object.

    NOT YET THE FULL v3 CONTRACT, and this description is what a partner integrates
    against, so it says so. The spec has an accepted event return 200 with an EMPTY body
    and then answers a RequestCreatedEvent asynchronously with exactly one fulfilled OR
    rejected callback delivered to the requester. This handler answers synchronously with
    a non-empty body describing what it found, and delivers no callback — there is no
    outbound HTTP client on this path. `pact_host.build_response_event` constructs a
    conformant callback and is deliberately retained for when delivery is wired; until
    then a partner must not wait on one.
    """
    from .services.pact_host import (
        EVENT_TYPES, error, is_cloudevents_media_type, list_footprints,
        request_filters, resolve_token, validate_event,
    )
    from .services.pact_store import footprint_view
    auth = resolve_token(db, request.headers.get("authorization"))
    if not auth["ok"]:
        return JSONResponse(error(auth["code"], auth["message"]), status_code=401)
    if not is_cloudevents_media_type(request.headers.get("content-type")):
        return JSONResponse(
            error("BadRequest",
                  "events must be sent in CloudEvents structured content mode with "
                  "media type application/cloudevents+json"),
            status_code=400)
    try:
        body = _json.loads(await request.body())
    except ValueError:
        return JSONResponse(error("BadRequest", "event body is not valid JSON"),
                            status_code=400)

    verdict = validate_event(body)
    if not verdict["valid"]:
        return JSONResponse(error(verdict["code"], verdict["message"]),
                            status_code=400)

    result = {"accepted": True, "event_type": verdict["event_type"]}
    if verdict["event_type"] == EVENT_TYPES["request_created"]:
        f = request_filters(verdict["data"])
        page = list_footprints(db, auth["organisation_id"], limit=1000, offset=0, **f)
        docs = [footprint_view(r, include_document=True)["document"]
                for r in page["rows"]]
        # The id is echoed BYTE-FOR-BYTE: the runner encodes routing data into it
        # and recovers it by splitting on '/'.
        result["callback"] = {
            "would_send": "fulfilled" if docs else "rejected",
            "request_event_id": body["id"],
            "footprints_matched": len(docs),
            "note": "A RequestFulfilledEvent needs at least one footprint (pfs has "
                    "minItems 1), so an empty match sends RequestRejectedEvent "
                    "instead — 'success with zero results' is not conformant.",
        }
    return JSONResponse(result)


# --- SBTi Corporate Net-Zero Standard V2.0 -----------------------------------

@app.get("/reports/sbti_v2")
def get_sbti_v2_report(run_id: Optional[int] = None,
                       base_run_id: Optional[int] = None,
                       turnover_eur: Optional[float] = None,
                       fte: Optional[int] = None,
                       balance_sheet_eur: Optional[float] = None,
                       high_income_country: Optional[bool] = None,
                       org: Organisation = Depends(current_org),
                       db: Session = Depends(get_db)):
    """SBTi V2.0 significance, company categorisation and target boundary.

    Significance is 5% of scope 3 categories 1-14 in the PHYSICAL inventory — never
    total Scope 3, never the whole inventory, and never including category 15. There
    is no aggregate coverage floor: the old 67% rule is gone and nothing backstops it.
    """
    from .reports.sbti_v2 import sbti_v2_report
    if run_id is not None:
        run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                              CalculationRun.organisation_id == org.id).first()
    else:
        run = db.query(CalculationRun).filter(CalculationRun.organisation_id == org.id)\
            .order_by(CalculationRun.id.desc()).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found for this organisation")
    base_run = None
    if base_run_id is not None:
        base_run = db.query(CalculationRun).filter(
            CalculationRun.id == base_run_id,
            CalculationRun.organisation_id == org.id).first()
        if base_run is None:
            raise HTTPException(status_code=404,
                                detail="base_run_id not found for this organisation")
    return JSONResponse(with_guidance(sbti_v2_report(
        db, run, turnover_eur=turnover_eur, fte=fte,
        balance_sheet_eur=balance_sheet_eur, high_income_country=high_income_country,
        base_run=base_run)))


@app.post("/sbti_v2/scope2_conformance")
def check_scope2_conformance(target_types: str = Query(...),
                             company_category: Optional[str] = None,
                             projected_electricity_growth: Optional[float] = None,
                             coverage_pct: float = 100.0,
                             org: Organisation = Depends(current_org)):
    """C12 Scope 2 target conformance. `target_types` is comma-separated.

    ONE target suffices — V2.0 does NOT require a location-based plus market-based
    pair. That appeared in the consultation drafts and was dropped.
    """
    from .services.sbti_v2 import scope2_target_conformance
    types = [t.strip() for t in target_types.split(",") if t.strip()]
    if company_category not in (None, "A", "B"):
        raise HTTPException(status_code=400,
                            detail="company_category must be 'A' or 'B' — V2.0 defines "
                                   "exactly two categories, there is no Category C")
    return JSONResponse(scope2_target_conformance(
        types, company_cat=company_category,
        projected_electricity_growth=projected_electricity_growth,
        coverage_pct=coverage_pct))


# --- Period-over-period screening over DECLARED series -----------------------

@app.post("/activities/series_key")
def declare_series_key(series_key: str = Query(...),
                       activity_ids: Optional[str] = None,
                       category: Optional[str] = None,
                       source_file: Optional[str] = None,
                       org: Organisation = Depends(current_org),
                       db: Session = Depends(get_db)):
    """Declare which physical series a set of activities belongs to.

    PREPARER-DECLARED and never written by the engine, for the same reason `scope`
    and `ghgp_category` are not: a derived value written here would be
    indistinguishable from a declaration on the next run. Nothing else on the row
    identifies a meter or a site, so an inferred key would merge physically
    distinct sites and report their sum as one trend.

    Select by explicit ids, or in bulk by category and/or source_file — the same
    two selectors /activities/coverage_window and /activities/entity use.
    """
    if not series_key.strip():
        raise HTTPException(status_code=400, detail="series_key must not be empty")
    q = db.query(ActivityRecord).filter(ActivityRecord.organisation_id == org.id)
    if activity_ids:
        try:
            ids = [int(x) for x in activity_ids.split(",") if x.strip()]
        except ValueError:
            raise HTTPException(status_code=400,
                                detail="activity_ids must be comma-separated integers")
        q = q.filter(ActivityRecord.id.in_(ids))
    if category:
        q = q.filter(ActivityRecord.category == category)
    if source_file:
        q = q.filter(ActivityRecord.source_file == source_file)
    if not (activity_ids or category or source_file):
        raise HTTPException(
            status_code=400,
            detail="supply activity_ids, category or source_file — declaring a series "
                   "across an entire organisation would defeat the purpose of the key")
    rows = q.all()
    for a in rows:
        a.series_key = series_key.strip()
    db.commit()
    return {"series_key": series_key.strip(), "activities_updated": len(rows),
            "activity_ids": [a.id for a in rows]}


@app.post("/bills/validate")
async def validate_utility_bill(request: Request,
                                provenance_tier: str = "text_layer",
                                org: Organisation = Depends(current_org)):
    """Validate a PARSED utility bill and return candidate activity rows.

    This is the validation half of bill extraction, and the half that is actually built:
    register arithmetic, MPAN mod-11, unit sanity (kVArh and kVA are not energy), gas
    volume-to-kWh with the calorific value, ESPI read quality, supersession of an
    estimate by a catch-up read, and whether the invoice reconciles to its own line
    items. THERE IS NO OCR HERE — you supply the parsed bill, from whatever read it.

    That distinction is the point of `provenance_tier`: a figure lifted from an embedded
    Factur-X attachment and one guessed by OCR must never be mistaken for each other
    downstream, so the tier travels on every row alongside the document hash.

    Rows are SUGGESTIONS. Nothing enters an inventory from here — the reconciliation
    verdict travels with them, and a bill that does not reconcile still returns its rows
    so a human can see what was wrong rather than being told only that it failed.
    """
    from .services.bill_extraction import (
        PROVENANCE_TIERS, document_hash, reconcile, to_activity_rows,
    )
    if provenance_tier not in PROVENANCE_TIERS:
        raise HTTPException(status_code=400,
                            detail=f"provenance_tier must be one of {list(PROVENANCE_TIERS)}")
    raw = await request.body()
    try:
        bill = _json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    if not isinstance(bill, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    doc_hash = document_hash(raw)
    out = to_activity_rows(bill, doc_hash=doc_hash, provenance_tier=provenance_tier)
    return {
        "organisation_id": org.id,
        "document_hash": doc_hash,
        "provenance_tier": provenance_tier,
        "reconciliation": reconcile(bill),
        **out,
        "note": "Candidate rows only — nothing has been written. Review them, then post "
                "the ones you accept through the normal activity upload.",
    }


@app.get("/reports/series_screen_history")
def get_series_screen_history(period_ids: str = Query(...),
                              org: Organisation = Depends(current_org),
                              db: Session = Depends(get_db)):
    """Screen an ORDERED run of periods and reclassify persistent deviations.

    `period_ids` is a comma-separated list in chronological order, oldest first.

    compare() screens ONE pair and cannot see a persistent shift — with two periods
    there is no way to tell a step from a spike. classify_level_shifts needs an
    accumulated history, and nothing assembled one, so the ISAE 3410 A101 treatment the
    module documented was unreachable: a site being commissioned was filed as a data
    defect every period, forever.
    """
    from .services.series_screen import compare, classify_level_shifts
    ids = [p.strip() for p in period_ids.split(",") if p.strip()]
    try:
        ids = [int(p) for p in ids]
    except ValueError:
        raise HTTPException(status_code=400, detail="period_ids must be integers")
    if len(ids) < 3:
        raise HTTPException(
            status_code=400,
            detail="at least three periods are required: a level shift is a deviation "
                   "persisting across consecutive comparisons, and two periods yield "
                   "only one comparison")
    payloads = []
    for baseline, current in zip(ids, ids[1:]):
        payloads.append(compare(db, org.id, current, baseline))
    out = classify_level_shifts(payloads)
    return {"period_ids": ids,
            "comparisons": payloads,
            "level_shifts": out["level_shifts"],
            "anomalies": out["anomalies"],
            "min_consecutive_periods": out["min_consecutive_periods"],
            "adjacency_note": out["adjacency_note"]}


@app.get("/activities/series")
def get_series_enrolment(org: Organisation = Depends(current_org),
                         db: Session = Depends(get_db)):
    """How much of the inventory is enrolled in period-over-period screening.

    An unenrolled row is not a clean one — it is simply not looked at.
    """
    from .services.series_screen import enrolment
    return JSONResponse(enrolment(db, org.id))


@app.get("/reports/series_screen")
def get_series_screen(current_period_id: int, baseline_period_id: int,
                      org: Organisation = Depends(current_org),
                      db: Session = Depends(get_db)):
    """Screen one reporting period against a baseline, series by series.

    The GHG Protocol Corporate Standard ch.7 rule — "changes of over 10 percent
    from year to year may warrant further investigation" — applied as a robust band
    on the LOG RATIO rather than a flat 10%, because weather and occupancy alone
    move a heating series further than that. A z-score is not used: it is bounded
    at (n-1)/sqrt(n) and cannot exceed 3 at ten or fewer points.
    """
    from .services.series_screen import compare
    result = compare(db, org.id, current_period_id, baseline_period_id)
    if not result.get("available") and result.get("status") == "period_not_found":
        raise HTTPException(status_code=404, detail=result["reason"])
    return JSONResponse(result)


# --- Pre-calculation screening: the assurance exception register -------------

@app.post("/activities/screen")
def run_screening(materiality_pct: float = 5.0, trivial_floor_pct: float = 0.25,
                  org: Organisation = Depends(current_org),
                  db: Session = Depends(get_db)):
    """Screen this organisation's activity data and update the exception register.

    Findings are keyed by a stable hash of the check and the activities it
    concerns, so re-screening UPDATES rather than duplicates and a disposition made
    earlier still attaches. A defect that has gone away is marked superseded, never
    deleted — ISAE 3410 para 69 forbids discarding engagement documentation.
    """
    from .services.screening import screen
    if not (0 < materiality_pct <= 100):
        raise HTTPException(status_code=400, detail="materiality_pct must be in (0, 100]")
    if not (0 <= trivial_floor_pct < materiality_pct):
        raise HTTPException(
            status_code=400,
            detail="trivial_floor_pct must be >= 0 and below materiality_pct — "
                   "'clearly trivial' is a different, smaller order of magnitude than "
                   "'not material' (ISAE 3410 A112)")
    return JSONResponse(screen(db, org.id, materiality_pct=materiality_pct,
                               trivial_floor_pct=trivial_floor_pct))


@app.get("/activities/findings")
def list_findings(request: Request, status: Optional[str] = None,
                  severity: Optional[str] = None, check_code: Optional[str] = None,
                  limit: Optional[int] = None, offset: Optional[int] = None,
                  org: Organisation = Depends(current_org),
                  db: Session = Depends(get_db)):
    """The exception register: every live finding with its expectation and threshold.

    PAGINATED (default 100); `summary` counts the whole register, not the page.
    """
    from .models import ActivityFinding
    from .services.screening import finding_view, summary
    q = db.query(ActivityFinding).filter(ActivityFinding.organisation_id == org.id)
    if status:
        q = q.filter(ActivityFinding.status == status)
    if severity:
        q = q.filter(ActivityFinding.severity == severity)
    if check_code:
        q = q.filter(ActivityFinding.check_code == check_code)
    from .services.pagination import next_link_headers, page_block, page_of_query
    limit, offset = _page_args(limit, offset)
    page = page_of_query(q.order_by(ActivityFinding.id), limit, offset)
    return JSONResponse({"summary": summary(db, org.id),
                         "page": page_block(page, limit, offset),
                         "findings": [finding_view(r) for r in page["rows"]]},
                        headers=next_link_headers(request, page))


@app.post("/activities/findings/{finding_id}/dispose")
def dispose_finding(finding_id: int, request: Request,
                    body: Optional[DisposeFindingBody] = None,
                    status: str = Query(...), reason_code: str = Query(...),
                    org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
    """Record a disposition: corrected, or accepted with a reason.

    A bare acknowledgement is refused. PCAOB Staff Audit Practice Alert No. 11:
    "Verifying that a review was signed off provides little or no evidence by
    itself about the control's effectiveness." A disposition needs a reason code
    from the closed vocabulary AND a substantive note saying what was investigated
    and concluded.
    """
    from .services.screening import dispose
    _reject_narrative_query_params(request, "note")
    note = _require_body(body, "note").note
    result = dispose(db, org.id, finding_id, status=status,
                     reason_code=reason_code, note=note)
    if result["disposed"]:
        return JSONResponse(result)
    if "not found" in result["reason"]:
        raise HTTPException(status_code=404, detail=result["reason"])
    raise HTTPException(status_code=400, detail=result["reason"])


@app.get("/reports/screening")
def get_screening_report(run_id: Optional[int] = None,
                         org: Organisation = Depends(current_org),
                         db: Session = Depends(get_db)):
    """The screening state frozen onto a run, with its blockers and warnings.

    A run computed before screening existed returns the legacy branch and is never
    retroactively blocked — the same anti-cliff rule the residual-mix and Scope 3
    temporal gates use.
    """
    from .services.screening import completeness
    if run_id is not None:
        run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                              CalculationRun.organisation_id == org.id).first()
    else:
        run = db.query(CalculationRun).filter(CalculationRun.organisation_id == org.id)\
            .order_by(CalculationRun.id.desc()).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found for this organisation")
    return JSONResponse(completeness(db, run))


# --- PACT Pathfinder (WBCSD) product carbon footprint exchange ---------------
# The consume side: a supplier's PCF, validated against the v3 Technical
# Specifications and stored verbatim, so it can later replace an industry-average
# factor with primary data.

@app.post("/pact/footprints/import")
async def import_product_footprint(request: Request,
                                   direction: str = "received",
                                   source_url: Optional[str] = None,
                                   org: Organisation = Depends(current_org),
                                   db: Session = Depends(get_db)):
    """Import a PACT v3 ProductFootprint document (JSON body).

    A non-conforming document is REFUSED with its errors, never stored and flagged:
    a stored footprint is exactly what later becomes a primary-data factor, and a
    flag is not a barrier once the row exists.
    """
    from .services.pact_store import import_footprint
    body = await request.body()
    result = import_footprint(db, org.id, body, direction=direction,
                              source_url=source_url)
    if result.get("stored"):
        return result
    if result.get("idempotent"):
        return JSONResponse(result, status_code=200)
    # Distinguish a protocol violation (same id, different content) from a plain
    # validation failure: the caller must do different things about each.
    status = 409 if result.get("incoming_content_hash") else 422
    return JSONResponse(result, status_code=status)


@app.get("/pact/footprints")
def get_product_footprints(request: Request, direction: Optional[str] = None,
                           status: Optional[str] = None,
                           product_id: Optional[str] = None,
                           limit: Optional[int] = None, offset: Optional[int] = None,
                           org: Organisation = Depends(current_org),
                           db: Session = Depends(get_db)):
    """Footprints this organisation holds, newest first. PAGINATED (default 100).

    `data` keeps its name and element shape; the `page` block is additive.
    """
    from .services.pact_store import list_footprints_page
    from .services.pagination import next_link_headers, page_block
    limit, offset = _page_args(limit, offset)
    page = list_footprints_page(db, org.id, direction=direction, status=status,
                                product_id=product_id, limit=limit, offset=offset)
    return JSONResponse({"data": page["rows"],
                         "page": page_block(page, limit, offset)},
                        headers=next_link_headers(request, page))


@app.get("/pact/footprints/{footprint_id}")
def get_product_footprint(footprint_id: int, include_document: bool = False,
                          org: Organisation = Depends(current_org),
                          db: Session = Depends(get_db)):
    """One held footprint. ``include_document`` returns the verbatim bytes as
    received — the evidence an assuror asks for, not our reconstruction of it."""
    from .models import ProductFootprint
    from .services.pact_store import footprint_view
    row = db.query(ProductFootprint).filter(
        ProductFootprint.id == footprint_id,
        ProductFootprint.organisation_id == org.id).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail="footprint not found for this organisation")
    return JSONResponse(footprint_view(row, include_document=include_document))


@app.post("/pact/footprints/{footprint_id}/materialise")
def materialise_pact_factor(footprint_id: int,
                            category: Optional[str] = None,
                            subcategory: Optional[str] = None,
                            org: Organisation = Depends(current_org),
                            db: Session = Depends(get_db)):
    """Turn a held supplier footprint into a supplier-specific emission factor.

    The factor is an ordinary EmissionFactor with method_type='supplier_specific',
    so every existing mechanism applies to it unchanged — the pedigree reliability
    indicator (1, the best, against 5 for spend-based), the narrowed Monte Carlo
    interval, the primary-data share, the GWP-vintage guard and the frozen per-line
    lineage. No special case was added to the calculation engine.
    """
    from .models import ProductFootprint
    from .services.pact_factor import materialise
    row = db.query(ProductFootprint).filter(
        ProductFootprint.id == footprint_id,
        ProductFootprint.organisation_id == org.id).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail="footprint not found for this organisation")
    result = materialise(db, row, category=category, subcategory=subcategory)
    if result.get("materialised") or result.get("idempotent"):
        return JSONResponse(result)
    return JSONResponse(result, status_code=422)


@app.get("/pact/footprints/{footprint_id}/materialisation")
def preview_pact_factor(footprint_id: int,
                        org: Organisation = Depends(current_org),
                        db: Session = Depends(get_db)):
    """Whether a footprint can become a factor, and what that factor would be —
    without writing anything."""
    from .models import ProductFootprint
    from .services.pact_factor import materialisation_verdict
    row = db.query(ProductFootprint).filter(
        ProductFootprint.id == footprint_id,
        ProductFootprint.organisation_id == org.id).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail="footprint not found for this organisation")
    return JSONResponse(materialisation_verdict(db, row))


@app.post("/pact/factors/{factor_id}/bind")
def bind_pact_factor(factor_id: int, activity_ids: str = Query(...),
                     org: Organisation = Depends(current_org),
                     db: Session = Depends(get_db)):
    """Bind activities to a materialised supplier factor. `activity_ids` is a
    comma-separated list.

    An explicit per-activity decision, never an automatic name match: the buyer
    knows which purchase the supplier's product is, and a fuzzy match would put
    someone else's footprint on a line with nothing in the result to reveal it.
    """
    from .services.pact_factor import bind_activities
    try:
        ids = [int(x) for x in activity_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400,
                            detail="activity_ids must be a comma-separated list of integers")
    if not ids:
        raise HTTPException(status_code=400, detail="activity_ids must not be empty")
    result = bind_activities(db, org.id, factor_id, ids)
    if result.get("reason") and result["bound"] == 0 and "not a materialised" in result["reason"]:
        raise HTTPException(status_code=404, detail=result["reason"])
    return JSONResponse(result)


@app.post("/pact/validate")
async def validate_product_footprint(request: Request,
                                     org: Organisation = Depends(current_org)):
    """Validate a document against the v3 spec WITHOUT storing it.

    Errors block an import; warnings do not. Useful before publishing one of your
    own footprints to a customer.
    """
    from .services.pact import parse_document, validate
    doc, err = parse_document(await request.body())
    if err:
        raise HTTPException(status_code=400, detail=err)
    return JSONResponse(validate(doc))


# --- Hourly Scope 2 (proposed GHG Protocol revision: temporal matching) -------
# A PARALLEL method beside the annual location/market figures. Nothing here feeds
# compute_co2e, so an organisation with no hourly data is unaffected in every way.

@app.post("/hourly/certificates")
def create_granular_certificate(
        issuer: str = Query(...), certificate_ref: str = Query(...),
        production_start: str = Query(...), production_end: str = Query(...),
        kwh: float = Query(...), grid_region: str = Query(...),
        technology: Optional[str] = None, production_device_id: Optional[str] = None,
        kg_co2e_per_kwh: float = 0.0,
        org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """Register an hourly energy attribute certificate (EnergyTag-shaped)."""
    from .models import GranularCertificate
    from .services.calc import _utcnow_iso
    from .services.hourly_scope2 import _parse_hour
    if not math.isfinite(kwh) or kwh <= 0:
        raise HTTPException(status_code=400, detail="kwh must be a positive finite number")
    if not math.isfinite(kg_co2e_per_kwh) or kg_co2e_per_kwh < 0:
        raise HTTPException(status_code=400, detail="kg_co2e_per_kwh must be >= 0")
    s, e = _parse_hour(production_start), _parse_hour(production_end)
    if s is None or e is None:
        raise HTTPException(status_code=400,
                            detail="production_start/end must be ISO-8601 datetimes")
    if e <= s:
        raise HTTPException(status_code=400, detail="production_end must be after production_start")
    dup = db.query(GranularCertificate).filter(
        GranularCertificate.issuer == issuer,
        GranularCertificate.certificate_ref == certificate_ref).first()
    if dup is not None:
        # Global uniqueness is the anti-double-counting guard: the same certificate
        # loaded twice under two ids would be matched twice.
        raise HTTPException(status_code=409,
                            detail=f"certificate {issuer}/{certificate_ref} already registered")
    c = GranularCertificate(
        organisation_id=org.id, issuer=issuer, certificate_ref=certificate_ref,
        production_start=production_start, production_end=production_end, kwh=kwh,
        technology=technology, grid_region=grid_region,
        production_device_id=production_device_id,
        kg_co2e_per_kwh=kg_co2e_per_kwh, created_at=_utcnow_iso())
    db.add(c); db.commit(); db.refresh(c)
    return {"id": c.id, "issuer": c.issuer, "certificate_ref": c.certificate_ref,
            "kwh": c.kwh, "grid_region": c.grid_region}


@app.post("/hourly/certificates/{certificate_id}/retire")
def retire_granular_certificate(certificate_id: int, reporting_period_id: int = Query(...),
                                org: Organisation = Depends(current_org),
                                db: Session = Depends(get_db)):
    """Retire a certificate against ONE reporting period.

    Retirement is the double-counting guard and it is one-way: a certificate already
    retired against a different period cannot be re-retired, because that is exactly
    the claim the guard exists to prevent.
    """
    from .models import GranularCertificate, ReportingPeriod
    from .services.calc import _utcnow_iso
    c = db.query(GranularCertificate).filter(
        GranularCertificate.id == certificate_id,
        GranularCertificate.organisation_id == org.id).first()
    if c is None:
        raise HTTPException(status_code=404, detail="certificate not found for this organisation")
    p = db.query(ReportingPeriod).filter(
        ReportingPeriod.id == reporting_period_id,
        ReportingPeriod.organisation_id == org.id).first()
    if p is None:
        raise HTTPException(status_code=404, detail="reporting period not found for this organisation")
    if c.retired_for_period_id is not None and c.retired_for_period_id != reporting_period_id:
        raise HTTPException(
            status_code=409,
            detail=f"certificate already retired against period {c.retired_for_period_id}; "
                   f"retiring it again would be the double count this guard prevents")
    c.retired_for_period_id = reporting_period_id
    c.retired_at = c.retired_at or _utcnow_iso()
    db.commit()
    return {"id": c.id, "retired_for_period_id": c.retired_for_period_id,
            "retired_at": c.retired_at}


@app.post("/hourly/loads")
def upload_hourly_load(file: UploadFile = File(...),
                       org: Organisation = Depends(current_org),
                       db: Session = Depends(get_db)):
    """Upload metered hourly consumption. CSV: hour_start,kwh,grid_region[,metering_point].

    A row that cannot be parsed is REJECTED and reported, never coerced to zero — an
    hour silently scored as zero load would count as perfectly matched.
    """
    from .models import HourlyLoad
    from .services.calc import _utcnow_iso
    from .services.hourly_scope2 import _parse_hour
    raw = file.file.read()
    try:
        df = pd.read_csv(io_BytesIO(raw))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"could not parse CSV: {exc}")
    df.columns = [c.strip().lower() for c in df.columns]
    required = {"hour_start", "kwh", "grid_region"}
    missing = required - set(df.columns)
    if missing:
        raise HTTPException(status_code=400,
                            detail=f"CSV is missing required column(s): {sorted(missing)}")
    accepted, rejected = 0, []
    for i, row in df.iterrows():
        hour = _csv_str(row.get("hour_start"))
        if _parse_hour(hour) is None:
            rejected.append({"row": int(i), "reason": "unparseable hour_start", "value": hour})
            continue
        try:
            kwh = float(row.get("kwh"))
        except (TypeError, ValueError):
            rejected.append({"row": int(i), "reason": "non-numeric kwh"})
            continue
        if not math.isfinite(kwh) or kwh < 0:
            rejected.append({"row": int(i), "reason": "kwh must be finite and >= 0"})
            continue
        # pandas reads an empty cell as NaN, and NaN is TRUTHY — so `x or ""`
        # yields the string "nan", which passes an emptiness check and would store a
        # load row against a region called "nan" that can never match a certificate.
        # This is the same trap services/ingestion.py documents.
        region = _csv_str(row.get("grid_region"))
        if not region:
            rejected.append({"row": int(i), "reason": "missing grid_region"})
            continue
        point = _csv_str(row.get("metering_point")) or "default"
        existing = db.query(HourlyLoad).filter(
            HourlyLoad.organisation_id == org.id, HourlyLoad.metering_point == point,
            HourlyLoad.hour_start == hour, HourlyLoad.entity_id.is_(None)).first()
        if existing is not None:
            existing.kwh, existing.grid_region = kwh, region
        else:
            db.add(HourlyLoad(organisation_id=org.id, metering_point=point,
                              hour_start=hour, kwh=kwh, grid_region=region,
                              source_file=file.filename, created_at=_utcnow_iso()))
        accepted += 1
    db.commit()
    return {"accepted": accepted, "rejected": len(rejected), "rejections": rejected[:50],
            "note": "Rejected rows are NOT stored as zero-load hours; they are absent, "
                    "and the matching report counts the resulting hour gap."}


@app.post("/reference/hourly_grid_intensity")
def add_hourly_grid_intensity(grid_region: str = Query(...), hour_start: str = Query(...),
                              kg_co2e_per_kwh_average: float = Query(...),
                              source: str = Query(...),
                              kg_co2e_per_kwh_residual: Optional[float] = None,
                              version: str = "1",
                              _: None = Depends(require_admin),
                              db: Session = Depends(get_db)):
    """Reference intensity for one region-hour: grid average and residual mix.

    ADMIN-GATED. HourlyGridIntensity has no tenant column and the matcher takes the
    highest id per (hour, region), so an unguarded write let any tenant replace the
    intensity in force for everyone — a multiplier on a reported Scope 2 figure, not
    merely on an uncertainty band.
    """
    from .models import HourlyGridIntensity
    from .services.calc import _utcnow_iso
    from .services.hourly_scope2 import _parse_hour
    if _parse_hour(hour_start) is None:
        raise HTTPException(status_code=400, detail="hour_start must be an ISO-8601 datetime")
    for nm, v in (("kg_co2e_per_kwh_average", kg_co2e_per_kwh_average),
                  ("kg_co2e_per_kwh_residual", kg_co2e_per_kwh_residual)):
        if v is not None and (not math.isfinite(v) or v < 0):
            raise HTTPException(status_code=400, detail=f"{nm} must be finite and >= 0")
    if (kg_co2e_per_kwh_residual is not None
            and kg_co2e_per_kwh_residual + 1e-9 < kg_co2e_per_kwh_average):
        # Residual strips out attributes other purchasers already claimed, so it can
        # never be below the average. A lower value is proof of a wrong row.
        raise HTTPException(
            status_code=400,
            detail="residual intensity below the grid average is arithmetically "
                   "impossible — the residual mix has other purchasers' clean "
                   "attributes removed and is always >= the average")
    row = HourlyGridIntensity(
        grid_region=grid_region, hour_start=hour_start,
        kg_co2e_per_kwh_average=kg_co2e_per_kwh_average,
        kg_co2e_per_kwh_residual=kg_co2e_per_kwh_residual,
        source=source, version=version, created_at=_utcnow_iso())
    db.add(row); db.commit(); db.refresh(row)
    return {"id": row.id, "grid_region": row.grid_region, "hour_start": row.hour_start}


@app.post("/hourly/deliverability_links")
def create_deliverability_link(request: Request, from_region: str = Query(...),
                               to_region: str = Query(...), basis: str = Query(...),
                               body: Optional[DeliverabilityLinkBody] = None,
                               org: Organisation = Depends(current_org),
                               db: Session = Depends(get_db)):
    """Declare that certificates from one region are physically deliverable to another."""
    from .models import DeliverabilityLink
    from .services.calc import _utcnow_iso
    _reject_narrative_query_params(request, "rationale")
    rationale = body.rationale if body else None
    if from_region.strip().upper() == to_region.strip().upper():
        raise HTTPException(status_code=400,
                            detail="same-region supply is deliverable implicitly; no link needed")
    link = DeliverabilityLink(organisation_id=org.id, from_region=from_region,
                              to_region=to_region, basis=basis, rationale=rationale,
                              created_at=_utcnow_iso())
    db.add(link); db.commit(); db.refresh(link)
    return {"id": link.id, "from_region": link.from_region, "to_region": link.to_region}


@app.get("/reports/hourly_scope2")
def get_hourly_scope2(reporting_period_id: int, include_hours: bool = False,
                      org: Organisation = Depends(current_org),
                      db: Session = Depends(get_db)):
    """Hourly temporal matching for one reporting period.

    The CFE score, the matched/unmatched split, hourly market-based emissions, hour
    coverage, and every reason a certificate did not count. A PARALLEL method: the
    annual location and market figures on the run are untouched.
    """
    from .services.hourly_scope2 import match
    result = match(db, org.id, reporting_period_id)
    if not include_hours:
        result.pop("hours", None)
    return JSONResponse(with_guidance(result))


@app.get("/assurance/evidence_pack")
def get_evidence_pack(run_id: Optional[int] = None, max_lines: int = 5000,
                      uncertainty_iterations: int = 10000,
                      org: Organisation = Depends(current_org),
                      db: Session = Depends(get_db)):
    """The assurance working-paper file for one run, assembled and hash-stamped.

    Inventory statement, reporting period, organisational boundary, transaction
    detail with full factor lineage, factor register, mapping decisions,
    completeness controls, data quality and uncertainty, methodology versions, and
    readiness with the applicable assurance standard — all from frozen run state,
    so the same run yields the same ``content_hash`` years later.

    ``evidence_gaps`` names, with reasons, what an ISAE 3410 / ISSA 5000 file
    expects that this platform cannot produce — reviewer identity, override
    before/after values, GL coding. A pack that omitted them would read as
    complete to the one person who most needs to know it is not.
    """
    from .services.evidence_pack import build_evidence_pack
    if not (1 <= max_lines <= 100000):
        raise HTTPException(status_code=400, detail="max_lines must be 1..100000")
    if not (1000 <= uncertainty_iterations <= 200000):
        raise HTTPException(status_code=400,
                            detail="uncertainty_iterations must be 1000..200000")
    if run_id is not None:
        run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                              CalculationRun.organisation_id == org.id).first()
    else:
        run = db.query(CalculationRun).filter(CalculationRun.organisation_id == org.id)\
            .order_by(CalculationRun.id.desc()).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found for this organisation")
    return JSONResponse(build_evidence_pack(
        db, run, max_lines=max_lines, uncertainty_iterations=uncertainty_iterations))


@app.get("/reports/uncertainty")
def get_uncertainty_report(run_id: Optional[int] = None,
                           method: str = "location",
                           correlation: str = DEFAULT_CORRELATION,
                           iterations: int = DEFAULT_ITERATIONS,
                           confidence: float = 0.95,
                           include_crosswalk: bool = False,
                           org: Organisation = Depends(current_org),
                           db: Session = Depends(get_db)):
    """Run-scoped alias of /runs/{run_id}/uncertainty.

    Same payload; `run_id` moves from the path to a query parameter so the
    registry-driven report UI can call it like every other report instead of
    special-casing one path shape.
    """
    from .services.uncertainty import propagate
    if run_id is not None:
        run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                              CalculationRun.organisation_id == org.id).first()
    else:
        run = db.query(CalculationRun).filter(CalculationRun.organisation_id == org.id)\
            .order_by(CalculationRun.id.desc()).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found for this organisation")
    result = propagate(db, run.id, method=method, correlation=correlation,
                       iterations=iterations, confidence=confidence,
                       include_crosswalk=include_crosswalk)
    if not result.get("available") and "run_id" not in result:
        raise HTTPException(status_code=400, detail=result.get("reason", "invalid request"))
    return JSONResponse(result)


@app.get("/reports/assurance_readiness")
def get_assurance_readiness(run_id: Optional[int] = None,
                            org: Organisation = Depends(current_org),
                            db: Session = Depends(get_db)):
    from .models import CalculationRun
    from .services.assurance import readiness_assessment
    if run_id is not None:
        run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                              CalculationRun.organisation_id == org.id).first()
    else:
        run = db.query(CalculationRun).filter(CalculationRun.organisation_id == org.id)\
            .order_by(CalculationRun.id.desc()).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found for this organisation")
    return JSONResponse(readiness_assessment(db, run))


@app.post("/targets")
def create_target(name: str = Query(...), target_type: str = Query(...),
                  base_run_id: int = Query(...), base_year: int = Query(...),
                  target_year: int = Query(...), target_reduction_pct: float = Query(...),
                  scope_coverage: str = "1+2", ambition: Optional[str] = None,
                  sbti_validated: bool = False,
                  org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import EmissionsTarget, CalculationRun
    from .services.calc import _utcnow_iso
    from .services.sbti import VALID_SCOPES
    if target_type not in ("near_term", "long_term", "net_zero"):
        raise HTTPException(status_code=400, detail="target_type must be near_term|long_term|net_zero")
    coverage_tokens = {s.strip() for s in (scope_coverage or "").split("+") if s.strip()}
    if not coverage_tokens or (coverage_tokens - VALID_SCOPES):
        raise HTTPException(status_code=400,
                            detail="scope_coverage must combine scopes 1/2/3 (e.g. '1+2', '1+2+3')")
    if not (0.0 <= target_reduction_pct <= 1.0):
        raise HTTPException(status_code=400, detail="target_reduction_pct must be in [0, 1]")
    if target_year <= base_year:
        raise HTTPException(status_code=400, detail="target_year must be after base_year")
    base = db.query(CalculationRun).filter(CalculationRun.id == base_run_id,
                                           CalculationRun.organisation_id == org.id).first()
    if base is None:
        raise HTTPException(status_code=404, detail="base_run_id not found for this organisation")
    t = EmissionsTarget(organisation_id=org.id, name=name, target_type=target_type,
                        scope_coverage=scope_coverage, base_run_id=base_run_id,
                        base_year=base_year, target_year=target_year,
                        target_reduction_pct=target_reduction_pct, ambition=ambition,
                        sbti_validated=sbti_validated, created_at=_utcnow_iso())
    db.add(t); db.commit(); db.refresh(t)
    return {"id": t.id, "name": t.name}


@app.get("/reports/sbti")
def get_sbti_report(target_id: int = Query(...), current_run_id: Optional[int] = None,
                    current_year: Optional[int] = None,
                    org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    return JSONResponse(with_guidance(sbti_report(db, org.id, target_id, current_run_id=current_run_id,
                                    current_year=current_year)))


@app.post("/credits")
def add_credit(registry: str = Query(...), quantity_tco2e: float = Query(...),
               credit_type: str = Query(...), project_id: Optional[str] = None,
               serial_number: Optional[str] = None, vintage_year: Optional[int] = None,
               ccp_approved: bool = False, vcmi_claim: Optional[str] = None,
               org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import CarbonCredit
    from .services.calc import _utcnow_iso
    if credit_type not in ("removal", "reduction", "avoidance"):
        raise HTTPException(status_code=400, detail="credit_type must be removal|reduction|avoidance")
    if not math.isfinite(quantity_tco2e) or quantity_tco2e <= 0:
        raise HTTPException(status_code=400, detail="quantity_tco2e must be a finite number > 0")
    from sqlalchemy.exc import IntegrityError
    c = CarbonCredit(organisation_id=org.id, registry=registry, project_id=project_id,
                     serial_number=serial_number, vintage_year=vintage_year,
                     quantity_tco2e=quantity_tco2e, credit_type=credit_type,
                     ccp_approved=ccp_approved, vcmi_claim=vcmi_claim,
                     created_at=_utcnow_iso())
    db.add(c)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409,
                            detail=f"a credit with registry {registry!r} serial "
                                   f"{serial_number!r} is already registered")
    db.refresh(c)
    return {"id": c.id, "registry": c.registry, "quantity_tco2e": c.quantity_tco2e}


@app.post("/credits/{credit_id}/retire")
def retire_credit(credit_id: int, run_id: int = Query(...),
                  org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import CarbonCredit, CalculationRun
    from .services.calc import _utcnow_iso
    c = db.query(CarbonCredit).filter(CarbonCredit.id == credit_id,
                                      CarbonCredit.organisation_id == org.id).first()
    if c is None:
        raise HTTPException(status_code=404, detail="credit not found for this organisation")
    run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                          CalculationRun.organisation_id == org.id).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run_id not found for this organisation")
    if c.retired:
        raise HTTPException(status_code=409, detail="credit already retired")
    c.retired = True
    c.retirement_date = _utcnow_iso()
    c.applied_to_run_id = run_id
    db.commit()
    return {"id": c.id, "retired": True, "applied_to_run_id": run_id}


@app.get("/reports/neutrality")
def get_neutrality_report(run_id: Optional[int] = None, basis: str = "location",
                          org: Organisation = Depends(current_org),
                          db: Session = Depends(get_db)):
    from .models import CalculationRun
    if basis not in ("location", "market"):
        raise HTTPException(status_code=400, detail="basis must be location|market")
    run = None
    if run_id is not None:
        run = db.query(CalculationRun).filter(CalculationRun.id == run_id,
                                              CalculationRun.organisation_id == org.id).first()
    else:
        run = db.query(CalculationRun).filter(CalculationRun.organisation_id == org.id)\
            .order_by(CalculationRun.id.desc()).first()
    if run is None:
        raise HTTPException(status_code=404, detail="run not found for this organisation")
    return JSONResponse(with_guidance(neutrality_assessment(db, org.id, run, basis=basis)))


@app.get("/reports/issb_s2")
def get_issb_s2_report(run_id: Optional[int] = None,
                       jurisdiction: str = "ISSB",
                       org: Organisation = Depends(current_org),
                       db: Session = Depends(get_db)):
    """IFRS S2 payload; jurisdiction: ISSB | UK_SRS | JP_SSBJ | SG_SGX | HK_HKEX."""
    return JSONResponse(with_guidance(issb_s2_report(db, org.id, run_id=run_id,
                                       jurisdiction=jurisdiction)))


@app.get("/reports/csddd")
def get_csddd_report(run_id: Optional[int] = None,
                     target_id: Optional[int] = None,
                     current_year: Optional[int] = None,
                     org: Organisation = Depends(current_org),
                     db: Session = Depends(get_db)):
    """CSDDD Article 22 climate transition-plan CARBON INPUTS readiness map.

    Honest scope: evidences only the GHG inventory (ISSB S2) and a science-based target
    (SBTi) that feed an Art 22 plan — NOT the transition plan, the due-diligence process,
    the human-rights / non-climate impact work, or CSDDD compliance. See report_scope /
    not_produced.
    """
    from .reports.csddd import csddd_report
    return JSONResponse(with_guidance(csddd_report(
        db, org.id, run_id=run_id, target_id=target_id, current_year=current_year)))


@app.get("/reports/tcfd")
def get_tcfd_report(run_id: Optional[int] = None,
                    jurisdiction_reference: Optional[str] = None,
                    org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
    """TCFD four-pillar cross-reference map over the ISSB S2 output.

    Honest scope: only Metrics & Targets (b) (gross Scope 1/2/3) is produced by the platform,
    sourced from ISSB S2; the other ten recommended disclosures are narrative the preparer
    supplies. Not a complete TCFD report — see the payload's report_scope.
    """
    from .reports.tcfd import tcfd_report
    return JSONResponse(with_guidance(tcfd_report(
        db, org.id, run_id=run_id, jurisdiction_reference=jurisdiction_reference)))


@app.get("/reports/gri")
def get_gri_report(run_id: Optional[int] = None,
                   base_run_id: Optional[int] = None,
                   intensity_denominator: Optional[float] = None,
                   intensity_denominator_unit: Optional[str] = None,
                   intensity_denominator_period_days: Optional[int] = None,
                   org: Organisation = Depends(current_org),
                   db: Session = Depends(get_db)):
    """GRI 305/302 content-index payload (305-5 needs base_run_id).

    `intensity_denominator_period_days` is required alongside a denominator: 305-4/302-3
    divide a period-scoped total by it, so a denominator covering a different span yields
    a ratio wrong by that ratio of spans.
    """
    if intensity_denominator is not None and (
            not math.isfinite(intensity_denominator) or intensity_denominator <= 0):
        raise HTTPException(status_code=400,
                            detail="intensity_denominator must be a finite number > 0")
    if intensity_denominator_period_days is not None and intensity_denominator_period_days <= 0:
        raise HTTPException(
            status_code=400,
            detail="intensity_denominator_period_days must be a positive number of days")
    return JSONResponse(with_guidance(gri_report(
        db, org.id, run_id=run_id, base_run_id=base_run_id,
        intensity_denominator=intensity_denominator,
        intensity_denominator_unit=intensity_denominator_unit,
        intensity_denominator_period_days=intensity_denominator_period_days)))


def _iso_14064_2_payload(db, organisation_id: int, *, baseline_run_id, project_run_id,
                         leakage_tco2e, baseline_justification, project_name) -> dict:
    """One renderer behind both the GET and the POST form, so the two cannot drift."""
    if leakage_tco2e is not None and not math.isfinite(leakage_tco2e):
        raise HTTPException(status_code=400, detail="leakage_tco2e must be a finite number")
    from .reports.iso_14064_2 import iso_14064_2_report
    return with_guidance(iso_14064_2_report(
        db, organisation_id, baseline_run_id=baseline_run_id,
        project_run_id=project_run_id, leakage_tco2e=leakage_tco2e,
        baseline_justification=baseline_justification, project_name=project_name))


@app.get("/reports/iso_14064_2")
def get_iso_14064_2_report(request: Request, baseline_run_id: Optional[int] = None,
                           project_run_id: Optional[int] = None,
                           leakage_tco2e: Optional[float] = None,
                           org: Organisation = Depends(current_org),
                           db: Session = Depends(get_db)):
    """ISO 14064-2 project GHG reduction = (baseline run - project run - leakage).

    Honest scope: the quantitative core a validator/verifier would check, not a verified
    assertion. A justified baseline and a quantified leakage are required; the reduction is
    a SEPARATE account from the corporate inventory (do not double-count). See the payload's
    double_counting_warning / requirements_not_produced.

    `baseline_justification` and `project_name` are narrative and have moved to the POST
    form of this same path, which returns an IDENTICAL payload. A GET cannot carry them
    in a body, and leaving them in the query string put a project's name and the prose
    justifying its baseline into the access log verbatim.
    """
    _reject_narrative_query_params(request, "baseline_justification", "project_name")
    return JSONResponse(_iso_14064_2_payload(
        db, org.id, baseline_run_id=baseline_run_id, project_run_id=project_run_id,
        leakage_tco2e=leakage_tco2e, baseline_justification=None, project_name=None))


@app.post("/reports/iso_14064_2")
def post_iso_14064_2_report(request: Request, baseline_run_id: Optional[int] = None,
                            project_run_id: Optional[int] = None,
                            leakage_tco2e: Optional[float] = None,
                            body: Optional[Iso140642Body] = None,
                            org: Organisation = Depends(current_org),
                            db: Session = Depends(get_db)):
    """The same report as GET /reports/iso_14064_2, with the narrative in the body.

    A POST that reads rather than writes, because the alternative was logging a baseline
    justification in a URL. The payload is byte-for-byte what the GET returns for the
    same inputs — one renderer serves both.

    The guard applies HERE TOO. Sending the narrative in this endpoint's query string
    put it straight back in the access log, and reading only the body meant the report
    was filed with the justification absent and a 200 saying it went fine.
    """
    _reject_narrative_query_params(request, "baseline_justification", "project_name")
    return JSONResponse(_iso_14064_2_payload(
        db, org.id, baseline_run_id=baseline_run_id, project_run_id=project_run_id,
        leakage_tco2e=leakage_tco2e,
        baseline_justification=(body.baseline_justification if body else None),
        project_name=(body.project_name if body else None)))


@app.get("/reports/cdp")
def get_cdp_export(run_id: Optional[int] = None,
                   intensity_denominator: Optional[float] = None,
                   intensity_denominator_unit: Optional[str] = None,
                   intensity_denominator_period_days: Optional[int] = None,
                   verification_status: str = "no_third_party_verification",
                   org: Organisation = Depends(current_org),
                   db: Session = Depends(get_db)):
    """CDP Climate questionnaire export (classic C-codes, labelled)."""
    if intensity_denominator is not None and (
            not math.isfinite(intensity_denominator) or intensity_denominator <= 0):
        raise HTTPException(status_code=400,
                            detail="intensity_denominator must be a finite number > 0")
    return JSONResponse(with_guidance(cdp_export(db, org.id, run_id=run_id,
                                   intensity_denominator=intensity_denominator,
                                   intensity_denominator_unit=intensity_denominator_unit,
                                   intensity_denominator_period_days=intensity_denominator_period_days,
                                   verification_status=verification_status)))


@app.get("/reports/esrs_e1")
def get_esrs_e1_report(run_id: Optional[int] = None,
                       net_revenue_millions: Optional[float] = None,
                       revenue_period_days: Optional[int] = None,
                       revenue_currency: str = "EUR",
                       credits_as_of: Optional[str] = None,
                       org: Organisation = Depends(current_org),
                       db: Session = Depends(get_db)):
    """CSRD ESRS E1 quantitative disclosure payload with pre-submission gates.

    credits_as_of (ISO timestamp) freezes the E1-7 credits section for a filing.
    """
    if net_revenue_millions is not None and (
            not math.isfinite(net_revenue_millions) or net_revenue_millions <= 0):
        raise HTTPException(status_code=400,
                            detail="net_revenue_millions must be a finite number > 0")
    return JSONResponse(with_guidance(esrs_e1_report(db, org.id, run_id=run_id,
                                       net_revenue_millions=net_revenue_millions,
                                       revenue_period_days=revenue_period_days,
                                       revenue_currency=revenue_currency,
                                       credits_as_of=credits_as_of)))


@app.get("/reports/sb253")
def get_sb253_report(request: Request, run_id: Optional[int] = None,
                     assurance_level: str = "none",
                     org: Organisation = Depends(current_org),
                     db: Session = Depends(get_db)):
    """California SB 253 (CCDAA) filing payload with pre-submission gates.

    `assurance_provider` names a firm and has moved to the POST form of this same path,
    which returns an IDENTICAL payload.
    """
    _reject_narrative_query_params(request, "assurance_provider")
    return JSONResponse(with_guidance(sb253_report(db, org.id, run_id=run_id,
                                     assurance_level=assurance_level,
                                     assurance_provider=None)))


@app.post("/reports/sb253")
def post_sb253_report(request: Request, run_id: Optional[int] = None,
                      assurance_level: str = "none",
                      body: Optional[Sb253Body] = None,
                      org: Organisation = Depends(current_org),
                      db: Session = Depends(get_db)):
    """The same report as GET /reports/sb253, with the assuror named in the body.

    Guarded like the GET: a provider sent in this endpoint's query string was logged AND
    dropped from the filing, which is the worse half of the defect, not the better one.
    """
    _reject_narrative_query_params(request, "assurance_provider")
    return JSONResponse(with_guidance(sb253_report(
        db, org.id, run_id=run_id, assurance_level=assurance_level,
        assurance_provider=(body.assurance_provider if body else None))))


@app.get("/compliance/{framework_key}")
def get_compliance(framework_key: str, request: Request,
                   org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """Required-data checklist for one framework: which mandatory fields this report carries,
    which are missing, and — where the standard sets a number — by how much you fall short.

    Accepts the same query params as the report itself. This is a DATA-completeness check,
    not a compliance opinion: preparer narrative and independent assurance are listed as
    outstanding, never assumed.
    """
    from .reports.export import build_report, BUILDERS
    from .reports.compliance import evaluate
    if framework_key not in BUILDERS:
        raise HTTPException(status_code=404, detail=f"unknown report {framework_key!r}")
    params = {k: v for k, v in request.query_params.items()}
    try:
        payload = build_report(db, org.id, framework_key, params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(evaluate(framework_key, payload))


@app.get("/export/{framework_key}")
def export_report(framework_key: str, request: Request, format: str = "csv",
                  org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    """Download any framework report as CSV or PDF.

    The report is REGENERATED server-side from the immutable run using the same renderer as
    the JSON endpoint — an exported document can never contain figures the engine would not
    itself produce. Every query param the JSON endpoint accepts is accepted here too.

    The PDF is a disclosure DOCUMENT (the framework's fields laid out for a reader), not a
    certification: it carries the same fail-closed verdict, is stamped DRAFT when the report
    is not disclosure-ready, and states that it is unverified.
    """
    from .reports.export import build_with_compliance, to_csv, to_pdf, BUILDERS
    from .services.calc import _utcnow_iso

    fmt = (format or "csv").lower()
    if fmt not in ("csv", "pdf"):
        raise HTTPException(status_code=400, detail="format must be csv or pdf")
    if framework_key not in BUILDERS:
        raise HTTPException(status_code=404,
                            detail=f"unknown report {framework_key!r}; "
                                   f"one of {sorted(BUILDERS)}")
    params = {k: v for k, v in request.query_params.items() if k != "format"}
    try:
        payload = build_with_compliance(db, org.id, framework_key, params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    label = str(payload.get("framework") or framework_key)
    stamp = _utcnow_iso()
    fname = f"{framework_key}_{stamp[:10]}"
    if fmt == "csv":
        return PlainTextResponse(
            to_csv(payload), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{fname}.csv"'})
    try:
        pdf = to_pdf(payload, framework_label=label, organisation=org.name, generated_at=stamp)
    except ImportError:
        raise HTTPException(status_code=503,
                            detail="PDF export unavailable: reportlab is not installed")
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{fname}.pdf"'})


@app.get("/reports/secr")
def get_secr_report(run_id: Optional[int] = None,
                    intensity_denominator: Optional[float] = None,
                    intensity_denominator_unit: Optional[str] = None,
                    intensity_denominator_period_days: Optional[int] = None,
                    org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
    """UK SECR disclosure payload with pre-submission validation gates."""
    if intensity_denominator is not None and (
            not math.isfinite(intensity_denominator) or intensity_denominator <= 0):
        raise HTTPException(status_code=400, detail="intensity_denominator must be a finite number > 0")
    return JSONResponse(with_guidance(secr_report(
        db, org.id, run_id=run_id,
        intensity_denominator=intensity_denominator,
        intensity_denominator_unit=intensity_denominator_unit,
        intensity_denominator_period_days=intensity_denominator_period_days)))


@app.post("/taxonomy/activities")
def add_taxonomy_activity(name: str = Query(...), reporting_year: int = Query(...),
                          turnover: float = 0.0, capex: float = 0.0, opex: float = 0.0,
                          eligible: bool = False, substantial_contribution: bool = False,
                          dnsh_pass: bool = False, minimum_safeguards_pass: bool = False,
                          objective: Optional[str] = None,
                          org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .models import TaxonomyActivity
    from .services.calc import _utcnow_iso
    for nm, v in (("turnover", turnover), ("capex", capex), ("opex", opex)):
        if not math.isfinite(v) or v < 0:
            raise HTTPException(status_code=400, detail=f"{nm} must be a finite number >= 0")
    a = TaxonomyActivity(organisation_id=org.id, name=name, reporting_year=reporting_year,
                         turnover=turnover, capex=capex, opex=opex, eligible=eligible,
                         substantial_contribution=substantial_contribution,
                         dnsh_pass=dnsh_pass, minimum_safeguards_pass=minimum_safeguards_pass,
                         objective=objective, created_at=_utcnow_iso())
    db.add(a); db.commit(); db.refresh(a)
    return {"id": a.id, "name": a.name, "reporting_year": a.reporting_year}


@app.get("/reports/eu_taxonomy")
def get_taxonomy_report(reporting_year: int = Query(...),
                        org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .reports.compliance_extra import taxonomy_report
    return JSONResponse(with_guidance(taxonomy_report(db, org.id, reporting_year)))


@app.get("/reports/ets_mrv")
def get_ets_mrv_report(scheme: str = "EU ETS", run_id: Optional[int] = None,
                       verified: bool = False,
                       org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .reports.compliance_extra import ets_mrv_report
    if scheme not in ("EU ETS", "UK ETS"):
        raise HTTPException(status_code=400, detail="scheme must be 'EU ETS' or 'UK ETS'")
    return JSONResponse(with_guidance(ets_mrv_report(db, org.id, scheme, run_id=run_id,
                                                     verified=verified)))


@app.get("/reports/esos")
def get_esos_report(run_id: Optional[int] = None,
                    org: Organisation = Depends(current_org), db: Session = Depends(get_db)):
    from .reports.compliance_extra import esos_report
    return JSONResponse(with_guidance(esos_report(db, org.id, run_id=run_id)))


@app.get("/frameworks")
def get_frameworks(category: Optional[str] = None):
    """List every framework/standard the platform touches, with support status.
    Public reference data — no authentication."""
    items = list_frameworks()
    if category:
        items = [f for f in items if f["category"].lower() == category.lower()]
    return items


@app.get("/frameworks/{key}")
def get_framework_guidance(key: str):
    """Full guidance for one framework/standard."""
    g = FRAMEWORKS.get(key)
    if g is None:
        raise HTTPException(status_code=404, detail=f"unknown framework {key!r}")
    return {"key": key, **g}


@app.get("/factors")
def list_factors(request: Request, org: Organisation = Depends(current_org),
                 db: Session = Depends(get_db), category: Optional[str] = None,
                 geo: Optional[str] = None, q: Optional[str] = None,
                 limit: Optional[int] = None, offset: Optional[int] = None):
    """The factor catalogue this tenant may see — searchable and paginated.

    Authenticated, and scoped: this was the only route in the application that returned
    rows from a customer-populated table with no credential, which republished both
    operator-licensed catalogues and suppliers' confidential PACT footprints to anonymous
    callers. `factor_visible_to` is the ONE definition of that rule and is applied here.

    SHAPE CHANGE: was a bare JSON array capped at 200 with no search. The mapping
    override dropdown passes no filter, so only the first 200 catalogue rows were ever
    reachable from it and the rest could not be chosen at all. `q` matches category,
    subcategory, source and geography case-insensitively.
    """
    from .services.pagination import next_link_headers, page_block, page_of_query
    limit, offset = _page_args(limit, offset)
    query = db.query(EmissionFactor).filter(factor_visible_to(org.id))
    if category:
        query = query.filter(EmissionFactor.category == category)
    if geo:
        query = query.filter(EmissionFactor.geography == geo)
    if q and q.strip():
        from sqlalchemy import func as _func, or_ as _or
        needle = f"%{q.strip().lower()}%"
        query = query.filter(_or(
            _func.lower(EmissionFactor.category).like(needle),
            _func.lower(EmissionFactor.subcategory).like(needle),
            _func.lower(EmissionFactor.source).like(needle),
            _func.lower(EmissionFactor.geography).like(needle)))
    page = page_of_query(query.order_by(EmissionFactor.id), limit, offset)
    body = {"factors": [{"id": f.id, "src": f.source, "ver": f.version,
                         "geo": f.geography, "year": f.year, "cat": f.category,
                         "subcat": f.subcategory, "unit": f.unit, "gwp": f.gwp_set,
                         "value": f.value} for f in page["rows"]],
            "page": page_block(page, limit, offset)}
    return JSONResponse(body, headers=next_link_headers(request, page))


# =========================================================================================
# DATA LIFECYCLE: read-back, correction, erasure, retention, portability, tenant deletion
# =========================================================================================
# The platform had 125 operations and ZERO DELETE/PUT/PATCH: nothing deleted a tenant or a
# record anywhere, and there was no list endpoint for 20+ object types, so an onboarding
# typo was permanent and invisible and a GDPR Art. 28(3)(g) deletion request could not be
# honoured by API or by DBA.
#
# The deletion POLICY — when a record is hard-deleted, when its personal data is redacted
# instead, and when deletion is refused outright — lives in services/lifecycle.py together
# with the evidence for it. Read that module docstring before changing anything here; the
# short version is that a filed calculation run freezes a figure but not the free text, so
# the text can go while the row stays and the run keeps reproducing byte-identically.
#
# Everything here is organisation-scoped through `current_org`, and the list endpoints get
# their scoping from ONE factory below rather than each handler remembering it.

from .services import lifecycle as _lifecycle
from .services.calc import _utcnow_iso as _lc_now

LIST_PAGE_DEFAULT = 50
LIST_PAGE_MAX = 500


def _list_page(db: Session, org: Organisation, model, limit: int, offset: int,
               order_desc: bool = True) -> dict:
    """One page of the CALLING org's rows of `model`.

    The organisation filter is applied HERE, not by the caller. The review's worst
    finding class was handlers that resolve the caller's organisation and then never use
    it, so no list endpoint below is given the chance to forget: it names a model and
    gets a scoped page back.

    limit/offset with has_more/next_offset, matching pact_list_footprints — the one good
    pagination example already in the codebase — so every paginated endpoint in the
    platform answers "is there more?" identically.
    """
    if limit < 1 or limit > LIST_PAGE_MAX:
        raise HTTPException(status_code=400,
                            detail=f"limit must be 1..{LIST_PAGE_MAX}")
    if offset < 0:
        raise HTTPException(status_code=400, detail="offset must be >= 0")
    q = db.query(model).filter(model.organisation_id == org.id)
    q = q.order_by(model.id.desc() if order_desc else model.id)
    page = _lifecycle.paginate(q, limit, offset)
    cols = model.__table__.columns
    return {
        "items": [_lifecycle.safe_row({c.name: getattr(r, c.name) for c in cols})
                  for r in page["rows"]],
        "total_matched": page["total_matched"],
        "limit": limit, "offset": offset,
        "has_more": page["has_more"],
        "next_offset": page["next_offset"] if page["has_more"] else None,
    }


# --- read-back ---------------------------------------------------------------------------
# One GET per tenant-owned object type that had a POST but no way to read it back. Without
# these a preparer could create a record and never see it again, so a typo in an uploaded
# inventory was uncorrectable and invisible. Registered from a table rather than written
# out 19 times, so every one is identically scoped, paginated and credential-filtered.
#
# Each entry is (route path, model, operation id). Models are imported lazily inside the
# factory call so this block stays appendable without touching the import header above.

def _register_list_endpoints():
    from .models import (
        ActivityRecord as _Act, AssuranceEngagement as _AsEng, CarbonCredit as _Cred,
        CbamGood as _Cbam, DeliverabilityLink as _Deliv, EmissionsTarget as _Tgt,
        FinancedPosition as _Fin, GranularCertificate as _Gc, HourlyLoad as _Hl,
        LcaAssessment as _Lca, MarketInstrument as _Mi, NatureSite as _NSite,
        NatureTarget as _NTgt, PactClient as _PClient, RemovalRecord as _Rem,
        ReportingEntity as _Ent, ReportingPeriod as _Per,
        Scope3CategoryDeclaration as _S3Decl, TaxonomyActivity as _Tax,
    )
    specs = (
        ("/activities", _Act, "list_activities"),
        ("/reporting_periods", _Per, "list_reporting_periods"),
        ("/entities", _Ent, "list_reporting_entities"),
        ("/market_instruments", _Mi, "list_market_instruments"),
        ("/lca/assessments", _Lca, "list_lca_assessments"),
        ("/finance/positions", _Fin, "list_financed_positions"),
        ("/removals", _Rem, "list_removal_records"),
        ("/scope3/declarations", _S3Decl, "list_scope3_declarations"),
        ("/nature/sites", _NSite, "list_nature_sites"),
        ("/nature/targets", _NTgt, "list_nature_targets"),
        ("/assurance/engagements", _AsEng, "list_assurance_engagements"),
        ("/pact/clients", _PClient, "list_pact_clients"),
        ("/hourly/certificates", _Gc, "list_granular_certificates"),
        ("/hourly/loads", _Hl, "list_hourly_loads"),
        ("/hourly/deliverability_links", _Deliv, "list_deliverability_links"),
        ("/targets", _Tgt, "list_emissions_targets"),
        ("/credits", _Cred, "list_carbon_credits"),
        ("/taxonomy/activities", _Tax, "list_taxonomy_activities"),
        ("/cbam/goods", _Cbam, "list_cbam_goods"),
    )

    def _make(model):
        def handler(limit: int = Query(LIST_PAGE_DEFAULT), offset: int = Query(0),
                    org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
            return _list_page(db, org, model, limit, offset)
        return handler

    for path, model, name in specs:
        app.add_api_route(
            path, _make(model), methods=["GET"], name=name,
            summary=f"List the calling organisation's {model.__tablename__}",
            description=(
                "Paginated with limit/offset; `has_more` and `next_offset` say whether "
                "more remain. Scoped to the calling organisation. Credential columns "
                "are never returned."),
        )


_register_list_endpoints()


# --- erasure ----------------------------------------------------------------------------

@app.delete("/activities/{activity_id}")
def delete_activity(activity_id: int,
                    mode: str = Query("auto", pattern="^(auto|hard|redact)$"),
                    reason: str = Query("erasure_request"),
                    org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
    """Erase one activity. The first DELETE this API has ever had.

    `mode=auto` (default) hard-deletes the row when NO calculation run froze it, and
    redacts its personal free text when one did. `mode=hard` refuses with 409 rather
    than redacting, and the refusal NAMES the runs that block it. `mode=redact` always
    redacts.

    Why a frozen activity is redacted rather than deleted: a filed run's figures come
    from the frozen line items, but the SECR/ESOS energy total and the evidence pack's
    transaction detail INNER JOIN the live activities table, so deleting the row makes
    those lines silently vanish from a disclosure that was already filed. Redaction
    clears only `description` and `source_file` — neither of which is in
    activities_fingerprint() — so every filed run stays bit-identical. The full argument
    is in services/lifecycle.py.
    """
    if reason not in _lifecycle.REDACTION_REASONS:
        raise HTTPException(
            status_code=400,
            detail="reason must be one of " + ", ".join(_lifecycle.REDACTION_REASONS))
    out = _lifecycle.erase_activity(db, org.id, activity_id,
                                    mode=mode, reason=reason, now=_lc_now())
    if out["action"] == "not_found":
        raise HTTPException(status_code=404,
                            detail="activity not found for this organisation")
    if out["action"] == "blocked":
        # 409, and the body names every blocking run. A bare refusal would leave the
        # caller unable to find out which filed disclosure they would have broken.
        raise HTTPException(status_code=409, detail=out)
    return out


# --- retention --------------------------------------------------------------------------

@app.post("/organisations/retention")
def set_retention(retention_days: Optional[int] = Query(None),
                  clear: bool = Query(False),
                  org: Organisation = Depends(current_org),
                  db: Session = Depends(get_db)):
    """Set (or clear) this organisation's activity retention period, in days.

    NULL means keep indefinitely and is the only default — a processor must not invent a
    retention period on the controller's behalf. Setting it does NOT delete anything:
    there is no scheduler, and retention is applied only when a caller explicitly runs
    POST /retention/apply after inspecting POST /retention/preview.
    """
    if clear:
        org.retention_days = None
    else:
        if retention_days is None:
            raise HTTPException(
                status_code=400,
                detail="pass retention_days, or clear=true to keep data indefinitely")
        if retention_days < 1:
            raise HTTPException(status_code=400, detail="retention_days must be >= 1")
        org.retention_days = retention_days
    db.commit()
    return {"organisation_id": org.id, "retention_days": org.retention_days,
            "note": ("NULL means keep indefinitely. Nothing is deleted until "
                     "POST /retention/apply is called explicitly.")}


@app.post("/retention/preview")
def preview_retention(as_of: Optional[str] = Query(None),
                      retention_days: Optional[int] = Query(None),
                      org: Organisation = Depends(current_org),
                      db: Session = Depends(get_db)):
    """DRY RUN: exactly what applying retention would do, per outcome. Changes nothing.

    Reported as three separate populations rather than one total, because "we would
    touch 400 rows" hides that most of them can only be redacted. Rows with no usable
    date are reported as skipped, never silently deleted — nothing shows them to be
    outside the window, and that is cannot_determine, not yes.
    """
    days = retention_days if retention_days is not None else org.retention_days
    if days is None:
        raise HTTPException(
            status_code=400,
            detail=("no retention period set for this organisation; POST "
                    "/organisations/retention first, or pass retention_days to preview "
                    "a hypothetical one"))
    if days < 1:
        raise HTTPException(status_code=400, detail="retention_days must be >= 1")
    try:
        return _lifecycle.retention_candidates(db, org.id, days,
                                               as_of or _lc_now()[:10])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/retention/apply")
def apply_retention(confirm: bool = Query(False),
                    as_of: Optional[str] = Query(None),
                    org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
    """Apply this organisation's retention period. Requires confirm=true.

    Hard-deletes only activities no run froze; redacts the rest. Recomputes the candidate
    set rather than trusting a preview the client may have taken minutes ago.
    """
    if org.retention_days is None:
        raise HTTPException(
            status_code=400,
            detail=("no retention period set for this organisation; nothing to apply. "
                    "POST /organisations/retention first."))
    if not confirm:
        raise HTTPException(
            status_code=400,
            detail=("retention deletes data irreversibly — call POST /retention/preview "
                    "first, then repeat this call with confirm=true"))
    today = _lc_now()[:10]
    effective_as_of = as_of or today
    # `as_of` is arithmetically interchangeable with retention_days — the cutoff is
    # as_of minus the period — so a FUTURE as_of turns any retention setting into
    # "delete everything": 100-year retention evaluated as_of 2999 erases the whole
    # inventory. One query parameter must not be able to override the period the
    # organisation deliberately configured.
    #
    # A PAST as_of is safe and left allowed: it moves the cutoff earlier, so it can only
    # select FEWER rows than today would, and it is what makes the tests deterministic.
    # The preview is unbounded on purpose — it changes nothing, and asking "what would
    # age out next year?" is a reasonable question. Apply recomputes its own window
    # regardless, so a future-dated preview cannot be replayed into a deletion.
    if effective_as_of > today:
        raise HTTPException(
            status_code=400,
            detail=(f"as_of {effective_as_of} is in the future, which would widen the "
                    f"retention window past the configured "
                    f"{org.retention_days} days and delete more than this "
                    f"organisation's policy allows; omit it, or pass a date on or "
                    f"before {today}"))
    try:
        return _lifecycle.retention_apply(db, org.id, org.retention_days,
                                          effective_as_of, _lc_now())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# --- portability ------------------------------------------------------------------------

@app.get("/organisations/export")
def export_own_data(org: Organisation = Depends(current_org),
                    db: Session = Depends(get_db)):
    """Everything this organisation owns, as machine-readable JSON.

    The Art. 20 portability answer and the Art. 15 subject-access substrate: a data
    subject's rows are in `activities` and the site/commuting lines referencing them, so
    an Art. 15 request is served by filtering this payload rather than a second
    half-built export path.

    Rows are emitted column-for-column from the table metadata, so the export cannot
    drift as the schema grows. Credential columns are never included, and what is left
    out is stated in the payload rather than left for the reader to notice.
    """
    return _lifecycle.export_tenant(db, org.id, _lc_now())


# --- tenant deletion --------------------------------------------------------------------

@app.delete("/organisations/self")
def delete_own_organisation(confirm_org_name: str = Query(...),
                            confirm_irreversible: bool = Query(False),
                            org: Organisation = Depends(current_org),
                            db: Session = Depends(get_db)):
    """Delete this organisation and EVERYTHING belonging to it. IRREVERSIBLE.

    Two confirmations, matching the existing /organisations/revoke_key precedent: the
    org's own name, and an explicit acknowledgement that this cannot be undone. Both are
    logged with the row counts before deletion.

    Authenticated by the tenant's OWN key rather than the platform admin key on purpose.
    Art. 28(3)(g) is an obligation owed TO the controller, and require_admin returns 503
    when ADMIN_API_KEY is unset (the default) — gating erasure behind it would make the
    customer's own right unexercisable on most deployments.

    FK-safe by construction: the order is derived from the table metadata (children
    first), not hand-maintained. Verified rather than trusted — every org-scoped table is
    re-counted afterwards and `verified_no_orphans` reports the result, so a caller is
    never told "deleted" over rows that survived.
    """
    if confirm_org_name != org.name:
        raise HTTPException(status_code=400, detail="confirm_org_name does not match")
    if not confirm_irreversible:
        raise HTTPException(
            status_code=400,
            detail=("this deletes every record belonging to this organisation and cannot "
                    "be undone — GET /organisations/export first if you want a copy, "
                    "then repeat with confirm_irreversible=true"))
    org_id, org_name = org.id, org.name
    out = _lifecycle.delete_tenant(db, org_id, org_name, _lc_now())
    if not out["verified_no_orphans"]:
        # The scan found rows the deletion did not account for. Report it as a server
        # error with the evidence: claiming success here is exactly the fail-open
        # verdict this codebase treats as a defect.
        raise HTTPException(status_code=500, detail={
            "message": ("tenant deletion left rows behind — this is a bug; the "
                        "remaining tables are listed"),
            **out})
    return out


# --- Single-service frontend (optional) ---------------------------------------------------
# When a built frontend exists (frontend/dist, produced by `npm run build`), serve it from
# the SAME origin as the API so one container/host is a complete demo. This mount is added
# LAST, so every API route above matches first; it is the catch-all for the SPA and its
# assets. Guarded by existence, so dev and the test suite (no build present) are unaffected.
_frontend_dist = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), "frontend", "dist")
if _os.path.isdir(_frontend_dist):
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=_frontend_dist, html=True), name="frontend")
