"""Global Warming Potential tables (100-year) and per-gas CO2e aggregation.

GWP is applied at CALCULATION time from per-gas masses for any ``EmissionFactor``
carrying a gas breakdown — see ``calc.compute_activity_co2e`` — which is what
makes the AR5/AR6 switch real rather than cosmetic (Gap 2): the same factor row
yields different CO2e under AR5 vs AR6. Factors WITHOUT a breakdown fall back to
their pre-aggregated ``value`` and are vintage-checked against the requested set
instead.

WHERE A GAS MASS LIVES
----------------------
``gas_masses_for_factor`` is THE assembler: it is the only function that knows
how a factor's gas masses are stored, and every CO2e path goes through it.

* CO2, CH4 and N2O live in the ``kg_co2`` / ``kg_ch4`` / ``kg_n2o`` columns on
  ``emission_factors``. They appear in essentially every combustion factor, so
  they stay on the hot path with no join, and every pre-existing row and filed
  run keeps working untouched.
* EVERY OTHER GAS lives in the ``factor_gas_masses`` association table, keyed by
  the gas name used here. Adding a gas is therefore DATA, not a schema change.

The two stores are DISJOINT by construction: a ``factor_gas_masses`` row naming
a column-backed gas is a data error and this module raises on it rather than
silently preferring one store and double counting or dropping the other.

Why an association table and not a column per species: the AR6 halocarbon list
runs to ~90 species and grows with every assessment cycle, so a column per gas
means a migration per cycle; and a nullable column cannot distinguish "this gas
was never measured" from "this gas was measured and is zero". In the table,
no row means not measured and a row of 0.0 means measured-and-zero — the
platform's NULL-vs-empty invariant, which a column schema would quietly lose.

SOURCE OF THE VALUES
--------------------
Primary reference for EVERY value below:

    GHG Protocol, "IPCC Global Warming Potential Values", Version 2.0,
    7 August 2024 — the AR4/AR5/AR6 100-year GWP table.
    https://ghgprotocol.org/sites/default/files/2024-08/Global-Warming-Potential-Values%20(August%202024).pdf

That document is adapted from IPCC AR6 WG1 Ch.7 §7.6.1.1 and is the compilation
the GHG Protocol — the standard this engine implements — directs preparers to.
It is cited in preference to the IPCC chapter itself because it is the artifact
an assuror checks a filing against, it is versioned, and it resolves the AR6
halocarbon values into a single published table.

Underlying IPCC sources, as that document cites them:
  * AR5: WG1 Ch.8, Table 8.A.1 / 8.7 (values WITHOUT climate-carbon feedbacks —
    the set used by the GHG Protocol and by DEFRA/DESNZ conversion factors).
  * AR6: WG1 Ch.7 §7.6.1.1 (Table 7.15 and supplementary Table 7.SM.7).

Fossil vs. non-fossil CH4 differ because the fossil value includes the radiative
forcing of the CO2 formed when the methane oxidises, whereas for non-fossil
methane that carbon is either not a net addition to the cycle or is already
counted as combustion CO2 from the same source. Per the GHG Protocol's own
instruction, "fossil" is for fugitive fossil sources (oil & gas, coal mining)
and fossil-carbon process emissions; COMBUSTION uses the non-fossil value.
"""
from __future__ import annotations

from typing import Dict, Mapping, Optional

