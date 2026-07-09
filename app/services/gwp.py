"""Global Warming Potential tables (100-year) and per-gas CO2e aggregation.

This module is the *foundation* for applying GWP at calculation time from per-gas
masses (kg CO2 / kg CH4 / kg N2O), which is what will make the AR5/AR6 switch real
rather than cosmetic (Gap 2). NOTE: it is not yet wired into ``calc.compute_co2e``
— the demo emission factors are still pre-aggregated kg CO2e per unit, so the
calculation multiplies that flat value. Per-gas factors + wiring land in Phase 2;
until then ``compute_co2e`` only *validates* that a factor's ``gwp_set`` matches the
requested set, and this module is exercised directly by its own tests.

Values are the IPCC 100-year GWPs:

  * AR5: WG1 Ch.8, Table 8.7 (values without climate-carbon feedbacks — the set
    used by the GHG Protocol and by DEFRA/DESNZ conversion factors).
  * AR6: WG1 Ch.7, Table 7.15.

Fossil vs. biogenic CH4 differ because oxidised biogenic methane returns carbon
that was recently in the atmosphere; keep them distinct for auditable reporting.
"""
from __future__ import annotations

from typing import Mapping

# kg CO2e per kg of gas, 100-year horizon.
GWP_100: Mapping[str, Mapping[str, float]] = {
    "AR5": {
        "CO2": 1.0,
        "CH4": 28.0,          # generic / biogenic
        "CH4_fossil": 30.0,
        "CH4_biogenic": 28.0,
        "N2O": 265.0,
        "SF6": 23500.0,
        "NF3": 16100.0,
    },
    "AR6": {
        "CO2": 1.0,
        "CH4": 27.9,          # blended
        "CH4_fossil": 29.8,
        "CH4_biogenic": 27.0,  # IPCC AR6 Table 7.15 "methane, non-fossil"
        "N2O": 273.0,
        "SF6": 25200.0,
        "NF3": 17400.0,
    },
}

SUPPORTED_GWP_SETS = tuple(GWP_100.keys())


class UnknownGwpSet(ValueError):
    pass


class UnknownGas(ValueError):
    pass


def gwp(gas: str, gwp_set: str = "AR6") -> float:
    """GWP-100 for ``gas`` under ``gwp_set``. Fail-closed on unknown inputs."""
    if gwp_set not in GWP_100:
        raise UnknownGwpSet(f"unknown GWP set {gwp_set!r}; supported: {SUPPORTED_GWP_SETS}")
    table = GWP_100[gwp_set]
    if gas not in table:
        raise UnknownGas(f"no GWP for gas {gas!r} in {gwp_set}")
    return table[gas]


def co2e_from_gases(gas_masses: Mapping[str, float], gwp_set: str = "AR6") -> float:
    """kg CO2e = sum(mass_gas_kg * GWP100[gas]). Fail-closed on unknown gas/set."""
    return sum(mass * gwp(gas, gwp_set) for gas, mass in gas_masses.items())
