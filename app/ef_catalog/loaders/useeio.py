"""US EPA / Cornerstone USEEIO Supply-Chain GHG emission factors — CSV adapter.

Targets the published NAICS CO2e/USD file with columns: 2017 NAICS Code,
2017 NAICS Title, GHG, Unit, Supply Chain Emission Factors without Margins,
Supply Chain Emission Factors with Margins, Reference USEEIO Code. Public
domain; freely redistributable. These are spend-based EEIO factors per USD at
a base year (e.g. 2022), AR6 GWP.

price_basis: 'basic' -> factors WITHOUT margins (producer price);
             'purchaser' -> factors WITH margins (retail spend as recorded).
Only the 'All GHGs' rows (the CO2e total) are loaded.
"""
import csv
import io
import re
from typing import List

from .base import FactorRow


def parse_useeio_csv(data: bytes, price_basis: str = "purchaser",
                     base_year: int = 2022, geography: str = "US") -> List[FactorRow]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig")))
    fields = [c.strip() for c in (reader.fieldnames or [])]

    def col(*names):
        for n in names:
            for c in (reader.fieldnames or []):
                if c.strip().lower() == n.lower():
                    return c
        return None

    code_c = col("2017 NAICS Code", "NAICS Code", "Code")
    title_c = col("2017 NAICS Title", "NAICS Title", "Name")
    ghg_c = col("GHG")
    unit_c = col("Unit")
    with_c = col("Supply Chain Emission Factors with Margins")
    without_c = col("Supply Chain Emission Factors without Margins")
    val_c = with_c if price_basis == "purchaser" else without_c
    if not (code_c and ghg_c and val_c):
        raise ValueError("not a USEEIO supply-chain file: missing NAICS/GHG/factor columns")

    rows = []
    for r in reader:
        if (r.get(ghg_c) or "").strip().lower() not in ("all ghgs", "co2e", "all ghg"):
            continue
        raw = (r.get(val_c) or "").strip().replace(",", "")
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        code = (r.get(code_c) or "").strip()
        title = (r.get(title_c) or "").strip() if title_c else ""
        unit = (r.get(unit_c) or "").strip() if unit_c else ""
        m = re.search(r"(19|20)\d{2}", unit)  # e.g. "kg CO2e/2022 USD"
        by = int(m.group(0)) if m else base_year
        cur_m = re.search(r"\b([A-Z]{3})\b", unit.upper())
        currency = cur_m.group(1) if cur_m else "USD"
        rows.append(FactorRow(
            category="spend", subcategory=(code or title), unit=currency, value=value,
            geography=geography, year=by, gwp_set="AR6", method_type="spend_based",
            lca_boundary="cradle_to_gate", base_year=by,
            price_basis=("purchaser" if price_basis == "purchaser" else "basic")))
    return rows
