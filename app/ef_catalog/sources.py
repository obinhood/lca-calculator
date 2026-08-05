"""Emission-factor SOURCE registry — provenance and licensing, in one auditable place.

Each entry pins WHERE a published factor set comes from, under WHAT licence, the attribution
the licence requires, and WHICH parser turns its file into normalised FactorRows. The refresh
layer (refresh.py) fetches a file for a source and loads it under a pinned (source, version),
so every factor in the catalog traces back to a documented, licensed origin — the same
provenance discipline the rest of the platform applies to numbers.

This registry is metadata only: importing it makes NO network calls. A fetch happens only when
an operator runs the refresh (scripts/refresh_factors.py). The `url` is the official
publication page for provenance; the operator supplies the exact file (`--url`/`--file`) to
load, and asserts the licence for any operator-supplied ("generic") file.
"""
from dataclasses import dataclass
from typing import Callable, List

from .loaders.base import FactorRow
from .loaders.defra import parse_defra_flat_csv
from .loaders.useeio import parse_useeio_csv
from .loaders.generic import parse_generic_csv


@dataclass(frozen=True)
class FactorSource:
    key: str
    name: str
    publisher: str
    url: str                                   # official publication page (provenance)
    license: str
    attribution: str                           # required attribution string, if any
    default_source: str                        # the `source` value stored on each factor
    parse: Callable[[bytes], List[FactorRow]]  # bytes -> normalised rows


SOURCES = {
    "defra": FactorSource(
        key="defra",
        name="UK Government GHG conversion factors for company reporting",
        publisher="UK DESNZ / DEFRA",
        url="https://www.gov.uk/government/collections/"
            "government-conversion-factors-for-company-reporting",
        license="Open Government Licence v3.0 (OGL)",
        attribution="Contains public sector information licensed under the Open Government "
                    "Licence v3.0.",
        default_source="DEFRA_DESNZ",
        parse=parse_defra_flat_csv),
    "useeio": FactorSource(
        key="useeio",
        name="USEEIO spend-based (EEIO) factors",
        publisher="US Environmental Protection Agency",
        url="https://www.epa.gov/land-research/"
            "us-environmentally-extended-input-output-useeio-models",
        license="Public domain (U.S. Government work, 17 U.S.C. §105)",
        attribution="U.S. EPA USEEIO — public domain.",
        default_source="USEEIO",
        # USEEIO factors are per-purchaser-price by default; an operator can re-run with a
        # basic-price file if they need that basis.
        parse=lambda b: parse_useeio_csv(b, price_basis="purchaser")),
    "generic": FactorSource(
        key="generic",
        name="Generic normalised CSV (operator-supplied)",
        publisher="(operator-supplied)",
        url="(operator-supplied file)",
        license="(as asserted by the operator for the supplied file)",
        attribution="(operator-supplied)",
        default_source="GENERIC",
        parse=parse_generic_csv),
}
