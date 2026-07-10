"""Fail-closed unit conversion for the calculation engine.

An activity quantity MUST be converted into the emission factor's unit *before*
multiplication. We never assume units already match and never silently coerce
incompatible dimensions — an unconvertible pair raises ``UnitConversionError`` so
the row is routed to review instead of producing a silently-wrong number (Gap 1).

Fail-closed also means rejecting inputs pint would *accept but mis-handle*:
  * non-finite / non-numeric quantities (``inf``/``NaN``/``None``/"abc") which would
    poison downstream aggregates;
  * ambiguous units whose pint default is a trap — e.g. bare ``ton`` resolves to the
    US short ton (907 kg), not the metric tonne (1000 kg), a silent ~9% error;
  * unit strings carrying stray punctuation (``"kWh!"``) that pint's tokenizer would
    quietly swallow.

Dimensional conversions (kWh<->MWh, km<->mile, L<->m3, kg<->tonne) are handled by
pint. Bridges that require a physical constant (gas volume -> energy via calorific
value, fuel volume -> mass via density) are intentionally NOT auto-applied: they
are incommensurable to pint and are therefore rejected here until an explicit,
audited bridge is supplied (Phase 2).
"""
from __future__ import annotations

import math
import re
from typing import Optional

import pint


class UnitConversionError(ValueError):
    """Raised when a quantity cannot be safely converted to a target unit."""


class QuantityError(UnitConversionError):
    """Raised when the quantity itself is invalid (None, non-numeric, non-finite).

    Subclasses ``UnitConversionError`` so existing ``except UnitConversionError``
    handlers still catch it, while callers that want to distinguish a missing/bad
    quantity from a unit mismatch can catch this first.
    """


def _build_registry() -> pint.UnitRegistry:
    ureg = pint.UnitRegistry()
    # Transport activity units are absent from pint's default registry.
    ureg.define("passenger = [passenger]")
    ureg.define("pkm = passenger * kilometer = passenger_kilometer")
    ureg.define("tkm = metric_ton * kilometer = tonne_kilometer")
    return ureg


_UREG = _build_registry()

# Common spellings normalised to pint-parseable tokens. NB: bare "ton"/"tons" are
# deliberately NOT aliased — they are rejected as ambiguous (see _AMBIGUOUS).
_ALIASES = {
    "kwh": "kWh", "mwh": "MWh", "gwh": "GWh", "wh": "Wh",
    "l": "L", "litre": "L", "liter": "L", "litres": "L", "liters": "L",
    "m3": "m**3", "m^3": "m**3",
    "mi": "mile", "miles": "mile",
    "tonne": "metric_ton", "tonnes": "metric_ton", "t": "metric_ton", "te": "metric_ton",
}

# Units whose pint default silently disagrees with GHG-accounting convention.
_AMBIGUOUS = {"ton", "tons", "gal", "gallon", "gallons", "mt"}

# Currency codes (ISO 4217 subset) for spend-based EEIO factors. Currencies must
# NEVER reach pint's dimensional analysis: mis-cased codes collide with real pint
# units ("Gbp" = gigapoint, "myr" = milliyear, "php" = picohorsepower) and would
# produce a silently WRONG number instead of a rejection. Currency handling is
# identity-or-reject: same code (case-insensitive) converts 1:1; anything else
# needs an audited FX rate and fails closed.
_CURRENCIES = {
    "GBP", "EUR", "USD", "CHF", "JPY", "CNY", "AUD", "CAD", "SEK", "NOK", "DKK",
    "PLN", "CZK", "HUF", "INR", "BRL", "MXN", "ZAR", "SGD", "HKD", "NZD", "KRW",
    "TRY", "MYR", "PHP", "THB", "IDR", "AED", "SAR", "ILS",
}

# A well-formed unit token: letters, digits, and operator/separator characters only.
_ALLOWED_UNIT_RE = re.compile(r"^[A-Za-z0-9_./*^+\- ]+$")


def _normalise(unit: str) -> str:
    u = (unit or "").strip()
    return _ALIASES.get(u.lower(), u)


def convert(quantity: Optional[float], from_unit: str, to_unit: str) -> float:
    """Return ``quantity`` expressed in ``to_unit``. Fail-closed on any incompatibility."""
    # 1. The quantity must be a finite number — reject None / "abc" / inf / NaN.
    try:
        q = float(quantity)
    except (TypeError, ValueError):
        raise QuantityError(f"non-numeric quantity: {quantity!r}")
    if not math.isfinite(q):
        raise QuantityError(f"non-finite quantity: {quantity!r}")

    # 2. The units must be present, unambiguous, and well-formed.
    raw_from, raw_to = (from_unit or "").strip(), (to_unit or "").strip()
    if not raw_from or not raw_to:
        raise UnitConversionError(f"missing unit: from={from_unit!r} to={to_unit!r}")

    # 2a. Currency short-circuit (identity-or-reject; never pint).
    cur_from, cur_to = raw_from.upper() in _CURRENCIES, raw_to.upper() in _CURRENCIES
    if cur_from or cur_to:
        if cur_from and cur_to and raw_from.upper() == raw_to.upper():
            return q
        raise UnitConversionError(
            f"cannot convert {from_unit!r} -> {to_unit!r}: currency conversion "
            f"requires an audited FX rate (and currencies never mix with physical units)")
    for raw in (raw_from, raw_to):
        if raw.lower() in _AMBIGUOUS:
            raise UnitConversionError(
                f"ambiguous unit {raw!r}; use an explicit unit (e.g. 'tonne', 'kg', 'L')"
            )
    fu, tu = _normalise(raw_from), _normalise(raw_to)
    for raw, norm in ((raw_from, fu), (raw_to, tu)):
        if not _ALLOWED_UNIT_RE.match(norm):
            raise UnitConversionError(f"unit contains invalid characters: {raw!r}")

    # 3. Convert (fast path for identical units — q is already validated as finite).
    if fu == tu:
        return q
    try:
        return float(_UREG.Quantity(q, fu).to(tu).magnitude)
    except Exception as exc:  # DimensionalityError, UndefinedUnitError, TokenError, etc.
        raise UnitConversionError(
            f"cannot convert {from_unit!r} -> {to_unit!r}: {exc.__class__.__name__}"
        ) from exc


def are_compatible(from_unit: str, to_unit: str) -> bool:
    """True if a value in ``from_unit`` can be safely converted to ``to_unit``."""
    try:
        convert(1.0, from_unit, to_unit)
        return True
    except UnitConversionError:
        return False
