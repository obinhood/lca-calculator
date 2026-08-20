"""SBTi Corporate Net-Zero Standard V2.0 — significance, categorisation, Scope 2.

Approved 11 June 2026, effective 1 February 2027. Built from the Criteria document,
not from summaries, because the two consultation drafts (March and November 2025)
differ materially from the final text and most secondary reporting describes the
drafts.

THREE THINGS THE SECONDARY SOURCES GET WRONG, each of which would make this engine
reject a conformant company:

1. THERE IS NO DUAL SCOPE 2 TARGET. C12.2 offers "either of the following
   options" — ONE Scope 2 target suffices, chosen from low-carbon-electricity
   alignment or absolute emissions reduction. The location-based-plus-market-based
   pair was in the drafts and was dropped. Requiring both would reject companies
   that comply.

2. THERE IS NO CATEGORY C. V2.0 defines exactly two company categories, A and B.

3. "ZERO-CARBON ELECTRICITY" IS NOT A V2.0 TERM. The Standard says LOW-carbon
   electricity and defines it numerically: a generator at or below
   0.048 kgCO2/kWh, tightening to 0.024 from 2035, evaluated PER GENERATOR rather
   than on a portfolio average.

THE 5% DENOMINATOR IS THE EASIEST THING TO GET WRONG. A Scope 3 category is
significant when it is at least 5% of scope 3 CATEGORIES 1-14 in the PHYSICAL
inventory — never total Scope 3, never the whole inventory, and never including
category 15 (which is carved out to the Financial Institutions standard).
Emissions accounted outside the GHG Protocol Table 5.4 minimum boundary are
excluded from that denominator (C5.6.b), with one asymmetry: well-to-wheel /
well-to-wake transport emissions are mandatory ON TOP of the minimum boundary AND
stay inside the denominator (C5.6.a with C14.1 fn21). Including category 15, or
dropping the WTW uplift, shifts every category's share and silently moves
categories across the 5% line.

THERE IS NO AGGREGATE COVERAGE FLOOR. The old 67% rule is gone and is not
back-stopped by anything. A company whose every category sits under 5% legitimately
owes zero Scope 3 target categories, and this module says so rather than
manufacturing a minimum.

A SUB-5% CATEGORY NEEDS NO JUSTIFICATION. It simply drops out. Justification is
required only for C14.2 exclusions of activities INSIDE a significant category,
and then a closed condition enum plus four disclosure fields are mandatory (C14.3).
Demanding a reason for every excluded category inverts the rule.
"""
import math
from typing import Optional

SBTI_V2_VERSION = "cnzs-v2.0"
SBTI_V2_APPROVED = "2026-06-11"
SBTI_V2_EFFECTIVE = "2027-02-01"
# SBTi's own documents disagree: the Standard and FAQ say "end of 2027", the
# transition document says January 2028. Configurable rather than hardcoded to
# either, because picking one silently is a liability.
SBTI_V1_SUBMISSION_CUTOFF_DEFAULT = "2027-12-31"

# C5.7 / C14.1 — significance threshold, of scope 3 categories 1-14.
SIGNIFICANCE_THRESHOLD = 0.05
# Category 15 is carved out to the Financial Institutions Net-Zero Standard and is
# NOT part of the denominator.
DENOMINATOR_CATEGORIES = tuple(range(1, 15))

# C8.3 — a scope varying by this much in the base year triggers recalculation.
RECALCULATION_SCOPE_VARIATION = 0.05

# A.1 — exactly two categories. Thresholds are assessed on the CONSOLIDATED GROUP,
# on the average of the two most recent financial statements, in EUR.
CATEGORY_A_TURNOVER_EUR = 450_000_000
CATEGORY_A_FTE = 1_000
HIGH_INCOME_SCOPE12_TCO2E = 10_000
HIGH_INCOME_BALANCE_SHEET_EUR = 25_000_000
HIGH_INCOME_TURNOVER_EUR = 50_000_000
HIGH_INCOME_FTE = 250

# C31.x — low-carbon electricity is a NUMERIC per-generator test, not a label.
LCE_KG_CO2_PER_KWH = 0.048
LCE_KG_CO2_PER_KWH_FROM_2035 = 0.024
LCE_TIGHTENING_YEAR = 2035
# C31.3 — an instrument from a generator commissioned or re-powered longer ago
# than this, relative to the consumption period, is rejected.
GENERATOR_MAX_AGE_YEARS = 15

