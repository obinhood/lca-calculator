"""SFDR Principal Adverse Impact (PAI) climate indicators, over the PCAF data.

  PAI 1 — GHG emissions (financed Scope 1, 2, 3, total).
  PAI 2 — Carbon footprint = total financed emissions / current value of
          investments (EUR millions), tCO2e per EUR million invested.
  PAI 3 — GHG intensity of investee companies = value-weighted average of each
          investee's own emissions / revenue (EUR millions).

Fail-closed: PAI 2/3 need a portfolio value; PAI 3 skips positions lacking
investee revenue and discloses the coverage.

Both PAI 2 and PAI 3 are DENOMINATED indicators (per EUR million), so the money in them
has to be in one currency. Positions are held in their own, and the portfolio value is
supplied by the caller — where a conversion to EUR cannot be performed at a loaded rate,
the indicator is refused rather than computed across mixed currencies.
"""
import math
from typing import Optional

from sqlalchemy.orm import Session

from ..models import FinancedPosition
from ..services.pcaf import (portfolio_financed, attribution_factor,
                             amounts_by_currency, fx_lineage)

# SFDR RTS Annex I states PAI 2 per EUR million invested and PAI 3 per EUR million of
# investee revenue. EUR is therefore the indicator's unit, not a house convention.
PAI_CURRENCY = "EUR"


def _position_year(p) -> Optional[int]:
    raw = (p.as_of_date or "")[:4]
    return int(raw) if raw.isdigit() else None


def _pai3_weights(db: Session, weights: list) -> dict:
    """Value-weight the investee intensities into ONE currency, or refuse PAI 3.

    `weights` is [(currency, outstanding, intensity, year)]. A weighted average over
    amounts in different currencies is not a weighted average of anything: 1,000,000 JPY
    counted equal to 1,000,000 USD over-weights the JPY investee ~150x. With a single
    currency the weights are a RATIO, so the result is invariant to which currency it is
    and no rate is needed.
    """
    by_ccy = amounts_by_currency((c, w) for c, w, _i, _y in weights)
    if not weights:
        return {"value": None, "reason": None, "currency": None, "conversions": None}
    if list(by_ccy) == [None]:
        return {"value": None, "currency": None, "conversions": None,
                "reason": "the positions record no currency — they cannot be value-weighted"}
    if len(by_ccy) == 1:
        num = sum(w * i for _c, w, i, _y in weights)
        den = sum(w for _c, w, _i, _y in weights)
        return {"value": (round(num / den, 6) if den else None),
                "currency": list(by_ccy)[0], "conversions": None, "reason": None}
    num = den = 0.0
    conversions = []
    for ccy, amount, intensity, year in weights:
        fx = fx_lineage(db, ccy, PAI_CURRENCY, year)
        if "reason" in fx:
            return {"value": None, "currency": None, "conversions": None,
                    "reason": (f"positions span "
                               f"{', '.join(sorted(str(c or 'no currency') for c in by_ccy))} "
                               f"and must be weighted in {PAI_CURRENCY}, but {fx['reason']}")}
        w = amount * fx["rate"]
        num += w * intensity
        den += w
        if ccy != PAI_CURRENCY:
            conversions.append({"from": ccy, "to": PAI_CURRENCY, "rate": fx["rate"],
                                "fx_rate_id": fx["fx_rate_id"],
                                "fx_rate_inverted": fx["fx_rate_inverted"],
                                "fx_year": fx["fx_year"]})
    return {"value": (round(num / den, 6) if den else None), "currency": PAI_CURRENCY,
            "conversions": conversions or None, "reason": None}


