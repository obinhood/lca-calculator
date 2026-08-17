"""SFDR Principal Adverse Impact (PAI) climate indicators, over the PCAF data.

  PAI 1 — GHG emissions (financed Scope 1, 2, 3, total).
  PAI 2 — Carbon footprint = total financed emissions / current value of
          investments (EUR millions), tCO2e per EUR million invested.
  PAI 3 — GHG intensity of investee companies = value-weighted average of each
          investee's own emissions / revenue (EUR millions).

Fail-closed: PAI 2/3 need a portfolio value; PAI 3 skips positions lacking
investee revenue and discloses the coverage.
"""
import math
from typing import Optional

from sqlalchemy.orm import Session

from ..models import FinancedPosition
from ..services.pcaf import portfolio_financed, attribution_factor


def sfdr_pai_report(db: Session, organisation_id: int,
                    portfolio_value_millions: Optional[float] = None,
                    include_scope3: bool = True) -> dict:
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
    weighted_intensity = 0.0
    weight_sum = 0.0
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
            weighted_intensity += p.outstanding_amount * intensity
            weight_sum += p.outstanding_amount
            n_with_revenue += 1
    pai3 = round(weighted_intensity / weight_sum, 6) if weight_sum else None

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
            "tco2e_per_eur_million_invested": round(financed["total"] / portfolio_value_millions, 6),
            "portfolio_value_millions": portfolio_value_millions,
        } if pv_ok else None),
        "pai_3_ghg_intensity_of_investees": {
            "value_weighted_tco2e_per_eur_million_revenue": pai3,
            "positions_with_revenue": n_with_revenue,
            "positions_total": len(positions),
            "coverage_note": "Positions without investee revenue are excluded from PAI 3.",
        },
        "pcaf_weighted_data_quality_score": pcaf["weighted_data_quality_score"],
        "note": "PAI climate indicators derived from PCAF financed emissions; "
                "verify against the current SFDR RTS templates before filing.",
    }
