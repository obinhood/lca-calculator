"""Serving the PACT v3 API — filtering, pagination, auth and CloudEvents.

The host side of the exchange, built against the Technical Specifications v3.0.3
AND against the behaviour of the official conformance runner, which differs from
the prose in several places that matter. Where they disagree the runner wins,
because conformance is what the network actually checks — and since April 2025
automated conformance is the only path: peer-to-peer testing was retired and V2
conformance expires 2026-04-01.

FOUR PLACES THE PROSE WILL MISLEAD YOU

1. THE TOKEN ENDPOINT IS NOT UNDER /3. Everything else is {base}/3/..., but the
   token lives at {auth-base}/auth/token. The runner defaults auth-base to base,
   so routing the token under /3 fails the first test case and aborts the run.

2. CREDENTIALS ARRIVE AS HTTP BASIC. The prose implies form fields named
   client_id and client_secret; the spec's own example and the runner send
   Authorization: Basic base64(id:secret) with only grant_type in the body. A host
   that reads the body alone fails immediately. Both are accepted here.

3. THE LINK HEADER MUST CARRY rel="next" AND NOTHING ELSE. The runner takes
   Object.values(parseLinkHeader(...))[0] — the FIRST link, not the one whose rel
   is "next" — so emitting the spec's own first/prev/next/last example makes it
   follow rel="first". Its parser is also brittle: the rel value must be
   double-quoted, and extra RFC 8288 parameters get swallowed by a greedy capture.

4. A FULFILLED EVENT WITH NO FOOTPRINTS IS INVALID. `pfs` has minItems 1, so
   "success with zero results" is non-conformant — send RequestRejectedEvent
   instead. And `requestEventId` must be echoed BYTE-FOR-BYTE: the runner encodes
   routing data into the id and recovers it by splitting on '/', so normalising it
   or minting a fresh UUID leaves the test hanging in PENDING rather than failing
   loudly.

One more asymmetry worth stating: `status` is a SCALAR enum on the ListFootprints
query and an ARRAY on RequestCreatedEvent. Sharing one parser between the sync
filter and the async one breaks on exactly that field.
"""
import base64
import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlencode

from sqlalchemy.orm import Session

from ..models import PactClient, PactToken, ProductFootprint

PACT_API_VERSION = "3"
TOKEN_TTL_SECONDS = 3600
# The spec floor. A pagination link must stay valid at least this long AND be
# replayable — a one-shot cursor is non-conformant.
PAGINATION_LINK_MIN_VALIDITY_SECONDS = 180
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000

# The closed Error.code enum. Anything outside it is not conformant.
ERROR_CODES = ("BadRequest", "AccessDenied", "TokenExpired", "NotFound",
               "InternalError", "NotImplemented")

EVENT_TYPES = {
    "request_created": "org.wbcsd.pact.ProductFootprint.RequestCreatedEvent.3",
    "request_fulfilled": "org.wbcsd.pact.ProductFootprint.RequestFulfilledEvent.3",
    "request_rejected": "org.wbcsd.pact.ProductFootprint.RequestRejectedEvent.3",
    "published": "org.wbcsd.pact.ProductFootprint.PublishedEvent.3",
}
CLOUDEVENTS_MEDIA_TYPE = "application/cloudevents+json"
BASE_EVENT_REQUIRED = ("type", "specversion", "id", "source", "time", "data")

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def error(code: str, message: str) -> dict:
    """The Error object served on 400/403/404/500."""
    if code not in ERROR_CODES:
        code = "InternalError"
    return {"code": code, "message": message}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.utcnow()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# --- auth --------------------------------------------------------------------

def create_client(db: Session, organisation_id: int,
                  partner_name: Optional[str] = None) -> dict:
    """Mint a client_id/client_secret pair for one data recipient.

    The secret is returned once and stored only as a hash, like the tenant API key.
    """
    from .calc import _utcnow_iso
    client_id = f"pact_{secrets.token_urlsafe(12)}"
    client_secret = secrets.token_urlsafe(32)
    db.add(PactClient(organisation_id=organisation_id, client_id=client_id,
                      client_secret_hash=_hash(client_secret),
                      partner_name=partner_name, created_at=_utcnow_iso()))
    db.commit()
    return {"client_id": client_id, "client_secret": client_secret,
            "partner_name": partner_name,
            "note": "The secret is shown once and stored only as a hash."}


def parse_basic_auth(header: Optional[str]) -> Optional[tuple]:
    """(client_id, client_secret) from an HTTP Basic header, or None.

    The runner sends credentials ONLY this way, despite the prose implying form
    fields.
    """
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header.split(None, 1)[1]).decode("utf-8")
    except (ValueError, IndexError, UnicodeDecodeError):
        return None
    if ":" not in raw:
        return None
    cid, secret = raw.split(":", 1)
    return cid, secret


