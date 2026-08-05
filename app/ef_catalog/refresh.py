"""Fetch a published emission-factor file and load it under a pinned (source, version).

The fetch is INJECTABLE (`fetcher`), so:
  * tests exercise the whole parse+load pipeline with a local sample and NEVER hit the network;
  * a live fetch only happens when an operator runs scripts/refresh_factors.py with an explicit
    --url or --file.

Loading reuses load_factors, so the immutable/version-pinned/supersede semantics are identical
to any other catalog load — a refresh adds a new version and supersedes the old one; it never
mutates a factor in place.
"""
import urllib.request
from typing import Callable, Optional

from sqlalchemy.orm import Session

from .sources import SOURCES
from .loaders.base import load_factors

DEFAULT_TIMEOUT_SECONDS = 30

Fetcher = Callable[[str], bytes]


def http_fetch(url: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> bytes:
    """Default fetcher — a plain HTTPS GET. Invoked ONLY by the operator-run CLI, never at
    import and never in tests (which inject their own fetcher)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "carbon-platform-ef-refresh/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (operator-invoked)
        return resp.read()


def refresh_source(session: Session, source_key: str, version: str, *,
                   url: Optional[str] = None, fetcher: Fetcher = http_fetch,
                   dry_run: bool = False) -> dict:
    """Fetch the file for `source_key`, parse it, and (unless dry_run) load it under
    (source, version). Returns a summary including the licence/attribution to surface.

    `url` overrides the registry's publication URL — required for a real fetch, since the
    registry URL is a provenance page, not necessarily a direct file.
    """
    src = SOURCES.get(source_key)
    if src is None:
        raise ValueError(f"unknown source {source_key!r}; one of {sorted(SOURCES)}")

    raw = fetcher(url or src.url)
    rows = src.parse(raw)

    summary = {
        "source_key": src.key,
        "source": src.default_source,
        "version": version,
        "parsed_rows": len(rows),
        "license": src.license,
        "attribution": src.attribution,
        "dry_run": dry_run,
    }
    if dry_run:
        summary["loaded"] = False
        return summary

    result = load_factors(session, rows, source=src.default_source, version=version)
    summary["loaded"] = True
    summary.update({k: result[k] for k in ("added", "skipped", "superseded") if k in result})
    return summary
