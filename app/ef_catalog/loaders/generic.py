"""Generic normalised-CSV adapter.

The robust path: transform any source into a CSV whose columns are FactorRow
fields, then load it. Required columns: category, unit, value. Optional:
subcategory, geography, year, gwp_set, kg_co2, kg_ch4, kg_n2o, ch4_origin,
method_type, lca_boundary, base_year, price_basis.

PER-GAS MASSES FOR ANY OTHER GAS use a ``gas:<identity>`` column, where the
identity is one services/gwp.py knows — a pure species or a refrigerant blend:

    category,subcategory,unit,value,gwp_set,gas:SF6
    switchgear,SF6 leakage,kg,24300,AR6,1.0

    category,subcategory,unit,value,gwp_set,gas:R-410A
    refrigerant,chiller top-up,kg,2255.5,AR6,1.0

That is the operator-facing route to the HFCs, PFCs, SF6 and NF3 that complete the
Kyoto basket. The cell holds kg of ACTUAL GAS per activity unit, never a CO2e
figure: the GWP set is applied at calculation time, which is what makes a
refrigerant or switchgear line move between AR5 and AR6 instead of being frozen at
whichever vintage it was imported under. An EMPTY cell means the source published
nothing for that gas and no row is stored; an explicit 0 means published-and-zero
and IS stored, because the two are not the same claim.
"""
import csv
import io
from typing import Dict, List

from .base import FactorRow

# Column prefix declaring a per-gas mass for a gas without a dedicated column.
_GAS_COL_PREFIX = "gas:"


def _f(v):
    return float(v) if v not in (None, "") else None


def _i(v):
    return int(v) if v not in (None, "") else 0


def _gas_masses(r: dict) -> Dict[str, float]:
    """``gas:<identity>`` cells for one row, as an identity -> kg map.

    Unparseable and unknown-gas cells are left in place rather than dropped here;
    ``load_factors`` validates and REJECTS the whole row, so a typo in a gas name
    fails loudly at load time instead of silently contributing nothing to a total.
    """
    out: Dict[str, float] = {}
    for col, val in r.items():
        if not col or not col.strip().lower().startswith(_GAS_COL_PREFIX):
            continue
        gas = col.strip()[len(_GAS_COL_PREFIX):].strip()
        if not gas or val in (None, ""):
            continue
        try:
            out[gas] = float(val)
        except (TypeError, ValueError):
            out[gas] = float("nan")   # rejected by load_factors' fail-closed check
    return out


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
            # These three are TOKENS compared downstream (ch4_origin routes the GWP
            # variant; lca_boundary is matched against the Table 5.4 acceptance vocabulary;
            # price_basis selects the EEIO basis), so they are stripped here — a CSV cell
            # holding only whitespace is an ABSENT value, not a token that matches nothing.
            ch4_origin=((r.get("ch4_origin") or "").strip() or None),
            method_type=(r.get("method_type") or "average_data").strip(),
            lca_boundary=((r.get("lca_boundary") or "").strip() or None),
            base_year=(_i(r.get("base_year")) or None) if r.get("base_year") else None,
            price_basis=((r.get("price_basis") or "").strip() or None),
            gas_masses=_gas_masses(r),
        ))
    return rows
