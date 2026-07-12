"""Generic normalised-CSV adapter.

The robust path: transform any source into a CSV whose columns are FactorRow
fields, then load it. Required columns: category, unit, value. Optional:
subcategory, geography, year, gwp_set, kg_co2, kg_ch4, kg_n2o, ch4_origin,
method_type, lca_boundary, base_year, price_basis.
"""
import csv
import io
from typing import List

from .base import FactorRow


def _f(v):
    return float(v) if v not in (None, "") else None


def _i(v):
    return int(v) if v not in (None, "") else 0


def parse_generic_csv(data: bytes) -> List[FactorRow]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    rows = []
    for r in reader:
        rows.append(FactorRow(
            category=(r.get("category") or "").strip(),
            subcategory=(r.get("subcategory") or "").strip(),
            unit=(r.get("unit") or "").strip(),
            value=float(r["value"]),
            geography=(r.get("geography") or "Global").strip(),
            year=_i(r.get("year")),
            gwp_set=(r.get("gwp_set") or "AR6").strip(),
            kg_co2=_f(r.get("kg_co2")), kg_ch4=_f(r.get("kg_ch4")), kg_n2o=_f(r.get("kg_n2o")),
            ch4_origin=(r.get("ch4_origin") or None),
            method_type=(r.get("method_type") or "average_data").strip(),
            lca_boundary=(r.get("lca_boundary") or None),
            base_year=(_i(r.get("base_year")) or None) if r.get("base_year") else None,
            price_basis=(r.get("price_basis") or None),
        ))
    return rows
