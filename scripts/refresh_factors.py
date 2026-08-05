"""Operator CLI to refresh the emission-factor catalog from a published source.

A LIVE fetch happens ONLY when a human runs this. Nothing here runs on import or during tests.
The source's licence and required attribution are printed before anything loads, so the
operator is accountable for the provenance of what enters the catalog.

Examples:
    # See what a file would load, without touching the DB (safe, no insert):
    python -m scripts.refresh_factors --source defra --version 2024 \
        --file ~/Downloads/defra_2024_flat.csv --dry-run

    # Load a downloaded file under (DEFRA_DESNZ, 2024), superseding the prior version:
    python -m scripts.refresh_factors --source defra --version 2024 \
        --file ~/Downloads/defra_2024_flat.csv

    # Fetch directly from a URL (operator confirms the URL is the correct, licensed file):
    python -m scripts.refresh_factors --source useeio --version 2.1 --url https://.../useeio.csv

The registry (app/ef_catalog/sources.py) documents each source's official publication page and
licence; --url/--file supplies the exact data file to load.
"""
import argparse
import sys

from app.database import SessionLocal
from app.ef_catalog.sources import SOURCES
from app.ef_catalog.refresh import refresh_source, http_fetch


def _file_fetcher(path: str):
    def _fetch(_url: str) -> bytes:
        with open(path, "rb") as fh:
            return fh.read()
    return _fetch


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Refresh emission factors from a published source.")
    p.add_argument("--source", required=True, choices=sorted(SOURCES),
                   help="source key (see app/ef_catalog/sources.py)")
    p.add_argument("--version", required=True, help="version tag to pin these factors under")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--file", help="load bytes from a LOCAL file (recommended: download first)")
    g.add_argument("--url", help="fetch bytes from an HTTPS URL")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch + parse + report counts, but do NOT insert")
    args = p.parse_args(argv)

    src = SOURCES[args.source]
    # Provenance and licence, up front — the operator is accountable for this.
    print(f"source     : {src.key} — {src.name}")
    print(f"publisher  : {src.publisher}")
    print(f"provenance : {src.url}")
    print(f"licence    : {src.license}")
    print(f"attribution: {src.attribution}")
    print(f"version    : {args.version}    dry-run: {args.dry_run}")

    if not args.file and not args.url:
        print("\nRefusing to fetch: pass --file <path> (a downloaded file) or --url <https>. "
              "The registry URL is a provenance page, not necessarily a direct file.",
              file=sys.stderr)
        return 2

    fetcher = _file_fetcher(args.file) if args.file else http_fetch
    session = SessionLocal()
    try:
        summary = refresh_source(session, args.source, args.version,
                                 url=args.url, fetcher=fetcher, dry_run=args.dry_run)
    finally:
        session.close()

    print("\nresult:")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    if args.dry_run:
        print("\n(dry run — nothing was inserted)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
