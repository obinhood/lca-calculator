"""Data-quality scoring and uncertainty (ecoinvent pedigree matrix -> lognormal).

Five representativeness indicators, each scored 1 (best) to 5 (worst), mapped to
ecoinvent's published uncertainty factors and combined into a lognormal geometric
standard deviation; the 95% interval follows from the log-scale sigma. A simpler
1-5 overall rating + high/medium/low label is also produced for disclosure.

Cite: ecoinvent data-quality guideline (pedigree matrix); the indicators mirror
GHG Protocol Scope 3 data-quality guidance (reliability driven by the calculation
method: supplier-specific = best, spend-based EEIO = worst).
"""
import math
from typing import Optional

# ecoinvent pedigree uncertainty factors, indicator score 1..5 (index 0..4).
_UF = {
    "reliability":   [1.00, 1.05, 1.10, 1.20, 1.50],
    "completeness":  [1.00, 1.02, 1.05, 1.10, 1.20],
    "temporal":      [1.00, 1.03, 1.10, 1.20, 1.50],
    "geographical":  [1.00, 1.01, 1.02, 1.00, 1.10],
    "technological": [1.00, 1.05, 1.20, 1.50, 2.00],
}
_BASIC_UNCERTAINTY = 1.05  # default basic uncertainty factor (Ub)

# GHG Protocol calculation method -> reliability indicator (1 = best).
_METHOD_RELIABILITY = {
    "supplier_specific": 1,
    "hybrid": 2,
    "average_data": 3,
    "spend_based": 5,
}
# Resolver mapping basis -> technological representativeness.
_BASIS_TECH = {
    "exact": 1, "exact_global": 2, "fuzzy_subcategory": 3,
    "category_geo": 4, "category_only": 5,
}


def _clamp(n: int) -> int:
    return max(1, min(5, int(n)))


def temporal_score(activity_year: Optional[int], factor_year: Optional[int]) -> int:
    if activity_year is None or factor_year is None:
        return 3
    d = abs(activity_year - factor_year)
    return 1 if d <= 1 else 2 if d <= 3 else 3 if d <= 6 else 4 if d <= 10 else 5


def geographical_score(activity_geo: Optional[str], factor_geo: Optional[str]) -> int:
    if not factor_geo:
        return 3
    if factor_geo == activity_geo:
        return 1
    if factor_geo == "Global":
        return 3
    return 4  # a different, specific geography


def indicators(method_type: Optional[str], mapping_basis: Optional[str],
               activity_geo: Optional[str], factor_geo: Optional[str],
               activity_year: Optional[int], factor_year: Optional[int],
               completeness: int = 2) -> dict:
    return {
        "reliability": _clamp(_METHOD_RELIABILITY.get(method_type or "average_data", 3)),
        "completeness": _clamp(completeness),
        "temporal": temporal_score(activity_year, factor_year),
        "geographical": geographical_score(activity_geo, factor_geo),
        "technological": _clamp(_BASIS_TECH.get(mapping_basis or "", 3)),
    }


def score(ind: dict) -> dict:
    """Overall DQ (mean of indicators, 1-5), rating label, and lognormal uncertainty."""
    vals = [ind[k] for k in _UF]
    overall = sum(vals) / len(vals)
    logvar = sum(math.log(_UF[k][ind[k] - 1]) ** 2 for k in _UF) \
        + math.log(_BASIC_UNCERTAINTY) ** 2
    sigma = math.sqrt(logvar)
    rating = "high" if overall <= 2 else "medium" if overall <= 3.5 else "low"
    return {
        "indicators": ind,
        "overall": round(overall, 2),
        "rating": rating,
        "sigma_log": round(sigma, 4),
        "gsd": round(math.exp(sigma), 4),
        "ci95_low_mult": round(math.exp(-1.96 * sigma), 4),
        "ci95_high_mult": round(math.exp(1.96 * sigma), 4),
    }


def line_dq(factor, activity, mapping_basis: Optional[str],
            activity_year: Optional[int]) -> dict:
    """Convenience: pedigree score for one activity/factor pair."""
    ind = indicators(
        method_type=getattr(factor, "method_type", None),
        mapping_basis=mapping_basis,
        activity_geo=getattr(activity, "geo", None),
        factor_geo=getattr(factor, "geography", None),
        activity_year=activity_year,
        factor_year=getattr(factor, "year", None),
    )
    return score(ind)