# kg CO2e per kg of gas, 100-year horizon.
#
# Keys are the gas identities this platform stores. For halocarbons the key is the
# industrial designation as the GHG Protocol table prints it ("HFC-134a"), because
# that is what appears on a refrigerant cylinder, an F-gas log and a DESNZ table —
# a formula-based key would force every ingestion path to translate.
GWP_100: Mapping[str, Mapping[str, float]] = {
    "AR5": {
        # --- Major greenhouse gases -------------------------------------------------
        "CO2": 1.0,
        "CH4": 28.0,          # platform default when ch4_origin is NULL; see note below
        "CH4_fossil": 30.0,
        "CH4_biogenic": 28.0,
        "N2O": 265.0,
        "SF6": 23500.0,
        "NF3": 16100.0,
        # --- Hydrofluorocarbons (HFCs) ----------------------------------------------
        "HFC-23": 12400.0,
        "HFC-32": 677.0,
        "HFC-41": 116.0,
        "HFC-125": 3170.0,
        "HFC-134": 1120.0,
        "HFC-134a": 1300.0,
        "HFC-143": 328.0,
        "HFC-143a": 4800.0,
        "HFC-152": 16.0,
        "HFC-152a": 138.0,
        "HFC-161": 4.0,
        "HFC-227ca": 2640.0,
        "HFC-227ea": 3350.0,
        "HFC-236cb": 1210.0,
        "HFC-236ea": 1330.0,
        "HFC-236fa": 8060.0,
        "HFC-245ca": 716.0,
        "HFC-245cb": 4620.0,
        "HFC-245ea": 235.0,
        "HFC-245eb": 290.0,
        "HFC-245fa": 858.0,
        "HFC-263fb": 76.0,
        "HFC-272ca": 144.0,
        "HFC-329p": 2360.0,
        "HFC-365mfc": 804.0,
        "HFC-43-10mee": 1650.0,
        # --- Perfluorocarbons (PFCs) / fully fluorinated species ---------------------
        "PFC-14": 6630.0,        # CF4
        "PFC-116": 11100.0,      # C2F6
        "PFC-218": 8900.0,       # C3F8
        "PFC-318": 9540.0,       # c-C4F8
        "PFC-31-10": 9200.0,     # n-C4F10
        "PFC-41-12": 8550.0,     # n-C5F12
        "PFC-51-14": 7910.0,     # n-C6F14
        "PFC-61-16": 7820.0,     # n-C7F16
        "PFC-71-18": 7620.0,     # n-C8F18
        "PFC-91-18": 7190.0,     # C10F18
        "SF5CF3": 17400.0,       # trifluoromethyl sulfur pentafluoride
        "SO2F2": 4090.0,         # sulfuryl fluoride
    },
    "AR6": {
        # --- Major greenhouse gases -------------------------------------------------
        "CO2": 1.0,
        "CH4": 27.9,          # platform default when ch4_origin is NULL; see note below
        "CH4_fossil": 29.8,
        "CH4_biogenic": 27.0,
        "N2O": 273.0,
        # SF6: the GHG Protocol v2.0 table gives 24,300 for AR6, and that is the figure
        # used here. AR6 Ch.7 is internally inconsistent on this gas — the main-chapter
        # Table 7.15 prints 25,200 while supplementary Table 7.SM.7 prints 24,300 — and
        # secondary compilations split between the two. 24,300 is chosen because it is
        # the value in the GHG Protocol's own published table (the document an assuror
        # checks a filing against) and the value carried on EU F-gas Regulation
        # 2024/573 equipment labelling. The divergence is recorded here rather than
        # resolved silently: a reader comparing this engine against a source quoting
        # 25,200 needs to see that the choice was deliberate.
        "SF6": 24300.0,
        "NF3": 17400.0,
        # --- Hydrofluorocarbons (HFCs) ----------------------------------------------
        "HFC-23": 14600.0,
        "HFC-32": 771.0,
        "HFC-41": 135.0,
        "HFC-125": 3740.0,
        "HFC-134": 1260.0,
        "HFC-134a": 1530.0,
        "HFC-143": 364.0,
        "HFC-143a": 5810.0,
        "HFC-152": 21.5,
        "HFC-152a": 164.0,
        "HFC-161": 4.84,
        "HFC-227ca": 2980.0,
        "HFC-227ea": 3600.0,
        "HFC-236cb": 1350.0,
        "HFC-236ea": 1500.0,
        "HFC-236fa": 8690.0,
        "HFC-245ca": 787.0,
        "HFC-245cb": 4550.0,
        "HFC-245ea": 255.0,
        "HFC-245eb": 325.0,
        "HFC-245fa": 962.0,
        "HFC-263fb": 74.8,
        "HFC-272ca": 599.0,
        "HFC-329p": 2890.0,
        "HFC-365mfc": 914.0,
        "HFC-43-10mee": 1600.0,
        # --- Perfluorocarbons (PFCs) / fully fluorinated species ---------------------
        "PFC-14": 7380.0,        # CF4
        "PFC-116": 12400.0,      # C2F6
        "PFC-218": 9290.0,       # C3F8
        "PFC-318": 10200.0,      # c-C4F8
        "PFC-31-10": 10000.0,    # n-C4F10
        "PFC-41-12": 9220.0,     # n-C5F12
        "PFC-51-14": 8620.0,     # n-C6F14
        "PFC-61-16": 8410.0,     # n-C7F16
        "PFC-71-18": 8260.0,     # n-C8F18
        "PFC-91-18": 7480.0,     # C10F18
        "SF5CF3": 18500.0,       # trifluoromethyl sulfur pentafluoride
        "SO2F2": 4630.0,         # sulfuryl fluoride
    },
}

