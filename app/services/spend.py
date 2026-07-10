"""Spend-based (EEIO) normalization: align spend to a factor's currency + base year.

An EEIO factor is kgCO2e per currency-unit at a specific base year and price
basis. Recorded spend must be:
  1. inflation-adjusted from its own year to the factor's base year (CPI ratio), then
  2. FX-converted from its currency to the factor's currency at the BASE-YEAR rate
     (not spot — GHG Protocol / EEIO practice).
before multiplying by the factor value.

Fail-closed: if any required FX rate or price index is missing, raise
``SpendNormalizationError`` — an audited number needs a documented rate, not a
guessed one. Same currency + same year is a pass-through (no lookup needed).
"""
from typing import Optional
from sqlalchemy.orm import Session

from ..models import FxRate, PriceIndex


class SpendNormalizationError(ValueError):
    """Spend could not be aligned to the factor's currency/base year (fail-closed)."""


def _price_index(db: Session, currency: str, year: int) -> Optional[float]:
    row = db.query(PriceIndex).filter(
        PriceIndex.currency == currency, PriceIndex.year == year).first()
    return row.index_value if row else None


def _fx_rate(db: Session, base: str, quote: str, year: int) -> Optional[float]:
    if base == quote:
        return 1.0
    row = db.query(FxRate).filter(
        FxRate.base_currency == base, FxRate.quote_currency == quote,
        FxRate.year == year).first()
    if row:
        return row.rate
    inv = db.query(FxRate).filter(
        FxRate.base_currency == quote, FxRate.quote_currency == base,
        FxRate.year == year).first()
    if inv and inv.rate:
        return 1.0 / inv.rate
    return None


def normalize_spend(db: Session, amount: float, spend_currency: str, spend_year: Optional[int],
                    factor_currency: str, base_year: Optional[int]) -> dict:
    """Return {amount_in_factor_currency, steps...} or raise SpendNormalizationError."""
    spend_currency = (spend_currency or "").upper()
    factor_currency = (factor_currency or "").upper()

    steps = {"input_amount": amount, "spend_currency": spend_currency,
             "spend_year": spend_year, "factor_currency": factor_currency,
             "base_year": base_year}

    working = float(amount)

    # 1. Inflation-adjust to the factor's base year (in the SPEND currency's economy).
    if base_year is not None and spend_year is not None and spend_year != base_year:
        idx_from = _price_index(db, spend_currency, spend_year)
        idx_to = _price_index(db, spend_currency, base_year)
        if idx_from is None or idx_to is None:
            raise SpendNormalizationError(
                f"missing price index for {spend_currency} in "
                f"{spend_year if idx_from is None else base_year}")
        working = working * (idx_to / idx_from)
        steps["inflation_factor"] = idx_to / idx_from

    # 2. FX-convert to the factor currency at the base-year rate.
    fx_year = base_year if base_year is not None else spend_year
    if spend_currency != factor_currency:
        if fx_year is None:
            raise SpendNormalizationError("cannot FX-convert without a reference year")
        rate = _fx_rate(db, spend_currency, factor_currency, fx_year)
        if rate is None:
            raise SpendNormalizationError(
                f"missing FX rate {spend_currency}->{factor_currency} for {fx_year}")
        working = working * rate
        steps["fx_rate"] = rate
        steps["fx_year"] = fx_year

    steps["amount_in_factor_currency"] = working
    return steps