# C12.1-C12.2 — one or more, not a mandatory pair.
SCOPE2_TARGET_TYPES = ("LCE_ALIGNMENT", "ABSOLUTE_EMISSIONS_REDUCTION")
# C12.4 — Category A above this projected growth must use absolute reduction.
SCOPE2_HIGH_GROWTH_THRESHOLD = 0.20

# C14.2 — a CLOSED list. An exclusion that is not one of these is not conformant.
EXCLUSION_CONDITIONS = (
    "CAT_1_2_SECOND_HAND_GOODS",
    "CAT_3_MITIGATED_VIA_SCOPE_1_2",
    "CAT_7_EMPLOYEE_COMMUTING_ENTIRE",
    "CAT_8_NO_OPERATIONAL_CONTROL_NO_INFLUENCE",
    "CAT_9_NO_CONTRACTUAL_INFLUENCE_ON_MODE",
    "CAT_10_PROCESSING_UNKNOWN_OR_NO_RELATIONSHIP",
    "CAT_14_FRANCHISEE_INDEPENDENT_OR_NO_ENERGY_CONTROL",
)
# C14.3 — every exclusion needs all four.
EXCLUSION_REQUIRED_FIELDS = (
    "exclusion_condition", "why_it_applies",
    "excluded_emissions_tco2e", "planned_mitigation_actions",
)


def company_category(*, turnover_eur: Optional[float] = None,
                     fte: Optional[int] = None,
                     scope12_tco2e: Optional[float] = None,
                     balance_sheet_eur: Optional[float] = None,
                     high_income_country: Optional[bool] = None) -> dict:
    """Category A or B (A.1). There is no Category C.

    Assessed on the CONSOLIDATED GROUP using the average of the two most recent
    financial statements in EUR — not on the reporting entity's own single year,
    which would misclassify a subsidiary of a large group.

    Refuses rather than guessing: with nothing supplied the answer is
    cannot_determine, because defaulting to B would let a large company past the
    Category A criteria that only apply to it.
    """
    supplied = [v for v in (turnover_eur, fte, scope12_tco2e, balance_sheet_eur)
                if v is not None]
    if not supplied:
        return {"determinable": False, "category": None,
                "reason": "no consolidated-group financials or emissions supplied; "
                          "defaulting to Category B would exempt a large company from "
                          "the criteria that only bind Category A"}

    reasons = []
    if turnover_eur is not None and turnover_eur >= CATEGORY_A_TURNOVER_EUR:
        reasons.append(f"net turnover EUR {turnover_eur:,.0f} >= "
                       f"{CATEGORY_A_TURNOVER_EUR:,.0f}")
    if fte is not None and fte >= CATEGORY_A_FTE:
        reasons.append(f"{fte:,} FTE >= {CATEGORY_A_FTE:,}")

    if not reasons and high_income_country:
        if scope12_tco2e is not None and scope12_tco2e >= HIGH_INCOME_SCOPE12_TCO2E:
            reasons.append(f"high-income country and scope 1+2 "
                           f"{scope12_tco2e:,.0f} tCO2e >= "
                           f"{HIGH_INCOME_SCOPE12_TCO2E:,}")
        else:
            met = []
            if balance_sheet_eur is not None and balance_sheet_eur >= HIGH_INCOME_BALANCE_SHEET_EUR:
                met.append("balance sheet")
            if turnover_eur is not None and turnover_eur >= HIGH_INCOME_TURNOVER_EUR:
                met.append("net turnover")
            if fte is not None and fte >= HIGH_INCOME_FTE:
                met.append("FTE")
            if len(met) >= 2:
                reasons.append(f"high-income country and two of three size tests met "
                               f"({', '.join(met)})")

    category = "A" if reasons else "B"
    return {
        "determinable": True, "category": category,
        "basis": reasons or ["none of the Category A thresholds met"],
        "assessed_on": "consolidated group, average of the two most recent financial "
                       "statements, converted to EUR",
        "note": "V2.0 defines exactly two categories. There is no Category C — that "
                "appears only in third-party summaries of the consultation drafts.",
    }