def issue_token(db: Session, client_id: str, client_secret: str) -> dict:
    """The client-credentials grant."""
    from .calc import _utcnow_iso
    row = db.query(PactClient).filter(PactClient.client_id == client_id).first()
    if row is None or row.revoked or row.client_secret_hash != _hash(client_secret):
        return {"ok": False, "error": "invalid_client",
                "error_description": "client authentication failed"}
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(seconds=TOKEN_TTL_SECONDS)
    db.add(PactToken(client_id=client_id, organisation_id=row.organisation_id,
                     token_hash=_hash(token), expires_at=_iso(expires),
                     created_at=_utcnow_iso()))
    db.commit()
    return {"ok": True, "access_token": token, "token_type": "Bearer",
            "expires_in": TOKEN_TTL_SECONDS}


def resolve_token(db: Session, authorization: Optional[str]) -> dict:
    """Resolve a bearer token to the organisation whose footprints it may read."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return {"ok": False, "code": "BadRequest",
                "message": "a Bearer token is required"}
    token = authorization.split(None, 1)[1].strip()
    row = db.query(PactToken).filter(PactToken.token_hash == _hash(token)).first()
    if row is None:
        return {"ok": False, "code": "BadRequest", "message": "invalid access token"}
    if row.expires_at <= _iso(_now()):
        return {"ok": False, "code": "TokenExpired", "message": "access token expired"}
    return {"ok": True, "organisation_id": row.organisation_id,
            "client_id": row.client_id}


# --- filtering ---------------------------------------------------------------

def _lower_set(values) -> set:
    return {str(v).strip().lower() for v in (values or []) if str(v).strip()}


def _row_geographies(row: ProductFootprint) -> set:
    """`geography` matches ANY of the three levels, not just the one that is set."""
    return _lower_set([row.geography_value] if row.geography_value else [])


def matches(row: ProductFootprint, *, product_ids=None, company_ids=None,
            geographies=None, classifications=None, status=None,
            valid_on=None, valid_after=None, valid_before=None) -> bool:
    """OR within a criterion, AND between criteria. Values compare case-insensitively."""
    if status and (row.status or "").strip().lower() != status.strip().lower():
        return False
    if product_ids and not (_lower_set(json.loads(row.product_ids or "[]"))
                            & _lower_set(product_ids)):
        return False
    if company_ids and not (_lower_set(json.loads(row.company_ids or "[]"))
                            & _lower_set(company_ids)):
        return False
    if geographies and not (_row_geographies(row) & _lower_set(geographies)):
        return False
    if classifications:
        # productClassifications is optional; treat absence as no match rather than
        # a silent pass, or a filter would widen instead of narrowing.
        try:
            doc = json.loads(row.document)
        except (ValueError, TypeError):
            doc = {}
        have = _lower_set(doc.get("productClassifications") or [])
        if not (have & _lower_set(classifications)):
            return False

    start = row.validity_period_start or row.reference_period_start
    end = row.validity_period_end or row.reference_period_end
    if valid_on and not (_lte(start, valid_on) and _lte(valid_on, end)):
        return False
    if valid_after and not _lte(valid_after, start):
        return False
    if valid_before and not _lte(end, valid_before):
        return False
    return True


def _lte(a: Optional[str], b: Optional[str]) -> bool:
    """a <= b on ISO strings; an absent bound never excludes."""
    if not a or not b:
        return True
    return a[:19] <= b[:19]


def list_footprints(db: Session, organisation_id: int, *, limit: int = DEFAULT_LIMIT,
                    offset: int = 0, **filters) -> dict:
    """One page of published footprints, plus whether more remain.

    Deprecated footprints are INCLUDED by default and excluded only when
    status=Active is explicitly asked for — filtering them out silently would hide
    the supersession chain the caller may be trying to follow.
    """
    rows = db.query(ProductFootprint).filter(
        ProductFootprint.organisation_id == organisation_id,
        ProductFootprint.direction == "published").order_by(
        ProductFootprint.id).all()
    hits = [r for r in rows if matches(r, **filters)]
    page = hits[offset:offset + limit]
    return {"rows": page, "total_matched": len(hits),
            "has_more": (offset + limit) < len(hits),
            "next_offset": offset + limit}


def link_header(base_url: str, path: str, params: dict, next_offset: int) -> str:
    """RFC 8288 next link, and ONLY next.

    The runner takes the FIRST parsed link rather than the one whose rel is "next",
    so a preceding rel="first" would be followed instead. The rel value must be
    double-quoted, and no extra parameters may follow it — the parser's greedy
    capture swallows them.
    """
    q = dict(params)
    q["offset"] = next_offset
    return f'<{base_url}{path}?{urlencode(q, doseq=True)}>; rel="next"'


# --- events ------------------------------------------------------------------

def is_cloudevents_media_type(content_type: Optional[str]) -> bool:
    """Parse the media type rather than comparing strings — the runner appends
    `; charset=UTF-8`."""
    if not content_type:
        return False
    return content_type.split(";")[0].strip().lower() == CLOUDEVENTS_MEDIA_TYPE


def validate_event(body) -> dict:
    """Validate an inbound CloudEvent against the base envelope and its type."""
    if not isinstance(body, dict):
        return {"valid": False, "code": "BadRequest",
                "message": "event must be a JSON object in structured content mode"}
    missing = [f for f in BASE_EVENT_REQUIRED if body.get(f) in (None, "")]
    if missing:
        return {"valid": False, "code": "BadRequest",
                "message": f"CloudEvent is missing required field(s): {missing}"}
    etype = body.get("type")
    if etype not in EVENT_TYPES.values():
        return {"valid": False, "code": "BadRequest",
                "message": f"unknown event type {etype!r}; expected one of "
                           f"{sorted(EVENT_TYPES.values())}"}
    data = body.get("data")
    if not isinstance(data, dict):
        return {"valid": False, "code": "BadRequest",
                "message": "event data must be an object"}

    if etype == EVENT_TYPES["published"]:
        pf_ids = data.get("pfIds")
        if not isinstance(pf_ids, list) or not pf_ids:
            return {"valid": False, "code": "BadRequest",
                    "message": "PublishedEvent data.pfIds must be a non-empty array"}
        bad = [x for x in pf_ids if not (isinstance(x, str) and _UUID_RE.match(x))]
        if bad:
            return {"valid": False, "code": "BadRequest",
                    "message": f"pfIds entries must be UUIDs; rejected {bad}"}

    if etype == EVENT_TYPES["request_fulfilled"]:
        pfs = data.get("pfs")
        if not isinstance(pfs, list) or not pfs:
            return {"valid": False, "code": "BadRequest",
                    "message": "RequestFulfilledEvent data.pfs has minItems 1 — send a "
                               "RequestRejectedEvent instead when there is nothing to "
                               "return; 'success with zero results' is not conformant"}
        if not data.get("requestEventId"):
            return {"valid": False, "code": "BadRequest",
                    "message": "requestEventId is required and must echo the request"}

    if etype == EVENT_TYPES["request_rejected"]:
        err = data.get("error")
        if not isinstance(err, dict) or not err.get("code") or not err.get("message"):
            return {"valid": False, "code": "BadRequest",
                    "message": "RequestRejectedEvent requires error.code and "
                               "error.message"}

    return {"valid": True, "event_type": etype, "data": data}


def request_filters(data: dict) -> dict:
    """Filters from a RequestCreatedEvent.

    `status` is an ARRAY here and a SCALAR on the ListFootprints query — the
    OpenAPI schema types them differently, so they cannot share a parser.
    """
    status = data.get("status")
    scalar = None
    if isinstance(status, list) and status:
        scalar = status[0]
    elif isinstance(status, str):
        scalar = status
    return {
        "product_ids": data.get("productId"),
        "company_ids": data.get("companyId"),
        "geographies": data.get("geography"),
        "classifications": data.get("classification"),
        "status": scalar,
        "valid_on": data.get("validOn"),
        "valid_after": data.get("validAfter"),
        "valid_before": data.get("validBefore"),
    }


def build_response_event(kind: str, *, request_event_id: str, source: str,
                         pfs: Optional[list] = None,
                         err: Optional[dict] = None) -> dict:
    """A fulfilled or rejected callback.

    `request_event_id` is echoed BYTE-FOR-BYTE. The runner encodes routing data
    into the id and recovers it by splitting on '/', so normalising it or minting
    a fresh UUID leaves the test hanging in PENDING rather than failing loudly.
    """
    import uuid
    if kind == "fulfilled" and not pfs:
        # Enforced here as well as in validation: the intuitive empty-success
        # response is non-conformant, so it cannot be constructed by accident.
        raise ValueError("a RequestFulfilledEvent needs at least one footprint; send "
                         "a RequestRejectedEvent instead")
    etype = EVENT_TYPES["request_fulfilled" if kind == "fulfilled" else "request_rejected"]
    data = {"requestEventId": request_event_id}
    if kind == "fulfilled":
        data["pfs"] = pfs
    else:
        data["error"] = err or {"code": "NotFound",
                                "message": "no footprints matched the request"}
    return {"type": etype, "specversion": "1.0", "id": str(uuid.uuid4()),
            "source": source, "time": _iso(_now()), "data": data}
