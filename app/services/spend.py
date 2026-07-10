"""Spend-based (EEIO) normalization: align spend to a factor's currency + base year.

An EEIO factor is kgCO2e per currency-unit at a specific base year and price
basis. Recorded spend must be:
  1. inflation-adjusted from its own year to the factor's base year (CPI ratio), then
  2. FX-converted from its currency to the factor's currency at the BASE-YEAR rate
     (not spot — GHG Protocol / EEIO practice).
before multiplying by the factor value.

Fail-closed, with no silent degradation:
  * a spend-based factor WITHOUT a base_year cannot be aligned to — reject
    (spot-FX-without-inflation is a guessed number, not an audited one);
  * an activity WITHOUT a usable year cannot be aligned FROM — reject;
  * any missing FX rate or price index — reject (``SpendNormalizationError``).

Lineage: every value used (CPI index values and row ids, FX rate, row id and
direction) is recorded in the returned steps so an assurer can re-verify the
number against the reference series as entered — not merely re-multiply a
pre-derived ratio. Reference rows are append-only; lookups take the LATEST
entry for a key (corrections insert new rows, history is preserved).
"""
from typing import Optional
from sqlalchemy.orm import Session

from ..models import FxRate, PriceIndex


class SpendNormalizationError(ValueError):
    """Spend could not be aligned to the factor's currency/base year (fail-closed)."""


def _price_index(db: Session, currency: str, year: int) -> Optional[PriceIndex]:
    return db.query(PriceIndex).filter(
        PriceIndex.currency == currency, PriceIndex.year == year)\
        .order_by(PriceIndex.id.desc()).first()


def _fx_rate(db: Session, base: str, quote: str, year: int):
    """(rate, row_id, inverted) at the latest entry for the pair/year, or None."""
    if base == quote:
        return 1.0, None, False
    row = db.query(FxRate).filter(
        FxRate.base_currency == base, FxRate.quote_currency == quote,
        FxRate.year == year).order_by(FxRate.id.desc()).first()
    if row:
        return row.rate, row.id, False
    inv = db.query(FxRate).filter(
        FxRate.base_currency == quote, FxRate.quote_currency == base,
        FxRate.year == year).order_by(FxRate.id.desc()).first()
    if inv and inv.rate:
        return 1.0 / inv.rate, inv.id, True
    return None


def normalize_spend(db: Session, amount: float, spend_currency: str, spend_year: Optional[int],
                    factor_currency: str, base_year: Optional[int]) -> dict:
    """Return {amount_in_factor_currency, steps...} or raise SpendNormalizationError."""
    spend_currency = (spend_currency or "").upper()
    factor_currency = (factor_currency or "").upper()

    # Fail-closed gates: no base year to align TO, or no spend year to align
    # FROM, means any result would be a guess.
    if base_year is None:
        raise SpendNormalizationError(
            "spend-based factor has no base_year — spend cannot be aligned to it "
            "(set base_year on the factor)")
    if spend_year is None:
        raise SpendNormalizationError(
            "activity has no usable date/year — spend cannot be inflation-adjusted "
            "to the factor's base year")

    steps = {"input_amount": amount, "spend_currency": spend_currency,
             "spend_year": spend_year, "factor_currency": factor_currency,
             "base_year": base_year}

    working = float(amount)

    # 1. Inflation-adjust to the factor's base year (in the SPEND currency's economy).
    if spend_year != base_year:
        row_from = _price_index(db, spend_currency, spend_year)
        row_to = _price_index(db, spend_currency, base_year)
        if row_from is None or row_to is None:
            raise SpendNormalizationError(
                f"missing price index for {spend_currency} in "
                f"{spend_year if row_from is None else base_year}")
        working = working * (row_to.index_value / row_from.index_value)
        steps["cpi_from"] = row_from.index_value
        steps["cpi_to"] = row_to.index_value
        steps["price_index_ids"] = [row_from.id, row_to.id]
        steps["inflation_factor"] = row_to.index_value / row_from.index_value

    # 2. FX-convert to the factor currency at the base-year rate.
    if spend_currency != factor_currency:
        hit = _fx_rate(db, spend_currency, factor_currency, base_year)
        if hit is None:
            raise SpendNormalizationError(
                f"missing FX rate {spend_currency}->{factor_currency} for {base_year}")
        rate, fx_id, inverted = hit
        working = working * rate
        steps["fx_rate"] = rate
        steps["fx_rate_id"] = fx_id
        steps["fx_rate_inverted"] = inverted
        steps["fx_year"] = base_year

    steps["amount_in_factor_currency"] = working
    return steps