def significance(category_emissions_tco2e: dict, *,
                 outside_minimum_boundary_tco2e: Optional[dict] = None,
                 wtw_uplift_tco2e: Optional[dict] = None) -> dict:
    """Which Scope 3 categories are significant under C5.7 / C14.1.

    `category_emissions_tco2e` maps GHGP Scope 3 category number to tCO2e as
    accounted. `outside_minimum_boundary_tco2e` is the portion of each accounted
    above the Table 5.4 minimum boundary, which C5.6.b removes from the
    denominator. `wtw_uplift_tco2e` is the well-to-wheel/well-to-wake transport
    uplift, which C5.6.a makes mandatory and fn21 KEEPS in the denominator — the
    one asymmetry in the rule.
    """
    outside = outside_minimum_boundary_tco2e or {}
    wtw = wtw_uplift_tco2e or {}

    in_denominator, excluded_above_boundary = {}, {}
    for cat in DENOMINATOR_CATEGORIES:
        accounted = float(category_emissions_tco2e.get(cat, 0.0) or 0.0)
        above = float(outside.get(cat, 0.0) or 0.0)
        uplift = float(wtw.get(cat, 0.0) or 0.0)
        # Remove the above-minimum-boundary portion, then add the WTW uplift back.
        value = max(0.0, accounted - above) + uplift
        in_denominator[cat] = value
        if above:
            excluded_above_boundary[cat] = above

    denominator = sum(in_denominator.values())
    if denominator <= 0:
        return {"determinable": False,
                "reason": "scope 3 categories 1-14 total zero in the physical "
                          "inventory; significance cannot be determined",
                "denominator_tco2e": 0.0, "significant": [], "shares": {}}

    shares = {c: v / denominator for c, v in in_denominator.items()}
    significant = sorted(c for c, s in shares.items() if s >= SIGNIFICANCE_THRESHOLD)

    cat15 = category_emissions_tco2e.get(15)
    return {
        "determinable": True,
        "threshold_pct": SIGNIFICANCE_THRESHOLD * 100,
        "denominator_tco2e": round(denominator, 6),
        "denominator_basis": "scope 3 categories 1-14, physical GHG inventory, GHG "
                             "Protocol Table 5.4 minimum boundary PLUS the mandatory "
                             "WTW transport uplift. Category 15 is excluded (carved "
                             "out to the Financial Institutions Net-Zero Standard), "
                             "as are emissions accounted above the minimum boundary "
                             "other than WTW (C5.6.b, C14.1 fn21).",
        "significant": significant,
        "shares": {c: round(s * 100, 4) for c, s in sorted(shares.items())},
        "category_15_excluded_tco2e": (None if cat15 is None
                                       else round(float(cat15), 6)),
        "excluded_above_minimum_boundary_tco2e": {
            c: round(v, 6) for c, v in sorted(excluded_above_boundary.items())},
        "no_aggregate_floor": True,
        "no_aggregate_floor_note": (
            "There is NO total-coverage floor in V2.0. The old 67% rule is gone and "
            "nothing backstops it: a company whose every category sits under 5% "
            "legitimately owes zero Scope 3 target categories."),
        "sub_threshold_note": (
            "A category below 5% drops out and needs NO justification. Justification "
            "is required only for C14.2 exclusions of activities INSIDE a significant "
            "category."),
    }


def validate_exclusion(exclusion: dict, *, denominator_tco2e: float) -> dict:
    """Check one C14.2 exclusion against the closed enum and the C14.3 payload."""
    problems = []
    cond = exclusion.get("exclusion_condition")
    if cond not in EXCLUSION_CONDITIONS:
        problems.append(
            f"exclusion_condition {cond!r} is not one of the C14.2 conditions "
            f"{list(EXCLUSION_CONDITIONS)}. The list is closed: an exclusion outside "
            f"it is not conformant.")
    for field in EXCLUSION_REQUIRED_FIELDS:
        v = exclusion.get(field)
        if v is None or (isinstance(v, str) and not v.strip()):
            problems.append(f"C14.3 requires {field}; it is missing")

    tco2e = exclusion.get("excluded_emissions_tco2e")
    pct = None
    if isinstance(tco2e, (int, float)) and math.isfinite(tco2e):
        if tco2e < 0:
            problems.append("excluded_emissions_tco2e must be >= 0")
        elif denominator_tco2e > 0:
            pct = round(100.0 * tco2e / denominator_tco2e, 4)

    return {
        "conformant": not problems, "problems": problems,
        "exclusion_condition": cond,
        "excluded_emissions_tco2e": tco2e,
        "excluded_emissions_pct_of_scope3_1_14": pct,
        "note": "Category 7 (employee commuting) may be excluded in its entirety with "
                "no influence test, but still requires the full C14.3 payload. "
                "Category 3's exclusion is valid only where those emissions are "
                "mitigated through Scope 1 or Scope 2 energy reductions, and an "
                "applicable Sector Standard can override it back to included.",
    }