# NOTE ON THE BARE "CH4" KEY: the IPCC publishes only fossil and non-fossil CH4.
# The bare key is this PLATFORM's default for a factor whose ``ch4_origin`` is NULL,
# not an IPCC figure, and it is deliberately left at its historical value here: it is
# reachable today, so changing it would move existing customers' published totals.
# Declaring ``ch4_origin`` on the factor is what makes a line use a cited IPCC value.

# --- Refrigerant blends -----------------------------------------------------------
#
# Refrigerants are sold, logged and leaked as BLENDS, not as pure species: nobody
# buys a cylinder of HFC-125. A blend is a mass mixture, so its GWP is the
# mass-weighted sum of its components' GWPs — which means a blend's GWP is DERIVED
# per GWP set rather than stored, and the AR5/AR6 switch is real for a refrigerant
# line exactly as it is for a fuel.
#
# Compositions are the ASHRAE 34 / EN 378 nominal mass fractions. Each is checked at
# import to sum to 1.0, and composing these against the component values above
# reproduces the published blend GWPs (e.g. R-410A AR5 = 1923.5, R-404A AR5 = 3942.8,
# R-407C AR5 = 1624.2, R-507A AR5 = 3985.0, R-508B AR5 = 11698.0).
#
# Only blends composed ENTIRELY of gases in GWP_100 are shipped. Blends containing a
# hydrocarbon fraction (R-413A, R-417A, R-422D, R-434A, R-438A) are deliberately
# omitted: their hydrocarbon components are not Kyoto-basket gases and the IPCC does
# not tabulate a 100-year GWP for them, so shipping a composition would require
# inventing a GWP of 0.0 for part of the mass. A preparer with such a blend records
# its components, which this schema can hold.
GAS_BLENDS: Mapping[str, Mapping[str, float]] = {
    # Single-component designations, for preparers logging by refrigerant name.
    "R-23": {"HFC-23": 1.0},
    "R-32": {"HFC-32": 1.0},
    "R-125": {"HFC-125": 1.0},
    "R-134a": {"HFC-134a": 1.0},
    "R-143a": {"HFC-143a": 1.0},
    "R-152a": {"HFC-152a": 1.0},
    # Zeotropic / azeotropic HFC blends.
    "R-404A": {"HFC-125": 0.44, "HFC-143a": 0.52, "HFC-134a": 0.04},
    "R-407A": {"HFC-32": 0.20, "HFC-125": 0.40, "HFC-134a": 0.40},
    "R-407C": {"HFC-32": 0.23, "HFC-125": 0.25, "HFC-134a": 0.52},
    "R-407F": {"HFC-32": 0.30, "HFC-125": 0.30, "HFC-134a": 0.40},
    "R-410A": {"HFC-32": 0.50, "HFC-125": 0.50},
    "R-427A": {"HFC-32": 0.15, "HFC-125": 0.25, "HFC-143a": 0.10, "HFC-134a": 0.50},
    "R-507A": {"HFC-125": 0.50, "HFC-143a": 0.50},
    "R-508B": {"HFC-23": 0.46, "PFC-116": 0.54},
}

