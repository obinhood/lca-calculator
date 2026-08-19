"""PCAF financed-emissions engine (Global GHG Accounting Standard for Financials).

Financed emissions per position = attribution factor x investee emissions.
Attribution factor = outstanding / denominator (EVIC, total equity+debt, or
property value depending on asset class — same currency, dimensionless ratio).
Portfolio totals are reported by asset class with an emissions-weighted PCAF
data-quality score (1 best .. 5 proxy).

The attribution factor is currency-SAFE by construction: numerator and denominator are
the same position's own currency, so the ratio is dimensionless. Anything that sums
MONEY across positions is not — see the exposure helpers at the bottom of this module.
"""
from typing import List, Optional

from sqlalchemy.orm import Session

from ..models import FinancedPosition

ASSET_CLASSES = {
    "listed_equity": "outstanding / EVIC (enterprise value incl. cash)",
    "corporate_bonds": "outstanding / EVIC",
    "business_loans": "outstanding / (total equity + debt)",
    "project_finance": "outstanding / total project equity + debt",
    "commercial_real_estate": "outstanding / property value at origination",
    "mortgages": "outstanding / property value at origination",
    "motor_vehicle_loans": "outstanding / total value at origination",
}


def attribution_factor(pos: FinancedPosition) -> float:
    return pos.outstanding_amount / pos.attribution_denominator


def position_financed(pos: FinancedPosition, include_scope3: bool) -> dict:
    af = attribution_factor(pos)
    s1 = pos.investee_scope1_tco2e or 0.0
    s2 = pos.investee_scope2_tco2e or 0.0
    s3 = (pos.investee_scope3_tco2e or 0.0) if include_scope3 else 0.0
    return {
        "position_id": pos.id, "investee": pos.investee_name,
        "asset_class": pos.asset_class, "attribution_factor": round(af, 6),
        "financed_scope1_tco2e": round(af * s1, 6),
        "financed_scope2_tco2e": round(af * s2, 6),
        "financed_scope3_tco2e": round(af * s3, 6),
        "financed_total_tco2e": round(af * (s1 + s2 + s3), 6),
        "data_quality_score": pos.data_quality_score,
        "attribution_over_100pct": af > 1.0 + 1e-9,
    }


def portfolio_financed(db: Session, organisation_id: int, include_scope3: bool = True,
                       as_of: Optional[str] = None) -> dict:
    base = db.query(FinancedPosition).filter(
        FinancedPosition.organisation_id == organisation_id)
    n_available = base.count()
    q = base
    if as_of is not None:
        # AT OR BEFORE the cutoff — not an exact-date match. Exact match (== as_of)
        # silently returned an EMPTY portfolio whenever the cutoff didn't equal a
        # position's stored date, making a bank's entire financed footprint vanish
        # with no error. <= takes the positions established by the reporting date.
        q = q.filter(FinancedPosition.as_of_date <= as_of)
    positions = q.order_by(FinancedPosition.id).all()

    lines = [position_financed(p, include_scope3) for p in positions]
    by_asset: dict = {}
    total = {"scope1": 0.0, "scope2": 0.0, "scope3": 0.0, "total": 0.0}
    dq_weighted = 0.0
    warnings = []
    for ln in lines:
        by_asset.setdefault(ln["asset_class"], 0.0)
        by_asset[ln["asset_class"]] += ln["financed_total_tco2e"]
        total["scope1"] += ln["financed_scope1_tco2e"]
        total["scope2"] += ln["financed_scope2_tco2e"]
        total["scope3"] += ln["financed_scope3_tco2e"]
        total["total"] += ln["financed_total_tco2e"]
        dq_weighted += ln["financed_total_tco2e"] * ln["data_quality_score"]
        if ln["attribution_over_100pct"]:
            warnings.append(f"position {ln['position_id']} ({ln['investee']}) has "
                            f"attribution factor > 100% (outstanding exceeds denominator)")

    # PCAF: report the emissions-weighted data-quality score.
    dq_score = round(dq_weighted / total["total"], 3) if total["total"] else None

    return {
        "framework": "PCAF financed emissions",
        "positions": len(positions),
        "positions_available": n_available,
        "as_of": as_of,
        # True when an as_of cutoff excluded EVERY position although the org holds
        # some — the caller must treat this as an error, not a zero footprint.
        "as_of_filtered_empty": bool(as_of is not None and n_available > 0 and not positions),
        "include_scope3": include_scope3,
        "financed_emissions_tco2e": {k: round(v, 6) for k, v in total.items()},
        "by_asset_class_tco2e": {k: round(v, 6) for k, v in by_asset.items()},
        "weighted_data_quality_score": dq_score,
        "data_quality_scale": "1 best (verified) .. 5 proxy (PCAF Data Quality Score)",
        "lines": lines,
        "warnings": warnings,
        "note": "Attribution factor = outstanding / denominator by asset class; "
                "financed = attribution x investee emissions.",
    }