def target_boundary(significance_result: dict, exclusions: Optional[list] = None,
                    covered_categories: Optional[list] = None) -> dict:
    """Whether a near-term Scope 3 target boundary satisfies C14.

    Covers at least every significant category, minus any conformant C14.2
    exclusion. A non-conformant exclusion does NOT remove its category.
    """
    if not significance_result.get("determinable"):
        return {"conformant": False, "determinable": False,
                "reason": significance_result.get("reason")}

    significant = set(significance_result["significant"])
    denominator = significance_result["denominator_tco2e"]
    checked = [validate_exclusion(e, denominator_tco2e=denominator)
               for e in (exclusions or [])]
    bad = [c for c in checked if not c["conformant"]]

    covered = set(covered_categories or [])
    # Only a CONFORMANT exclusion excuses a category.
    excused = set()
    for e, c in zip(exclusions or [], checked):
        if c["conformant"] and isinstance(e.get("category"), int):
            excused.add(e["category"])

    missing = sorted(significant - covered - excused)
    return {
        "determinable": True,
        "conformant": not missing and not bad,
        "significant_categories": sorted(significant),
        "covered_categories": sorted(covered),
        "excused_by_conformant_exclusion": sorted(excused),
        "missing_categories": missing,
        "non_conformant_exclusions": bad,
        "note": "A non-conformant exclusion does not remove its category from the "
                "required boundary — otherwise an invalid justification would "
                "silently shrink the target.",
    }


def recalculation_triggers(base_significance: dict, current_significance: dict,
                           *, scope_variation: Optional[dict] = None) -> dict:
    """C8.3 — when the target must be recalculated.

    Two independent triggers, and the second is the one an engine that computes
    significance once at validation will miss: a category NEWLY crossing or
    falling below the 5% line is itself a trigger, separate from the per-scope
    variation test.
    """
    triggers = []
    if base_significance.get("determinable") and current_significance.get("determinable"):
        was, now = set(base_significance["significant"]), set(current_significance["significant"])
        crossed_up, crossed_down = sorted(now - was), sorted(was - now)
        if crossed_up:
            triggers.append({
                "trigger": "category_crossed_above_threshold",
                "categories": crossed_up,
                "criterion": "C8.3 — a scope 3 category newly reaching 5% of "
                             "categories 1-14 requires recalculation"})
        if crossed_down:
            triggers.append({
                "trigger": "category_fell_below_threshold",
                "categories": crossed_down,
                "criterion": "C8.3 — a scope 3 category newly falling below 5% "
                             "requires recalculation"})

    for scope, variation in sorted((scope_variation or {}).items()):
        if variation is None:
            continue
        if abs(variation) >= RECALCULATION_SCOPE_VARIATION:
            triggers.append({
                "trigger": "scope_variation",
                "scope": scope,
                "variation_pct": round(variation * 100, 4),
                "criterion": f"C8.3 — cumulative changes varying total emissions of "
                             f"any individual scope by "
                             f">= {RECALCULATION_SCOPE_VARIATION:.0%} in the target "
                             f"base year"})

    return {"recalculation_required": bool(triggers), "triggers": triggers,
            "note": "The significance set is NOT frozen at validation. An engine that "
                    "tests significance once will miss the crossing trigger entirely."}


def lce_threshold(year: int) -> float:
    """Low-carbon electricity threshold for a consumption year (kgCO2/kWh)."""
    return (LCE_KG_CO2_PER_KWH_FROM_2035 if year >= LCE_TIGHTENING_YEAR
            else LCE_KG_CO2_PER_KWH)


def is_low_carbon(generator_kg_co2_per_kwh: Optional[float], year: int) -> dict:
    """Whether ONE generator qualifies as low-carbon (C31).

    Evaluated per generator, never on a portfolio average: averaging lets a high
    carbon source ride in on the back of a clean one.
    """
    t = lce_threshold(year)
    if generator_kg_co2_per_kwh is None or not isinstance(
            generator_kg_co2_per_kwh, (int, float)) or not math.isfinite(
            generator_kg_co2_per_kwh):
        return {"determinable": False, "low_carbon": None, "threshold": t,
                "reason": "no generator emissions intensity supplied; low-carbon "
                          "status is a numeric test and cannot be assumed from a "
                          "technology label"}
    return {
        "determinable": True,
        "low_carbon": generator_kg_co2_per_kwh <= t,
        "generator_kg_co2_per_kwh": generator_kg_co2_per_kwh,
        "threshold": t, "year": year,
        "note": f"Evaluated PER GENERATOR against {t} kgCO2/kWh"
                f"{' (tightened from 2035)' if year >= LCE_TIGHTENING_YEAR else ''}, "
                f"never on a portfolio average. 'Zero-carbon electricity' is not a "
                f"V2.0 term — the Standard says LOW-carbon and defines it numerically.",
    }


