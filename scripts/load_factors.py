"""Load emission factors from a published source file into the catalog.

Usage:
  python -m scripts.load_factors --source defra   --file conversion-factors-2024-flat-file.csv --version 2024
  python -m scripts.load_factors --source useeio  --file SupplyChainGHGEmissionFactors_v1.3_NAICS_CO2e_USD2022.csv --version 1.3 --price-basis purchaser
  python -m scripts.load_factors --source generic --file my_normalised_factors.csv --version 2025.1 --source-name MY_SOURCE

Sources are vintage-pinned: loading a newer --version supersedes the prior
version of each matching factor (the resolver then uses the newest). Only the
free, redistributable sources (DEFRA OGL, EPA/USEEIO public domain) are shipped
as adapters; see app/ef_catalog/registry.py for the licence of each source.
"""
import argparse
from pathlib import Path

from app.database import SessionLocal
from app.ef_catalog.loaders.base import load_factors
from app.ef_catalog.loaders.defra import parse_defra_flat_csv
from app.ef_catalog.loaders.useeio import parse_useeio_csv
from app.ef_catalog.loaders.generic import parse_generic_csv


def main():
    ap = argparse.ArgumentParser(description="Load emission factors into the catalog.")
    ap.add_argument("--source", required=True, choices=["defra", "useeio", "generic"])
    ap.add_argument("--file", required=True)
    ap.add_argument("--version", required=True, help="vintage/version tag, e.g. 2024 or 1.3")
    ap.add_argument("--source-name", help="catalog source label (default derives from --source)")
    ap.add_argument("--price-basis", default="purchaser", choices=["purchaser", "basic"])
    ap.add_argument("--no-supersede", action="store_true")
    args = ap.parse_args()

    data = Path(args.file).read_bytes()
    if args.source == "defra":
        rows = parse_defra_flat_csv(data)
        source_name = args.source_name or "DEFRA_DESNZ"
    elif args.source == "useeio":
        rows = parse_useeio_csv(data, price_basis=args.price_basis)
        source_name = args.source_name or "USEEIO"
    else:
        rows = parse_generic_csv(data)
        source_name = args.source_name or "GENERIC"

    session = SessionLocal()
    try:
        result = load_factors(session, rows, source=source_name, version=args.version,
                              supersede=not args.no_supersede)
        print(f"Loaded {result['added']} factors from {source_name} v{args.version} "
              f"(skipped {result['skipped']}, superseded {result['superseded']} prior).")
    finally:
        session.close()


if __name__ == "__main__":
    main()