# --- money across positions -------------------------------------------------------
# Each position is held in its OWN currency, so a SUM of positions is only a quantity
# once every amount is in one currency. Ignoring `currency` is not a neutral
# simplification — it is a conversion at rate 1.0, and it disclosed a portfolio of
# 1,000,000 JPY + 1,000,000 USD as 2,000,000 of covered exposure against a 1,500,000
# USD gross: a "133% of gross exposure covered" that a reader cannot even recognise as
# impossible when the true figure is ~67%.
#
# Doctrine, identical to services/applicability._convert: where the conversion cannot be
# performed, REFUSE the figure and say why. A guessed rate produces a wrong number that
# reads as an authoritative one, which is strictly worse than an absent one.

def amounts_by_currency(items) -> dict:
    """``{currency: total}`` from ``(currency, amount)`` pairs.

    A missing/blank currency keys as None — "unknown", which is never silently folded
    into the target currency (an unknown is never a match).
    """
    out: dict = {}
    for ccy, amount in items:
        key = (ccy or "").strip().upper() or None
        out[key] = out.get(key, 0.0) + (amount or 0.0)
    return out


def fx_lineage(db: Session, have: Optional[str], want: Optional[str],
               year: Optional[int]) -> dict:
    """The FX lineage for converting `have`->`want` in `year`, or a refusal reason.

    Returns ``{"rate", "fx_rate_id", "fx_rate_inverted", "fx_year"}`` on success and
    ``{"reason": ...}`` when the conversion cannot be performed. The rate id is part of
    the answer: fx_rates is append-only (a correction INSERTs a row), so recording WHICH
    row was applied is what lets a converted figure be re-derived years later.
    """
    have = (have or "").strip().upper() or None
    want = (want or "").strip().upper() or None
    if have is None:
        return {"reason": f"the amount records no currency, and the total is in {want} — "
                          f"it cannot be converted"}
    if want is None:
        return {"reason": "no target currency"}
    if have == want:
        return {"rate": 1.0, "fx_rate_id": None, "fx_rate_inverted": False,
                "fx_year": year}
    if year is None:
        # spend.py refuses for the same reason: a rate is chosen for the year the money
        # describes, and "whatever is on file" can flip a disclosed figure.
        return {"reason": f"no year is available to choose a {have}->{want} rate for — "
                          f"the conversion cannot be dated"}
    from .spend import _fx_rate
    hit = _fx_rate(db, have, want, year)
    if hit is None:
        return {"reason": f"no {have}->{want} FX rate is loaded for {year} — the figure "
                          f"is refused rather than converted at a guessed rate "
                          f"(POST /reference/fx_rates)"}
    rate, fx_id, inverted = hit
    return {"rate": rate, "fx_rate_id": fx_id, "fx_rate_inverted": inverted,
            "fx_year": year}


def freeze_exposure_conversion(db: Session, currency: Optional[str],
                               amount: Optional[float], declared_currency: Optional[str],
                               year: Optional[int]) -> dict:
    """Frozen keys converting ONE position's exposure into the declared gross currency.

    IFRS S2 ¶B58-B63's "% of gross exposure covered" divides a sum of position exposures
    by the DECLARED gross exposure, so the two have to be in one currency. The conversion
    is frozen onto the run here — never done at render time — because the Scope 3
    renderer's reproduction contract forbids joining the live FX table: re-rendering a
    filed run must return the same percentage even after a rate is corrected.
    """
    want = (declared_currency or "").strip().upper() or None
    if want is None:
        # Nothing to convert TO. The renderer falls back to the single-currency case.
        return {"exposure_declared_currency": None}
    fx = fx_lineage(db, currency, want, year)
    if "reason" in fx:
        return {"exposure_declared_currency": want,
                "outstanding_in_declared_currency": None,
                "exposure_fx_unavailable_reason": fx["reason"]}
    return {"exposure_declared_currency": want,
            "outstanding_in_declared_currency": (amount or 0.0) * fx["rate"],
            "exposure_fx_rate": fx["rate"],
            "exposure_fx_rate_id": fx["fx_rate_id"],
            "exposure_fx_rate_inverted": fx["fx_rate_inverted"],
            "exposure_fx_year": fx["fx_year"]}
