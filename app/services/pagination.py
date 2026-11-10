"""Shared collection pagination — ONE design, replicated from the PACT host.

Every paginated collection answers with the same three things: the window the caller
asked for, whether rows exist beyond it, and the offset that would fetch them.

The defect this module exists to remove is the SILENT cap. A list truncated at 500 with
no indication it was truncated is indistinguishable from a complete one, and a reader
draws a conclusion from it — which is worse than an error, because an error is at least
visible. So a cap is never silent here: ``has_more`` says whether anything was withheld,
``total_matched`` says how much, and ``next_offset`` says how to get it.

The design (limit validated 1..1000, offset >= 0, the window computed in the service
layer, an RFC 8288 ``Link`` header that preserves every other query parameter) is
deliberately the same one ``services/pact_host.list_footprints`` already implements for
the PACT host endpoint; this module is its generalisation, not a second scheme.

NOTE ON ORDERING: a LIMIT/OFFSET window is only stable over a deterministic ORDER BY.
Every caller here orders by primary key before windowing; a caller that does not would
return overlapping or skipped rows across pages.
"""

from typing import Iterable, Optional
from urllib.parse import urlencode

DEFAULT_LIMIT = 100
MAX_LIMIT = 1000


class PaginationError(ValueError):
    """limit/offset outside the contract. Callers map this to 400."""


def validate(limit: int, offset: int) -> None:
    """Reject a window the contract does not allow, rather than clamping it.

    Clamping is how a silent cap gets reintroduced: a caller asking for 5000 and
    receiving 1000 without being told has the same problem all over again.
    """
    if limit < 1 or limit > MAX_LIMIT:
        raise PaginationError(f"limit must be 1..{MAX_LIMIT}")
    if offset < 0:
        raise PaginationError("offset must be >= 0")


def _meta(rows: list, total: int, limit: int, offset: int) -> dict:
    return {"rows": rows, "total_matched": total,
            "has_more": (offset + limit) < total,
            "next_offset": offset + limit}


def page_of(items: Iterable, limit: int, offset: int) -> dict:
    """Window an already-materialised sequence.

    For collections whose filtering cannot be expressed in SQL. It still has to hold the
    filtered set in memory, so prefer :func:`page_of_query` wherever the filter is a
    database predicate.
    """
    validate(limit, offset)
    rows = list(items)
    return _meta(rows[offset:offset + limit], len(rows), limit, offset)


def page_of_query(query, limit: int, offset: int) -> dict:
    """Window a SQLAlchemy query IN THE DATABASE.

    This is the one that matters for size: it never materialises a row outside the
    window. The assurer drill-down returned 65.5 MB in a single response and peaked
    746 MB RSS precisely because it loaded and serialised every line item of a run.
    """
    validate(limit, offset)
    total = query.order_by(None).count()
    return _meta(query.limit(limit).offset(offset).all(), total, limit, offset)


def page_block(page: dict, limit: int, offset: int) -> dict:
    """The block every paginated response carries.

    ``next_offset`` is None — not the past-the-end integer — once the window reaches the
    end, so a client loops on a value rather than on a flag and still terminates.
    """
    return {"limit": limit, "offset": offset,
            "returned": len(page["rows"]),
            "total_matched": page["total_matched"],
            "has_more": page["has_more"],
            "next_offset": page["next_offset"] if page["has_more"] else None}


def link_header(base_url: str, path: str, params: dict, next_offset: int) -> str:
    """RFC 8288 ``next`` link, preserving every OTHER query parameter.

    Dropping the caller's filters from the next link is the classic pagination bug: page
    two of a filtered list silently becomes page two of the unfiltered one. ``offset`` is
    replaced rather than appended, or the URL would carry both values.
    """
    q = {k: v for k, v in params.items() if k != "offset"}
    q["offset"] = next_offset
    return f'<{base_url}{path}?{urlencode(q, doseq=True)}>; rel="next"'


def next_link_headers(request, page: dict) -> dict:
    """``{}`` or ``{"Link": ...}``, ready to pass as a response's headers.

    Empty when nothing follows: emitting a next link on the last page makes a client that
    trusts it loop forever.
    """
    if not page["has_more"]:
        return {}
    q = {k: request.query_params.getlist(k)
         for k in request.query_params.keys() if k != "offset"}
    base = str(request.base_url).rstrip("/")
    return {"Link": link_header(base, request.url.path, q, page["next_offset"])}


def params(limit: Optional[int] = None, offset: Optional[int] = None) -> tuple:
    """Normalise the pair, applying the default limit for an absent one."""
    return (DEFAULT_LIMIT if limit is None else limit, 0 if offset is None else offset)
