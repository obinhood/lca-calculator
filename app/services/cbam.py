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
  * The certificate OBLIGATION differs from reported embedded emissions:
      - indirect emissions enter the obligation only for the goods categories
        listed in Regulation (EU) 2023/956 Annex II (cement, fertilisers,
        electricity); iron/steel, aluminium and hydrogen owe on direct only
        in the initial definitive period — both are still REPORTED.
      - the CBAM factor phases the obligation in as EU ETS free allocation
        phases out (~2.5% of embedded in 2026 rising to 100% by 2034).
      - a carbon price effectively paid in the origin country reduces the
        obligation pro-rata (simplified form, labelled), floored at zero.
Default matching is by LONGEST CN-code prefix for the import year (falling
back to the latest earlier vintage), deterministic; empty/blank prefixes are
never matched (an empty prefix would hijack every unknown CN code).
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CbamDefaultValue, CbamGood

# CBAM factor by declaration year: the share of embedded emissions that must be
# covered by certificates while EU ETS free allocation phases out
# (Regulation (EU) 2023/956, Art. 31 phase-in schedule; 100% from 2034).
CBAM_FACTOR = {
    2026: 0.025, 2027: 0.05, 2028: 0.10, 2029: 0.225, 2030: 0.485,
    2031: 0.61, 2032: 0.735, 2033: 0.86,
}


def cbam_factor(year: int) -> float:
    if year < 2026:
        return 0.0          # transitional period: reporting only, no certificates
    return CBAM_FACTOR.get(year, 1.0)   # 2034+ -> full obligation


# Goods whose INDIRECT (electricity) emissions are inside the certificate
# obligation in the initial definitive period (2023/956 Annex II); the others
# owe on direct emissions only, though indirect is still reported.
INDIRECT_IN_OBLIGATION = {"cement", "fertilisers", "electricity"}

# Annual de minimis: consignments up to this total mass per importer per year
# are exempt (2026 simplification package).
DE_MINIMIS_TONNES = 50.0


class CbamResolutionError(ValueError):
    """A goods line has no usable emissions basis (fail-closed)."""


def resolve_default(db: Session, cn_code: str, year: int) -> Optional[CbamDefaultValue]:
    """Longest-prefix default for a CN code; latest vintage <= year, else None."""
    code = (cn_code or "").strip()
    if not code:
        return None
    candidates = db.query(CbamDefaultValue)\
        .filter(CbamDefaultValue.valid_year <= year).all()
    matches = [d for d in candidates
               if d.cn_code_prefix and d.cn_code_prefix.strip()
               and code.startswith(d.cn_code_prefix.strip())]
    if not matches:
        return None
    matches.sort(key=lambda d: (len(d.cn_code_prefix.strip()), d.valid_year, d.id),
                 reverse=True)
    return matches[0]


def line_embedded(db: Session, good: CbamGood) -> dict:
    """Embedded emissions + certificate-obligation basis for one goods line."""
    year = int(good.import_date[:4]) if good.import_date else 0
    has_actuals = (good.actual_direct_t_per_t is not None
                   and good.actual_indirect_t_per_t is not None)

    # Category attribution always comes from the CN-code default table, even
    # when verified actuals supply the numbers (the category drives the
    # indirect-in-obligation rule).
    default = resolve_default(db, good.cn_code, year)
    category = default.good_category if default else None

    if has_actuals and good.actual_verified:
        direct, indirect = good.actual_direct_t_per_t, good.actual_indirect_t_per_t
        basis = "actual_verified"
        default_used = None
        note = None
        if category is None:
            note = ("no CN-code default match — category unattributed; indirect "
                    "emissions conservatively INCLUDED in the obligation")
    else:
        if default is None:
            raise CbamResolutionError(
                f"no default value for CN {good.cn_code} (year {year}) and no "
                f"verified actual values — line cannot be declared")
        direct, indirect = default.direct_t_co2e_per_t, default.indirect_t_co2e_per_t
        basis = "default"
        default_used = default.id
        note = ("actual values present but NOT verified — defaults substituted "
                "(CBAM requires accredited verification)") if has_actuals else None

    embedded_direct = good.quantity_tonnes * direct
    embedded_indirect = good.quantity_tonnes * indirect
    # Unknown category (verified actuals, no default row) -> conservative: include.
    indirect_in_scope = category in INDIRECT_IN_OBLIGATION if category else True
    obligation = embedded_direct + (embedded_indirect if indirect_in_scope else 0.0)

    return {
        "good_id": good.id,
        "cn_code": good.cn_code,
        "origin_country": good.origin_country,
        "quantity_tonnes": good.quantity_tonnes,
        "basis": basis,
        "good_category": category or "unattributed",
        "default_value_id": default_used,
        "direct_t_per_t": direct,
        "indirect_t_per_t": indirect,
        "embedded_direct_t": embedded_direct,
        "embedded_indirect_t": embedded_indirect,
        "embedded_total_t": embedded_direct + embedded_indirect,
        "indirect_in_obligation": indirect_in_scope,
        "obligation_basis_t": obligation,
        "note": note,
    }


def certificates_due(obligation_t: float, carbon_price_paid_eur_per_t: Optional[float],
                     ets_price_eur_per_t: float, year: int) -> float:
    """Certificates due for one line's obligation basis.

    obligation x CBAM factor (free-allocation phase-in) x (1 - pro-rata
    origin-country carbon-price reduction), floored at zero. The price
    deduction is the simplified pro-rata form, labelled as such.
    """
    due = obligation_t * cbam_factor(year)
    if carbon_price_paid_eur_per_t and ets_price_eur_per_t > 0:
        reduction = min(1.0, carbon_price_paid_eur_per_t / ets_price_eur_per_t)
        due *= (1.0 - reduction)
    return max(0.0, due)
