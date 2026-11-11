"""UK Government (DEFRA/DESNZ) GHG conversion factors — 'flat file' CSV adapter.

Targets the published flat-file layout with columns: Scope, Level 1..4,
Column Text, UOM, GHG/Unit, GHG Conversion Factor <year>. Point at the official
annual CSV (OGL, freely redistributable). Category/subcategory come from the Level
columns; UOM is normalised to the platform's units.

PER-GAS INGESTION — WHAT IS AND IS NOT ALLOWED
---------------------------------------------
DESNZ publishes, for most rows, a TOTAL line ("kg CO2e") and beside it one line per
gas ("kg CO2e of CO2 per unit", "... of CH4 ...", "... of N2O ..."). The per-gas
lines are already CO2e-WEIGHTED — they are not raw gas masses — and the weighting is
DESNZ's declared GWP set, AR5.

This adapter used to keep only the total, stamp it ``gwp_set="AR5"``, and leave the
per-gas columns NULL. Its refusal to invent a split was right, but the consequence
was that a customer who loaded the real published file and ran with the platform's
default (AR6) got ``total_co2e: 0.0`` reported as ``status: "complete"``: an
aggregate factor's vintage is baked into its value, so calc excludes it when the
requested set differs — and with every line excluded, the whole inventory was zero.

The distinction that matters, and that this adapter now draws:

  * REFUSED, still: splitting a TOTAL into per-gas shares. Given only
    0.18293 kg CO2e/kWh there is no way to know how much is CH4. Most vendors
    relabel DEFRA figures as AR6; this one does not, and does not guess.
  * ALLOWED: converting a PUBLISHED per-gas CO2e line back to the gas mass that
    produced it, by dividing by the GWP DESNZ states it used. That is the exact
    inverse of DESNZ's own arithmetic, not an inference —

        kg CH4 = (kg CO2e of CH4) / GWP_AR5(CH4)

    and the mass is recovered with the SAME gas key the calculation will later use
    to recompose it, so a run under AR5 reproduces the published figure exactly, by
    construction, whatever that key's value happens to be.

RECONCILIATION GATE
-------------------
Recovered masses are only stored if recomposing them under AR5 reproduces the
PUBLISHED total to within the precision the file itself is published at
(``_rounding_tolerance``). A larger residual means a gas line is missing, and
storing the breakdown anyway would silently understate the factor by the missing
gas's share — small, invisible and in the wrong direction. Such a row stays
aggregate-only, so its vintage mismatch is reported as a reason rather than
quietly swallowed.

Rows where DESNZ publishes no per-gas lines at all (material use, waste, hotel
stays) likewise stay aggregate-only. That is faithful: the vintage is genuinely
baked in, and the engine says so.
"""
import csv
import io
import re
from typing import Dict, List, Optional, Tuple

from ...services.gwp import GAS_BLENDS, GWP_100, gwp, is_known_gas
from .base import FactorRow

# The GWP set DESNZ weights its published per-gas CO2e lines with. Stated in the
# DESNZ conversion-factors methodology; it is also why the aggregate `value` is
# stamped AR5 below. If DESNZ ever restates on AR6 this constant is the one place
# that changes — and the reconciliation gate below would catch a silent restatement,
# because masses recovered with the wrong divisor stop summing to the total.
DEFRA_GWP_SET = "AR5"

# Gas keys (as services/gwp.py names them) for the per-gas lines DESNZ publishes.
# `ch4_origin` is deliberately left NULL on DEFRA rows, so CH4 recomposes through
# the same bare "CH4" key it was recovered with.
_GAS_LINE_KEYS = {"co2": "CO2", "ch4": "CH4", "n2o": "N2O"}

# "kg CO2e of CH4 per unit" -> ch4
_PER_GAS_RE = re.compile(r"\bof\s+([a-z0-9]+)\b")

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


def _decimals(raw: str) -> int:
    """Decimal places in a published figure, used to size the rounding tolerance."""
    if "." not in raw:
        return 0
    return len(raw.split(".", 1)[1].rstrip())