SUPPORTED_GWP_SETS = tuple(GWP_100.keys())

# The gases whose mass lives in a dedicated column on ``emission_factors``. Everything
# else lives in ``factor_gas_masses``. The two stores must stay disjoint.
COLUMN_BACKED_GASES = frozenset({"CO2", "CH4", "CH4_fossil", "CH4_biogenic", "N2O"})

# Kyoto-basket family of each gas identity, for disclosure renderers that must report
# or test by family (the EU/UK ETS installation figure, ESRS E1 gas breakdowns).
_FAMILY_PREFIX = (("HFC-", "HFC"), ("PFC-", "PFC"))
_FAMILY_EXACT = {"CO2": "CO2", "CH4": "CH4", "CH4_fossil": "CH4", "CH4_biogenic": "CH4",
                 "N2O": "N2O", "SF6": "SF6", "NF3": "NF3",
                 # Fully fluorinated species in the AR6 table that are not Kyoto-basket
                 # gases in their own right; grouped with the PFCs for reporting.
                 "SF5CF3": "PFC", "SO2F2": "PFC"}

# The seven Kyoto-basket gas families (CO2, CH4, N2O, HFCs, PFCs, SF6, NF3).
KYOTO_FAMILIES = ("CO2", "CH4", "N2O", "HFC", "PFC", "SF6", "NF3")

# Families that are fluorinated gases — the ones an ETS/F-gas completeness check asks
# about, and the ones that were unrepresentable before per-gas storage existed.
F_GAS_FAMILIES = frozenset({"HFC", "PFC", "SF6", "NF3"})


class UnknownGwpSet(ValueError):
    pass


class UnknownGas(ValueError):
    pass


class GasStorageError(ValueError):
    """A factor's gas masses are stored inconsistently (e.g. the same gas in both
    the column store and the association table)."""


def _validate_blends() -> None:
    """Fail at IMPORT if a shipped blend is malformed.

    A blend whose fractions do not sum to 1 understates or overstates every line that
    uses it, by a factor nothing downstream can detect. A blend naming a gas with no
    GWP would raise only on the first customer who logged that refrigerant. Both are
    caught here, where the cost is a failed import rather than a wrong disclosure.
    """
    for blend, comp in GAS_BLENDS.items():
        if blend in GWP_100["AR6"]:
            raise GasStorageError(
                f"blend {blend!r} collides with a pure-species GWP key")
        total = sum(comp.values())
        if abs(total - 1.0) > 1e-9:
            raise GasStorageError(
                f"blend {blend!r} mass fractions sum to {total!r}, not 1.0")
        for gas in comp:
            for s in SUPPORTED_GWP_SETS:
                if gas not in GWP_100[s]:
                    raise GasStorageError(
                        f"blend {blend!r} names gas {gas!r} with no {s} GWP")


_validate_blends()


def is_known_gas(gas: str) -> bool:
    """True if ``gas`` is a pure species or a shipped blend in every supported set."""
    return gas in GAS_BLENDS or all(gas in GWP_100[s] for s in SUPPORTED_GWP_SETS)


def gas_family(gas: str) -> str:
    """Kyoto-basket family of ``gas`` ("CO2" | "CH4" | "N2O" | "HFC" | "PFC" | "SF6"
    | "NF3"). Fail-closed: an unrecognised identity raises rather than being silently
    grouped, which would drop it from a by-family disclosure total.

    A blend takes the family of its heaviest-GWP component family when all its
    components share one family, and "HFC" for the mixed HFC/PFC blends in use —
    both resolved from the composition, never guessed from the name.
    """
    if gas in _FAMILY_EXACT:
        return _FAMILY_EXACT[gas]
    for prefix, fam in _FAMILY_PREFIX:
        if gas.startswith(prefix):
            return fam
    if gas in GAS_BLENDS:
        fams = {gas_family(g) for g in GAS_BLENDS[gas]}
        if len(fams) == 1:
            return next(iter(fams))
        # A mixed-family blend (R-508B is HFC-23 + PFC-116) is reported under the
        # family contributing the larger share of its GWP, computed not assumed.
        by_fam: Dict[str, float] = {}
        for g, frac in GAS_BLENDS[gas].items():
            by_fam[gas_family(g)] = by_fam.get(gas_family(g), 0.0) + frac * GWP_100["AR6"][g]
        return max(by_fam.items(), key=lambda kv: kv[1])[0]
    raise UnknownGas(f"no Kyoto family for gas {gas!r}")


