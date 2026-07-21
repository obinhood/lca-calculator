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


def _derive_boundary(scope: str, level1: str, level2: str = "") -> Optional[str]:
    """LCA system boundary for a DEFRA row, derived ONLY where the published
    structure makes it UNAMBIGUOUS *and* the derived token is safe for every GHGP
    category the factor could serve; None otherwise.

    This is the whole point of the backfill: without a boundary the Scope 3
    completeness gate can never check GHGP Table 5.4 (it only warns, W1). Two failure
    directions are both avoided:

    * FALSE PASS (understatement) — a fabricated boundary that makes B12 pass a factor
      whose real boundary is below the minimum. Worse than None; the exact silent-pass
      lie `ghgp.boundary_meets_minimum` refuses to tell.
    * FALSE BLOCK — a boundary that B12 rejects for a *compliant* line. A factor is
      scope-agnostic (EmissionFactor carries no scope), so a Scope-1 gas-combustion
      factor is legitimately usable on a Scope-3 activity (e.g. a leased building's gas
      heating → Cat 8). Under the ORIGINAL token vocabulary the accepted sets were
      asymmetric — `combustion` accepted by Cat 4/6/7/9 but not Cat 8/13/14, `generation`
      vice-versa — so deriving those tokens flipped a safe W1 into a false block. The
      s3bnd-v2 boundary policy fixed that asymmetry at its source, so they are derived
      again here.

    Tokens are assigned where the DEFRA table structure is unambiguous: the Scope 3
    tables (upstream WTT/T&D, waste, travel/freight, materials) and the direct energy
    tables (Scope 1 fuel combustion, Scope 2 generation). `ttw` and, under s3bnd-v2,
    `combustion`/`generation` are accepted across the whole scope1/2-family (Cat 4-10,
    12-14); the upstream/waste/material tokens are factor-type-specific to their
    categories, so applying one outside its category is a TRUE block, not a false one.
    DEFRA separates the direct in-use factor from its upstream "WTT-" counterpart in
    distinct tables, so the table name disambiguates without guessing. Anything not
    clearly determined stays None and the gate honestly reports "not assessable" (W1).
    """
    s = (scope or "").strip().lower()
    l1 = (level1 or "").strip().lower()
    ctx = f"{l1} {(level2 or '').strip().lower()}"

    # Upstream fuel/energy (well-to-tank) and grid transmission & distribution losses
    # -> Category 3 (accepts well_to_tank / wtt / td_loss). The "WTT-" prefix and the
    # "Transmission and distribution" table are DEFRA's own, unambiguous labels.
    if l1.startswith("wtt"):
        if any(t in ctx for t in ("t&d", "transmission", "distribution")):
            return "td_loss"
        return "well_to_tank"
    if "transmission and distribution" in l1:
        return "td_loss"

    # Waste treatment process emissions -> Category 5 (accepts waste_treatment).
    if l1.startswith("waste") or "waste disposal" in ctx:
        return "waste_treatment"
    # DEFRA's direct travel/freight tables are the TAILPIPE (tank-to-wheel) figure; the
    # upstream fuel is the separate "WTT-" table handled above. `ttw` is accepted by
    # every scope1/2-family Scope 3 category (Cat 4-10, 12-14), so it never false-blocks;
    # and a tailpipe factor is never upstream-only, so it never false-passes.
    if l1.startswith("business travel") or l1.startswith("freighting") or "delivery" in l1:
        return "ttw"
    # Purchased materials -> Category 1 (accepts cradle_to_gate).
    if l1.startswith("material use"):
        return "cradle_to_gate"

    # Direct energy. These are Scope 1/2 factors, so on their PRIMARY use the Scope 3
    # boundary check never runs (a Scope 1/2 line has no ghgp_category). They matter
    # because a factor is scope-AGNOSTIC: the same gas-combustion factor is legitimately
    # used on a Scope 3 line (a leased building's heating, Cat 8). Under the ORIGINAL
    # token vocabulary that false-blocked, which is why these were left None; the
    # s3bnd-v2 policy accepts `combustion` and `generation` across the whole
    # scope1/2-family, so deriving them is now both faithful and safe. Where they are
    # genuinely wrong — a combustion factor on Cat 3 (upstream fuel) or Cat 1/2
    # (cradle-to-gate goods) — the gate now BLOCKS, which is the check working.
    if s == "scope 1":
        # Direct combustion of fuels/bioenergy. Refrigerants / fugitive / process are
        # NOT a combustion boundary -> None.
        if l1.startswith("fuel") or "bioenergy" in l1:
            return "combustion"
        return None
    if s == "scope 2":
        if any(t in l1 for t in ("electricity", "heat", "steam", "cooling", "district")):
            return "generation"
        return None
    return None


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
            lca_boundary=_derive_boundary(r.get("Scope"), r.get("Level 1"),
                                          r.get("Level 2"))))
    return rows