def _rounding_tolerance(decimals: List[int], n_terms: int) -> float:
    """Largest residual explicable by PUBLICATION ROUNDING alone.

    Each published figure (the total and each per-gas line) is rounded to its own
    last decimal place, so each contributes at most half a unit in that place. The
    worst case is the sum of those half-units.

    This is deliberately derived from the file's own precision rather than set as a
    flat percentage. A flat tolerance large enough to absorb rounding on a small
    factor is also large enough to absorb a MISSING GAS LINE on a large one — which
    is precisely the silent understatement the gate exists to catch. On the real
    DESNZ natural-gas row the rounding bound is ~2e-5 kg CO2e/kWh while a missing
    N2O line is ~7e-5, so the bound separates them; a 1% rule would pass both.
    """
    dp = min(decimals) if decimals else 5
    half = 0.5 * (10.0 ** -dp)
    # A small absolute floor for binary-float noise in the division/recomposition.
    return max(half * (n_terms + 1), 1e-12)


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


def _refrigerant_gas(*labels) -> Optional[str]:
    """The gas identity for a DESNZ refrigerant designation, or None.

    DESNZ writes refrigerants as "R410A", "HFC-134a", "R-404A" across years and
    tables; services/gwp.py keys them canonically ("R-410A"). Only designations that
    resolve to a KNOWN species or shipped blend are accepted — an unrecognised
    refrigerant returns None and its row stays aggregate-only, rather than being
    mapped to a near-neighbour whose GWP is thousands of units different.
    """
    for label in labels:
        tok = (label or "").strip().upper().replace(" ", "")
        if not tok:
            continue
        for cand in (tok, re.sub(r"^R-?", "R-", tok), re.sub(r"^HFC-?", "HFC-", tok)):
            # GWP_100 keys halocarbons in mixed case ("HFC-134a"); match case-folded.
            for known in list(GAS_BLENDS) + list(GWP_100["AR6"]):
                if known.upper() == cand and is_known_gas(known):
                    return known
    return None


def _refrigerant_mass(published_co2e: float, total_dp: int,
                      gas: str) -> Optional[Dict[str, float]]:
    """Unit gas mass for a refrigerant row, if its published figure reconciles.

    A DESNZ refrigerant line is "kg CO2e per kg of refrigerant" — which IS that
    refrigerant's GWP under the set DESNZ uses. So the faithful ingestion of 1 kg of
    refrigerant is 1 kg of THAT GAS, with the GWP applied at calculation time:
    stored as an aggregate instead, a refrigerant line would be frozen at AR5 and
    excluded from every AR6 run, which is how fugitive F-gases went missing.

    The published figure is reconciled against the GWP this platform composes for
    the designation. A mismatch means either the composition here or the vintage
    assumed differs from DESNZ's, so the row stays aggregate-only rather than
    silently re-scaling a customer's refrigerant emissions.
    """
    composed = gwp(gas, DEFRA_GWP_SET)
    if abs(published_co2e - composed) > _rounding_tolerance([total_dp], 1):
        return None
    return {gas: 1.0}


def _classify_ghg_line(ghg: str) -> Optional[str]:
    """What kind of line this ``GHG/Unit`` cell describes.

    Returns "total" for the aggregate CO2e line, a gas key ("CO2"/"CH4"/"N2O") for a
    published per-gas CO2e line, or None for anything unrecognised. Unrecognised is
    SKIPPED rather than guessed: a line this adapter cannot classify must not be
    added into either the total or a gas mass.
    """
    g = (ghg or "").strip().lower()
    if "co2e" not in g:
        return None
    if " of " not in g:
        return "total"
    m = _PER_GAS_RE.search(g)
    if not m:
        return None
    return _GAS_LINE_KEYS.get(m.group(1))