def is_f_gas(gas: str) -> bool:
    """True if ``gas`` is a fluorinated gas (HFC, PFC, SF6 or NF3)."""
    return gas_family(gas) in F_GAS_FAMILIES


def gwp(gas: str, gwp_set: str = "AR6") -> float:
    """GWP-100 for ``gas`` under ``gwp_set``. Fail-closed on unknown inputs.

    ``gas`` may be a pure species ("HFC-134a") or a shipped refrigerant blend
    ("R-410A"), whose GWP is composed from its components' values under the SAME
    set — so a blend's figure moves with the GWP vintage like any other.
    """
    if gwp_set not in GWP_100:
        raise UnknownGwpSet(f"unknown GWP set {gwp_set!r}; supported: {SUPPORTED_GWP_SETS}")
    table = GWP_100[gwp_set]
    if gas in table:
        return table[gas]
    if gas in GAS_BLENDS:
        return sum(frac * table[g] for g, frac in GAS_BLENDS[gas].items())
    raise UnknownGas(f"no GWP for gas {gas!r} in {gwp_set}")


def co2e_from_gases(gas_masses: Mapping[str, float], gwp_set: str = "AR6") -> float:
    """kg CO2e = sum(mass_gas_kg * GWP100[gas]). Fail-closed on unknown gas/set."""
    return sum(mass * gwp(gas, gwp_set) for gas, mass in gas_masses.items())


def gas_masses_for_factor(factor) -> Dict[str, float]:
    """THE per-gas mass map (kg gas per activity unit) for one emission factor.

    This is the single place that knows where a gas mass is stored. It unions the
    column-backed gases (CO2/CH4/N2O) with the ``factor_gas_masses`` association
    rows that hold every other gas — F-gases included — so a caller never has to
    remember that there are two stores, and a gas added to the association table
    reaches the calculation with no code change.

    CH4 is routed to the correct GWP variant by the factor's ``ch4_origin``: fossil
    fugitive/process vs. non-fossil (combustion, landfill, organic); NULL uses the
    platform default. Per the GHG Protocol's methane instruction, COMBUSTION methane
    takes the non-fossil value.

    Fail-closed on inconsistent storage: an association row naming a column-backed
    gas raises ``GasStorageError``. Silently preferring one store would either double
    count that gas or drop it, and nothing downstream could detect either.
    """
    gases: Dict[str, float] = {}
    if getattr(factor, "kg_co2", None) is not None:
        gases["CO2"] = factor.kg_co2
    if getattr(factor, "kg_ch4", None) is not None:
        origin = getattr(factor, "ch4_origin", None)
        key = {"fossil": "CH4_fossil", "biogenic": "CH4_biogenic"}.get(origin, "CH4")
        gases[key] = factor.kg_ch4
    if getattr(factor, "kg_n2o", None) is not None:
        gases["N2O"] = factor.kg_n2o

    for row in (getattr(factor, "gas_masses", None) or []):
        gas = row.gas
        if gas in COLUMN_BACKED_GASES:
            raise GasStorageError(
                f"factor {getattr(factor, 'id', '?')} stores {gas!r} in "
                f"factor_gas_masses, but {gas!r} is column-backed (kg_co2/kg_ch4/"
                f"kg_n2o). One gas, one store — two would double count or drop it.")
        if gas in gases:
            raise GasStorageError(
                f"factor {getattr(factor, 'id', '?')} has duplicate gas {gas!r}")
        gases[gas] = row.kg_per_unit
    return gases
