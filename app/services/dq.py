"""Data-quality scoring and uncertainty (ecoinvent pedigree matrix -> lognormal).

Five representativeness indicators, each scored 1 (best) to 5 (worst), mapped to
the published pedigree uncertainty factors and combined into a lognormal
geometric standard deviation; the 95% interval follows from the log-scale sigma.
A simpler 1-5 overall rating + high/medium/low label is also produced for
disclosure.

Conservative-by-default (GHG Protocol Quantitative Inventory Uncertainty
guidance): when the information needed to score an indicator is MISSING, the
indicator defaults to POOR (5), never to a flattering mid score — "we don't
know" must widen the band, not narrow it. The one exception: a factor bound by
an explicit human decision (approved/overridden) scores technological 2 even
without a resolver basis, because a person verified the match.
"""
import math
from typing import Optional

# Pedigree uncertainty factors, indicator score 1..5 (index 0..4), aligned to the
# GHG Protocol Quantitative Inventory Uncertainty pedigree table.
_UF = {
    "reliability":   [1.00, 1.05, 1.10, 1.20, 1.50],
    "completeness":  [1.00, 1.02, 1.05, 1.10, 1.20],
    "temporal":      [1.00, 1.03, 1.10, 1.20, 1.50],
    "geographical":  [1.00, 1.01, 1.02, 1.05, 1.10],
    "technological": [1.00, 1.05, 1.20, 1.50, 2.00],
}

# Basic uncertainty factor (Ub) by activity category, per the GHG Protocol
# guidance's category defaults (transport services 2.00; others default 1.05).
_BASIC_UNCERTAINTY_BY_CATEGORY = {
    "flight": 2.00, "train": 2.00, "car": 2.00,
    "waste": 1.50,   # CH4/N2O-dominated treatment processes
}
_BASIC_UNCERTAINTY_DEFAULT = 1.05

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

_MISSING = 5  # conservative default for any indicator we cannot actually score


def _clamp(n: int) -> int:
    return max(1, min(5, int(n)))


def temporal_score(activity_year: Optional[int], factor_year: Optional[int]) -> int:
    if activity_year is None or factor_year is None:
        return _MISSING
    d = abs(activity_year - factor_year)
    return 1 if d <= 1 else 2 if d <= 3 else 3 if d <= 6 else 4 if d <= 10 else 5


def geographical_score(activity_geo: Optional[str], factor_geo: Optional[str]) -> int:
    if not factor_geo or not activity_geo:
        return _MISSING
    if factor_geo == activity_geo:
        return 1
    if factor_geo == "Global":
        return 3
    return 4  # a different, specific geography


def completeness_score(has_gas_breakdown: bool, has_date: bool,
                       has_context: bool) -> int:
    """Proxy completeness from what the record actually carries.

    Starts at 1 and degrades for each missing element: per-gas decomposition on
    the factor, a usable activity date, and descriptive context (subcategory or
    description). A proxy, not a sample-adequacy audit — but it varies with the
    data instead of being a constant.
    """
    return _clamp(1 + (0 if has_gas_breakdown else 1)
                    + (0 if has_date else 1)
                    + (0 if has_context else 1))


def technological_score(mapping_basis: Optional[str], mapping_status: Optional[str]) -> int:
    if mapping_basis in _BASIS_TECH:
        return _BASIS_TECH[mapping_basis]
    if mapping_status in ("approved", "overridden"):
        return 2  # explicit human decision, basis unknown
    return _MISSING


def indicators(method_type: Optional[str], mapping_basis: Optional[str],
               activity_geo: Optional[str], factor_geo: Optional[str],
               activity_year: Optional[int], factor_year: Optional[int],
               completeness: Optional[int] = None,
               mapping_status: Optional[str] = None) -> dict:
    return {
        "reliability": _clamp(_METHOD_RELIABILITY.get(method_type, _MISSING)
                              if method_type else _MISSING),
        "completeness": _clamp(completeness) if completeness is not None else _MISSING,
        "temporal": temporal_score(activity_year, factor_year),
        "geographical": geographical_score(activity_geo, factor_geo),
        "technological": technological_score(mapping_basis, mapping_status),
    }


def score(ind: dict, category: Optional[str] = None) -> dict:
    """Overall DQ (1-5), rating label, and lognormal uncertainty.

    The rating is capped by the WORST indicator, not just the mean — a line
    resting on the worst method tier must not read as "high quality" because
    its other indicators are pristine.
    """
    vals = [ind[k] for k in _UF]
    overall = sum(vals) / len(vals)
    worst = max(vals)
    ub = _BASIC_UNCERTAINTY_BY_CATEGORY.get((category or "").lower(),
                                            _BASIC_UNCERTAINTY_DEFAULT)
    logvar = sum(math.log(_UF[k][ind[k] - 1]) ** 2 for k in _UF) + math.log(ub) ** 2
    sigma = math.sqrt(logvar)
    if overall <= 2 and worst <= 3:
        rating = "high"
    elif overall <= 3.5 and worst <= 4:
        rating = "medium"
    else:
        rating = "low"
    return {
        "indicators": ind,
        "overall": round(overall, 2),
        "worst_indicator": worst,
        "rating": rating,
        "basic_uncertainty": ub,
        "sigma_log": round(sigma, 4),
        "gsd": round(math.exp(sigma), 4),
        "ci95_low_mult": round(math.exp(-1.96 * sigma), 4),
        "ci95_high_mult": round(math.exp(1.96 * sigma), 4),
    }


def line_dq(factor, activity, mapping_basis: Optional[str],
            activity_year: Optional[int]) -> dict:
    """Convenience: pedigree score for one activity/factor pair."""
    completeness = completeness_score(
        has_gas_breakdown=bool(getattr(factor, "has_gas_breakdown", False)),
        has_date=activity_year is not None,
        has_context=bool((getattr(activity, "subcategory", "") or "").strip()
                         or (getattr(activity, "description", "") or "").strip()),
    )
    ind = indicators(
        method_type=getattr(factor, "method_type", None),
        mapping_basis=mapping_basis,
        activity_geo=getattr(activity, "geo", None),
        factor_geo=getattr(factor, "geography", None),
        activity_year=activity_year,
        factor_year=getattr(factor, "year", None),
        completeness=completeness,
        mapping_status=getattr(activity, "mapping_status", None),
    )
    return score(ind, category=getattr(activity, "category", None))