def _recover_masses(per_gas: Dict[str, Tuple[float, int]], total: float,
                    total_dp: int) -> Tuple[Optional[Dict[str, float]], float]:
    """Gas masses recovered from published per-gas CO2e lines, or None.

    Each published per-gas CO2e figure is divided by the GWP of that gas under the
    set DESNZ declares, recovering the gas mass exactly. The recovered masses are
    then recomposed under the same set and compared with the published TOTAL: if the
    residual exceeds what publication rounding can explain, a gas line is missing and
    None is returned, leaving the row aggregate-only rather than understated.

    Returns (masses or None, residual).
    """
    if not per_gas:
        return None, 0.0
    masses = {gas: co2e / gwp(gas, DEFRA_GWP_SET) for gas, (co2e, _) in per_gas.items()}
    recomposed = sum(m * gwp(gas, DEFRA_GWP_SET) for gas, m in masses.items())
    residual = abs(total - recomposed)
    decimals = [total_dp] + [dp for _, dp in per_gas.values()]
    if residual > _rounding_tolerance(decimals, len(per_gas)):
        return None, residual
    return masses, residual


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

    # A factor is identified by its published table position + unit; the total line
    # and its per-gas lines share that identity and differ only in `GHG/Unit`. Keyed
    # in FILE ORDER so output order is deterministic and independent of dict hashing.
    groups: Dict[tuple, dict] = {}
    for r in reader:
        kind = _classify_ghg_line(r.get(ghg_col))
        if kind is None:
            continue
        raw = (r.get(cf_col) or "").strip().replace(",", "")
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue

        key = (r.get("Scope") or "", r.get("Level 1") or "", r.get("Level 2") or "",
               r.get("Level 3") or "", r.get("Level 4") or "",
               r.get("Column Text") or "", r.get("UOM") or "")
        g = groups.setdefault(key, {"row": r, "total": None, "total_dp": 0, "gases": {}})
        if kind == "total":
            # A duplicate total for one published row would be ambiguous; keep the
            # first and ignore the rest rather than summing two readings of one figure.
            if g["total"] is None:
                g["total"], g["total_dp"] = value, _decimals(raw)
        else:
            g["gases"].setdefault(kind, (value, _decimals(raw)))

    rows: List[FactorRow] = []
    for key, g in groups.items():
        if g["total"] is None:
            # Per-gas lines with no published total: nothing to reconcile against, so
            # the masses cannot be confirmed complete. Skipped rather than trusted.
            continue
        r = g["row"]
        masses, _residual = _recover_masses(g["gases"], g["total"], g["total_dp"])
        category = _slug(r.get("Level 1"))
        subcategory = _slug(r.get("Level 2"), r.get("Level 3"), r.get("Level 4"),
                            r.get("Column Text"))
        unit = _norm_uom(r.get("UOM"))

        # A refrigerant row carries no per-gas lines — its published figure is a GWP,
        # not a mass — so it is decomposed from its DESIGNATION instead. Only "per kg
        # of refrigerant" rows qualify: the figure must be CO2e per unit mass of the
        # named gas for "1 kg of that gas" to be the faithful reading.
        gas_masses: Dict[str, float] = {}
        if masses is None and unit == "kg":
            rgas = _refrigerant_gas(r.get("Level 3"), r.get("Column Text"),
                                    r.get("Level 4"), r.get("Level 2"))
            if rgas is not None:
                gas_masses = _refrigerant_mass(g["total"], g["total_dp"], rgas) or {}

        rows.append(FactorRow(
            category=category or "uncategorised", subcategory=subcategory,
            unit=unit, value=g["total"], geography=geography,
            year=year, gas_masses=gas_masses,
            # The aggregate `value` remains the AR5-weighted published total, so a
            # row that could NOT be decomposed is still vintage-checked honestly.
            # Where masses were recovered, calc applies the requested set to them and
            # the stamp describes only the (now redundant) aggregate.
            gwp_set=DEFRA_GWP_SET, method_type="average_data",
            kg_co2=(masses or {}).get("CO2"),
            kg_ch4=(masses or {}).get("CH4"),
            kg_n2o=(masses or {}).get("N2O"),
            lca_boundary=_derive_boundary(r.get("Scope"), r.get("Level 1"),
                                          r.get("Level 2"))))
    return rows
