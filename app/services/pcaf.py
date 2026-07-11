"""PCAF financed-emissions engine (Global GHG Accounting Standard for Financials).

Financed emissions per position = attribution factor x investee emissions.
Attribution factor = outstanding / denominator (EVIC, total equity+debt, or
property value depending on asset class — same currency, dimensionless ratio).
Portfolio totals are reported by asset class with an emissions-weighted PCAF
data-quality score (1 best .. 5 proxy).
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
    q = db.query(FinancedPosition).filter(
        FinancedPosition.organisation_id == organisation_id)
    if as_of is not None:
        q = q.filter(FinancedPosition.as_of_date == as_of)
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
