"""UK Government (DEFRA/DESNZ) GHG conversion factors — 'flat file' CSV adapter.

Targets the published flat-file layout with columns: Scope, Level 1..4,
Column Text, UOM, GHG/Unit, GHG Conversion Factor <year>. Point at the official
annual CSV (OGL, freely redistributable). DEFRA expresses per-gas rows already
as AR5-weighted CO2e (not raw gas mass), so only the TOTAL is stored, as an
aggregate factor at gwp_set=AR5 — faithful to the source (no back-solved
per-gas). Category/subcategory come from the Level columns; UOM is normalised
to the platform's units.
"""
import csv
import io
from typing import List, Optional

from .base import FactorRow

_UOM = {
    "litres": "L", "litre": "L", "tonnes": "tonne", "tonne": "tonne",
    "kwh": "kWh", "km": "km", "kg": "kg", "miles": "mile",
    "passenger.km": "pkm", "tonne.km": "tkm", "m3": "m**3", "kwh (net cv)": "kWh",
}


def _slug(*parts) -> str:
    bits = [p.strip() for p in parts if p and p.strip()]
    return " / ".join(bits)


def _norm_uom(uom: str) -> str:
    u = (uom or "").strip()
    return _UOM.get(u.lower(), u)


def parse_defra_flat_csv(data: bytes, year: Optional[int] = None,
                         geography: str = "GB") -> List[FactorRow]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    fields = reader.fieldnames or []
    cf_col = next((c for c in fields if c.strip().lower().startswith("ghg conversion factor")), None)
    ghg_col = next((c for c in fields if c.strip().lower() in ("ghg/unit", "ghg")), None)
    if cf_col is None or ghg_col is None:
        raise ValueError("not a DEFRA flat file: missing 'GHG Conversion Factor' / 'GHG/Unit'")
    if year is None:
        digits = "".join(ch for ch in cf_col if ch.isdigit())
        year = int(digits) if digits else 0

    rows = []
    for r in reader:
        ghg = (r.get(ghg_col) or "").strip().lower()
        # Keep only the TOTAL row (e.g. "kg CO2e"); skip per-gas "... of CO2 ..." rows.
        if "co2e" not in ghg or " of " in ghg:
            continue
        raw = (r.get(cf_col) or "").strip().replace(",", "")
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        category = _slug(r.get("Level 1"))
        subcategory = _slug(r.get("Level 2"), r.get("Level 3"), r.get("Level 4"),
                            r.get("Column Text"))
        rows.append(FactorRow(
            category=category or "uncategorised", subcategory=subcategory,
            unit=_norm_uom(r.get("UOM")), value=value, geography=geography, year=year,
            gwp_set="AR5", method_type="average_data",
            lca_boundary=None))
    return rows