def scope2_target_conformance(target_types: list, *,
                              company_cat: Optional[str] = None,
                              projected_electricity_growth: Optional[float] = None,
                              coverage_pct: Optional[float] = None) -> dict:
    """C12 — Scope 2 near-term target conformance.

    ONE target suffices. C12.2 offers "either of the following options", and the
    location-based-plus-market-based PAIR that circulated widely was in the
    consultation drafts and did not survive into the final text. Requiring both
    would reject conformant companies.
    """
    problems, warnings = [], []
    unknown = [t for t in target_types if t not in SCOPE2_TARGET_TYPES]
    if unknown:
        problems.append(f"unknown Scope 2 target type(s) {unknown}; C12.2 permits "
                        f"{list(SCOPE2_TARGET_TYPES)}")
    if not target_types:
        problems.append("at least one Scope 2 near-term target is required (C12.1)")

    if coverage_pct is not None and coverage_pct < 100.0:
        problems.append(f"Scope 2 targets must cover 100% of Scope 2; "
                        f"{coverage_pct}% declared (C12.2)")

    forced = False
    if (company_cat == "A" and projected_electricity_growth is not None
            and projected_electricity_growth > SCOPE2_HIGH_GROWTH_THRESHOLD):
        forced = True
        if "ABSOLUTE_EMISSIONS_REDUCTION" not in target_types:
            problems.append(
                f"C12.4 — Category A with projected average annual electricity growth "
                f"of {projected_electricity_growth:.0%} (above "
                f"{SCOPE2_HIGH_GROWTH_THRESHOLD:.0%}) must use "
                f"ABSOLUTE_EMISSIONS_REDUCTION; an LCE alignment target may only be "
                f"additional")

    return {
        "conformant": not problems,
        "problems": problems, "warnings": warnings,
        "target_types": target_types,
        "absolute_reduction_forced_by_growth": forced,
        "dual_target_required": False,
        "dual_target_note": (
            "V2.0 does NOT require a location-based target paired with a "
            "market-based or zero-carbon one. C12.2 says 'either of the following "
            "options' — one Scope 2 target suffices. The pair appeared in the "
            "consultation drafts and was dropped; requiring it would reject "
            "conformant companies."),
        "ambition_basis_note": (
            "The physical GHG inventory uses the LOCATION-BASED method for Scope 2 "
            "and target ambition is determined from it (C9.4). Market-based figures "
            "never set ambition under V2.0."),
        "single_base_year_note": (
            "One target base year applies across all near-term targets (C4.4). There "
            "is no separate market-based base year."),
    }


def version_applicability(period_start: Optional[str], *,
                          v1_cutoff: str = SBTI_V1_SUBMISSION_CUTOFF_DEFAULT) -> dict:
    """Which ruleset applies, without pretending version is a single switch.

    Several V2.0 innovations — company categorisation, the updated absolute
    contraction method, the implementation hierarchy and market instruments — are
    being back-ported into V1, so a single boolean mis-routes rules through
    2026-2028. The dates themselves are configurable because SBTi's own documents
    disagree: the Standard and FAQ say "end of 2027", the transition document says
    January 2028.
    """
    return {
        "v2_approved": SBTI_V2_APPROVED,
        "v2_effective": SBTI_V2_EFFECTIVE,
        "v1_submission_cutoff": v1_cutoff,
        "period_start": period_start,
        "v2_in_force_for_period": (
            None if not period_start else period_start >= SBTI_V2_EFFECTIVE),
        "note": "Version is not a single switch. Company categorisation, the updated "
                "absolute contraction method, the implementation hierarchy and market "
                "instruments are back-ported into V1, so applicability is per "
                "requirement rather than per version label.",
        "cutoff_note": "SBTi's own documents disagree on the V1 cutoff — the Standard "
                       "and FAQ say 'end of 2027', the transition document says "
                       "January 2028. The value here is configuration, not a "
                       "hardcoded assumption.",
    }