def sfdr_pai_report(db: Session, organisation_id: int,
                    portfolio_value_millions: Optional[float] = None,
                    include_scope3: bool = True,
                    portfolio_value_currency: str = PAI_CURRENCY,
                    fx_year: Optional[int] = None) -> dict:
    pcaf = portfolio_financed(db, organisation_id, include_scope3=include_scope3)
    financed = pcaf["financed_emissions_tco2e"]

    blockers = []
    pv_ok = (portfolio_value_millions is not None
             and math.isfinite(portfolio_value_millions) and portfolio_value_millions > 0)
    if not pv_ok:
        blockers.append("portfolio_value_millions required (finite, > 0) for PAI 2/3")

    positions = db.query(FinancedPosition).filter(
        FinancedPosition.organisation_id == organisation_id).all()

    # PAI 3: value-weighted average investee GHG intensity (own emissions/revenue).
    weights = []
    n_with_revenue = 0
    # `investee_scope3_tco2e` is nullable precisely so "not reported" and "zero" stay
    # distinguishable. Coercing NULL to 0.0 keeps the position in the average at an
    # understated intensity — so the coercion is COUNTED and disclosed rather than
    # silently applied, and a reader can see how much of PAI 3 rests on absent data.
    n_scope3_missing = 0
    for p in positions:
        if p.investee_revenue_millions and p.investee_revenue_millions > 0:
            if include_scope3 and p.investee_scope3_tco2e is None:
                n_scope3_missing += 1
            s3 = (p.investee_scope3_tco2e or 0.0) if include_scope3 else 0.0
            own = (p.investee_scope1_tco2e or 0.0) + (p.investee_scope2_tco2e or 0.0) + s3
            intensity = own / p.investee_revenue_millions
            weights.append((p.currency, p.outstanding_amount, intensity,
                            fx_year or _position_year(p)))
            n_with_revenue += 1
    pai3w = _pai3_weights(db, weights)
    pai3 = pai3w["value"]
    if pai3 is None and pai3w["reason"]:
        blockers.append(f"PAI 3 refused: {pai3w['reason']}")

    # PAI 2: the denominator is the caller's portfolio value, and the indicator is stated
    # per EUR million — so a value supplied in another currency has to be converted or
    # the indicator refused. It was previously labelled EUR whatever was passed in.
    pv_ccy = (portfolio_value_currency or "").strip().upper() or None
    pv_eur, pv_fx = None, None
    if pv_ok:
        if pv_ccy is None:
            blockers.append(f"portfolio_value_currency required for PAI 2 — the indicator "
                            f"is per {PAI_CURRENCY} million invested and an undeclared "
                            f"currency cannot be assumed to be {PAI_CURRENCY}")
        else:
            # The rate year: the caller's, else the latest year the portfolio describes.
            _years = [y for y in (_position_year(p) for p in positions) if y]
            _pv_year = fx_year or (max(_years) if _years else None)
            _fx = fx_lineage(db, pv_ccy, PAI_CURRENCY, _pv_year)
            if "reason" in _fx:
                blockers.append(f"PAI 2 refused: portfolio_value_millions is in {pv_ccy} "
                                f"and PAI 2 is per {PAI_CURRENCY} million — {_fx['reason']}")
            else:
                pv_eur = portfolio_value_millions * _fx["rate"]
                pv_fx = None if pv_ccy == PAI_CURRENCY else _fx

    return {
        # Echoed because it CHANGES PAI 1, 2 and 3. Two reports generated with
        # different values for it are not comparable, and without this a reader
        # cannot tell which one they are holding.
        "include_scope3": bool(include_scope3),
        "pai3_data_coverage": {
            "positions_with_revenue": n_with_revenue,
            "investee_scope3_not_reported": n_scope3_missing,
            "note": (f"{n_scope3_missing} of {n_with_revenue} position(s) in PAI 3 have "
                     f"NO reported investee Scope 3 and were treated as zero, which "
                     f"understates the weighted intensity — 'not reported' is not 'zero'."
                     if n_scope3_missing else
                     "every position in PAI 3 carries reported investee Scope 3 data")
            if include_scope3 else "Scope 3 excluded from PAI 3 by request",
            "scope": "PAI 3 only. PAI 1 and PAI 2 come from the PCAF engine, which "
                     "applies the same NULL-as-zero coercion over ALL positions "
                     "(including those without revenue, which PAI 3 excludes) and does "
                     "not yet count it — so their Scope 3 component may also rest on "
                     "absent data.",
        },
        "framework": "SFDR Principal Adverse Impacts (climate)",
        "ok": not blockers,
        "blockers": blockers,
        "pai_1_ghg_emissions_tco2e": {
            "scope1": financed["scope1"], "scope2": financed["scope2"],
            "scope3": financed["scope3"], "total": financed["total"],
            "note": "Financed emissions attributed to the portfolio (PCAF).",
        },
        "pai_2_carbon_footprint": ({
            "tco2e_per_eur_million_invested": round(financed["total"] / pv_eur, 6),
            "portfolio_value_millions": portfolio_value_millions,
            # As SUPPLIED, and the EUR figure the indicator was actually divided by. A
            # denominator in another currency used to be labelled EUR unchecked.
            "portfolio_value_currency": pv_ccy,
            "portfolio_value_millions_eur": round(pv_eur, 6),
            "portfolio_value_fx": pv_fx,
        } if (pv_ok and pv_eur) else None),
        "pai_3_ghg_intensity_of_investees": {
            "value_weighted_tco2e_per_eur_million_revenue": pai3,
            "positions_with_revenue": n_with_revenue,
            "positions_total": len(positions),
            "coverage_note": "Positions without investee revenue are excluded from PAI 3.",
            # The currency the value weights were expressed in, and the rates applied to
            # get them there. None when the average was refused (see blockers).
            "weighting_currency": pai3w["currency"],
            "weighting_conversions": pai3w["conversions"],
            "refused_reason": pai3w["reason"],
            "revenue_currency_note":
                f"investee_revenue_millions carries no currency in the data model and is "
                f"taken as {PAI_CURRENCY} millions, as the indicator requires. A revenue "
                f"figure recorded in another currency would scale that investee's "
                f"intensity — record revenue in {PAI_CURRENCY} millions.",
        },
        "pcaf_weighted_data_quality_score": pcaf["weighted_data_quality_score"],
        "note": "PAI climate indicators derived from PCAF financed emissions; "
                "verify against the current SFDR RTS templates before filing.",
    }
