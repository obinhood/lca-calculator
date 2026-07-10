"""EU CBAM (Carbon Border Adjustment Mechanism) embedded-emissions engine.

Definitive period (from 2026): importers of covered goods (cement, iron/steel,
aluminium, fertilisers, hydrogen, electricity) declare the embedded emissions
of each imported goods line and surrender certificates against them.

Rules encoded here, fail-closed:
  * VERIFIED actual installation values are used when present (basis "actual").
  * UNVERIFIED actuals are never used — CBAM requires accredited verification;
    the line falls back to default values and the substitution is flagged.
  * No default match and no verified actuals -> the line is an ERROR, surfaced
    in the declaration and blocking readiness (never silently skipped).
  * Certificates due = embedded emissions reduced pro-rata by a carbon price
    effectively paid in the origin country, floored at zero (simplified
    deduction, labelled as such — the official formula follows the
    implementing regulation).
Default matching is by LONGEST CN-code prefix for the import year (falling
back to the latest earlier vintage), deterministic.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CbamDefaultValue, CbamGood


class CbamResolutionError(ValueError):
    """A goods line has no usable emissions basis (fail-closed)."""


def resolve_default(db: Session, cn_code: str, year: int) -> Optional[CbamDefaultValue]:
    """Longest-prefix default for a CN code; latest vintage <= year, else None."""
    candidates = db.query(CbamDefaultValue)\
        .filter(CbamDefaultValue.valid_year <= year).all()
    matches = [d for d in candidates if cn_code.startswith(d.cn_code_prefix)]
    if not matches:
        return None
    matches.sort(key=lambda d: (len(d.cn_code_prefix), d.valid_year, d.id), reverse=True)
    return matches[0]


def line_embedded(db: Session, good: CbamGood) -> dict:
    """Embedded emissions for one goods line, with basis + lineage."""
    year = int(good.import_date[:4]) if good.import_date else 0
    has_actuals = (good.actual_direct_t_per_t is not None
                   and good.actual_indirect_t_per_t is not None)

    if has_actuals and good.actual_verified:
        direct, indirect = good.actual_direct_t_per_t, good.actual_indirect_t_per_t
        basis = "actual_verified"
        default_used = None
        note = None
    else:
        default = resolve_default(db, good.cn_code, year)
        if default is None:
            raise CbamResolutionError(
                f"no default value for CN {good.cn_code} (year {year}) and no "
                f"verified actual values — line cannot be declared")
        direct, indirect = default.direct_t_co2e_per_t, default.indirect_t_co2e_per_t
        basis = "default"
        default_used = default.id
        note = ("actual values present but NOT verified — defaults substituted "
                "(CBAM requires accredited verification)") if has_actuals else None

    return {
        "good_id": good.id,
        "cn_code": good.cn_code,
        "origin_country": good.origin_country,
        "quantity_tonnes": good.quantity_tonnes,
        "basis": basis,
        "default_value_id": default_used,
        "direct_t_per_t": direct,
        "indirect_t_per_t": indirect,
        "embedded_direct_t": good.quantity_tonnes * direct,
        "embedded_indirect_t": good.quantity_tonnes * indirect,
        "embedded_total_t": good.quantity_tonnes * (direct + indirect),
        "note": note,
    }


def certificates_due(embedded_t: float, carbon_price_paid_eur_per_t: Optional[float],
                     ets_price_eur_per_t: float) -> float:
    """Certificates due after the origin-country carbon-price deduction.

    Simplified pro-rata deduction: paying the full ETS-equivalent price abroad
    zeroes the obligation; a partial price reduces it proportionally. Floored
    at zero, never negative.
    """
    if not carbon_price_paid_eur_per_t or ets_price_eur_per_t <= 0:
        return embedded_t
    reduction = min(1.0, carbon_price_paid_eur_per_t / ets_price_eur_per_t)
    return embedded_t * (1.0 - reduction)
