"""SBTi Corporate Net-Zero Standard V2.0.

The tests that matter most are the ones guarding against the CONSULTATION DRAFTS,
because most secondary reporting describes them and they differ materially from
the final text approved 11 June 2026. Three premises circulating widely are wrong,
and building any of them would make this engine reject a conformant company:
there is no dual Scope 2 target, there is no Category C, and "zero-carbon
electricity" is not a V2.0 term.

The other load-bearing test is the denominator: 5% of scope 3 categories 1-14,
minimum boundary PLUS the WTW uplift. Getting it wrong moves categories across the
significance line in either direction.
"""
import pytest

from app.services.sbti_v2 import (
    CATEGORY_A_FTE, CATEGORY_A_TURNOVER_EUR, DENOMINATOR_CATEGORIES,
    EXCLUSION_CONDITIONS, GENERATOR_MAX_AGE_YEARS, LCE_KG_CO2_PER_KWH,
    LCE_KG_CO2_PER_KWH_FROM_2035, SBTI_V2_EFFECTIVE, SCOPE2_TARGET_TYPES,
    SIGNIFICANCE_THRESHOLD, company_category, is_low_carbon, lce_threshold,
    recalculation_triggers, scope2_target_conformance, significance,
    target_boundary, validate_exclusion, version_applicability,
)


# --- guards against the consultation drafts ----------------------------------

def test_there_is_no_dual_scope_2_target_requirement():
    """THE most-repeated error. C12.2 offers 'either of the following options' —
    one target suffices. The location-based + market-based pair was in the drafts
    and was dropped; requiring it would reject conformant companies."""
    r = scope2_target_conformance(["LCE_ALIGNMENT"], coverage_pct=100.0)
    assert r["conformant"] is True
    assert r["dual_target_required"] is False
    assert "dropped" in r["dual_target_note"]

    r2 = scope2_target_conformance(["ABSOLUTE_EMISSIONS_REDUCTION"], coverage_pct=100.0)
    assert r2["conformant"] is True


def test_there_is_no_category_c():
    """V2.0 defines exactly two company categories."""
    big = company_category(turnover_eur=500_000_000)
    small = company_category(turnover_eur=1_000_000, fte=10)
    assert big["category"] == "A"
    assert small["category"] == "B"
    assert "no Category C" in big["note"]


def test_zero_carbon_electricity_is_not_a_v2_term():
    """The Standard says LOW-carbon and defines it numerically."""
    r = is_low_carbon(0.03, 2027)
    assert r["low_carbon"] is True
    assert "not a" in r["note"] and "V2.0 term" in r["note"]


def test_the_old_67_percent_coverage_floor_is_not_carried_forward():
    """A company whose every category is under 5% legitimately owes zero Scope 3
    target categories. Nothing backstops the removed rule."""
    tiny_spread = {c: 100.0 for c in range(1, 15)}     # each is 1/14 = 7.1%
    r = significance({c: 1.0 for c in range(1, 15)} | {1: 1.0})
    assert r["no_aggregate_floor"] is True
    assert "67%" in r["no_aggregate_floor_note"]


# --- the denominator ----------------------------------------------------------

def test_category_15_is_excluded_from_the_denominator():
    """Cat 15 is carved out to the Financial Institutions standard. Including it
    shrinks every share and silently drops categories below the line."""
    with_15 = significance({1: 100.0, 2: 100.0, 15: 100_000.0})
    assert with_15["denominator_tco2e"] == pytest.approx(200.0)
    assert with_15["category_15_excluded_tco2e"] == pytest.approx(100_000.0)
    assert set(with_15["significant"]) == {1, 2}


def test_the_denominator_is_categories_one_to_fourteen():
    assert DENOMINATOR_CATEGORIES == tuple(range(1, 15))
    r = significance({c: 10.0 for c in range(1, 16)})
    assert r["denominator_tco2e"] == pytest.approx(140.0)


def test_above_minimum_boundary_emissions_leave_the_denominator():
    """C5.6.b removes them."""
    r = significance({1: 200.0, 2: 100.0},
                     outside_minimum_boundary_tco2e={1: 100.0})
    assert r["denominator_tco2e"] == pytest.approx(200.0)
    assert r["excluded_above_minimum_boundary_tco2e"] == {1: 100.0}


def test_the_wtw_uplift_stays_in_the_denominator():
    """THE asymmetry, and the easiest thing to get wrong. WTW is mandatory ON TOP
    of the minimum boundary and fn21 explicitly keeps it in the denominator, while
    every other above-boundary amount is removed."""
    r = significance({4: 100.0}, wtw_uplift_tco2e={4: 50.0})
    assert r["denominator_tco2e"] == pytest.approx(150.0)
    assert "WTW" in r["denominator_basis"]


def test_the_threshold_is_five_percent():
    assert SIGNIFICANCE_THRESHOLD == 0.05
    r = significance({1: 5.0, 2: 95.0})
    assert 1 in r["significant"] and 2 in r["significant"]
    r2 = significance({1: 4.9, 2: 95.1})
    assert 1 not in r2["significant"]


def test_an_empty_inventory_refuses_rather_than_reporting_none_significant():
    r = significance({})
    assert r["determinable"] is False
    assert "cannot be determined" in r["reason"]


# --- exclusions ---------------------------------------------------------------

def test_a_sub_threshold_category_needs_no_justification():
    """Demanding a reason for every excluded category inverts the rule."""
    r = significance({1: 96.0, 2: 4.0})
    assert 2 not in r["significant"]
    assert "NO justification" in r["sub_threshold_note"]


@pytest.mark.parametrize("cond", EXCLUSION_CONDITIONS)
def test_every_declared_exclusion_condition_is_accepted(cond):
    r = validate_exclusion({
        "exclusion_condition": cond, "why_it_applies": "documented reason",
        "excluded_emissions_tco2e": 10.0,
        "planned_mitigation_actions": "supplier engagement programme",
    }, denominator_tco2e=1000.0)
    assert r["conformant"] is True, cond
    assert r["excluded_emissions_pct_of_scope3_1_14"] == pytest.approx(1.0)


def test_an_exclusion_outside_the_closed_list_is_refused():
    r = validate_exclusion({
        "exclusion_condition": "WE_FOUND_IT_HARD", "why_it_applies": "x",
        "excluded_emissions_tco2e": 1.0, "planned_mitigation_actions": "y",
    }, denominator_tco2e=100.0)
    assert r["conformant"] is False
    assert any("closed" in p for p in r["problems"])


@pytest.mark.parametrize("missing", [
    "why_it_applies", "excluded_emissions_tco2e", "planned_mitigation_actions"])
def test_every_c14_3_field_is_required(missing):
    e = {"exclusion_condition": "CAT_7_EMPLOYEE_COMMUTING_ENTIRE",
         "why_it_applies": "x", "excluded_emissions_tco2e": 1.0,
         "planned_mitigation_actions": "y"}
    e.pop(missing)
    r = validate_exclusion(e, denominator_tco2e=100.0)
    assert r["conformant"] is False
    assert any(missing in p for p in r["problems"])


def test_a_non_conformant_exclusion_does_not_shrink_the_target_boundary():
    """Otherwise an invalid justification would silently remove a category."""
    sig = significance({1: 50.0, 2: 50.0})
    r = target_boundary(sig, exclusions=[{
        "category": 2, "exclusion_condition": "NOT_A_REAL_CONDITION",
        "why_it_applies": "x", "excluded_emissions_tco2e": 1.0,
        "planned_mitigation_actions": "y"}], covered_categories=[1])
    assert r["conformant"] is False
    assert 2 in r["missing_categories"]
    assert r["non_conformant_exclusions"]


def test_a_conformant_exclusion_excuses_its_category():
    sig = significance({1: 50.0, 7: 50.0})
    r = target_boundary(sig, exclusions=[{
        "category": 7, "exclusion_condition": "CAT_7_EMPLOYEE_COMMUTING_ENTIRE",
        "why_it_applies": "no influence test required for commuting",
        "excluded_emissions_tco2e": 50.0,
        "planned_mitigation_actions": "hybrid working policy"}],
        covered_categories=[1])
    assert r["conformant"] is True
    assert r["excused_by_conformant_exclusion"] == [7]


def test_a_boundary_missing_a_significant_category_is_non_conformant():
    sig = significance({1: 50.0, 2: 50.0})
    r = target_boundary(sig, covered_categories=[1])
    assert r["conformant"] is False and r["missing_categories"] == [2]


# --- recalculation ------------------------------------------------------------

def test_a_category_crossing_the_threshold_triggers_recalculation():
    """C8.3, and the trigger an engine that computes significance once will miss."""
    base = significance({1: 96.0, 2: 4.0})
    now = significance({1: 90.0, 2: 10.0})
    r = recalculation_triggers(base, now)
    assert r["recalculation_required"] is True
    up = [t for t in r["triggers"] if t["trigger"] == "category_crossed_above_threshold"]
    assert up and up[0]["categories"] == [2]


def test_a_category_falling_below_the_threshold_also_triggers():
    base = significance({1: 90.0, 2: 10.0})
    now = significance({1: 96.0, 2: 4.0})
    r = recalculation_triggers(base, now)
    down = [t for t in r["triggers"] if t["trigger"] == "category_fell_below_threshold"]
    assert down and down[0]["categories"] == [2]


def test_a_five_percent_scope_variation_triggers():
    sig = significance({1: 100.0})
    r = recalculation_triggers(sig, sig, scope_variation={"1": 0.06})
    assert r["recalculation_required"] is True
    assert any(t["trigger"] == "scope_variation" for t in r["triggers"])


def test_a_small_variation_does_not_trigger():
    sig = significance({1: 100.0})
    r = recalculation_triggers(sig, sig, scope_variation={"1": 0.01, "2": -0.02})
    assert r["recalculation_required"] is False


# --- low-carbon electricity ---------------------------------------------------

def test_the_lce_threshold_tightens_in_2035():
    assert lce_threshold(2027) == LCE_KG_CO2_PER_KWH == 0.048
    assert lce_threshold(2034) == 0.048
    assert lce_threshold(2035) == LCE_KG_CO2_PER_KWH_FROM_2035 == 0.024
    assert lce_threshold(2040) == 0.024


@pytest.mark.parametrize("intensity,year,expected", [
    (0.048, 2027, True), (0.049, 2027, False),
    (0.030, 2035, False), (0.024, 2035, True),
])
def test_low_carbon_is_a_numeric_per_generator_test(intensity, year, expected):
    assert is_low_carbon(intensity, year)["low_carbon"] is expected


def test_low_carbon_cannot_be_assumed_from_a_technology_label():
    r = is_low_carbon(None, 2027)
    assert r["determinable"] is False
    assert "technology label" in r["reason"]


# --- Scope 2 conformance ------------------------------------------------------

@pytest.mark.parametrize("t", SCOPE2_TARGET_TYPES)
def test_each_permitted_scope2_target_type_is_conformant(t):
    assert scope2_target_conformance([t], coverage_pct=100.0)["conformant"] is True


def test_an_unknown_scope2_target_type_is_refused():
    r = scope2_target_conformance(["RENEWABLE_PERCENTAGE"], coverage_pct=100.0)
    assert r["conformant"] is False
    assert any("unknown" in p for p in r["problems"])


def test_no_scope2_target_is_non_conformant():
    r = scope2_target_conformance([], coverage_pct=100.0)
    assert r["conformant"] is False


def test_partial_scope2_coverage_is_non_conformant():
    r = scope2_target_conformance(["LCE_ALIGNMENT"], coverage_pct=80.0)
    assert r["conformant"] is False
    assert any("100%" in p for p in r["problems"])


def test_high_growth_category_a_must_use_absolute_reduction():
    """C12.4 — an LCE alignment target may only be additional."""
    r = scope2_target_conformance(["LCE_ALIGNMENT"], company_cat="A",
                                  projected_electricity_growth=0.35,
                                  coverage_pct=100.0)
    assert r["conformant"] is False
    assert r["absolute_reduction_forced_by_growth"] is True
    assert any("C12.4" in p for p in r["problems"])


def test_high_growth_with_absolute_reduction_is_conformant():
    r = scope2_target_conformance(
        ["ABSOLUTE_EMISSIONS_REDUCTION", "LCE_ALIGNMENT"], company_cat="A",
        projected_electricity_growth=0.35, coverage_pct=100.0)
    assert r["conformant"] is True


def test_category_b_is_not_forced_by_growth():
    r = scope2_target_conformance(["LCE_ALIGNMENT"], company_cat="B",
                                  projected_electricity_growth=0.35,
                                  coverage_pct=100.0)
    assert r["conformant"] is True


def test_ambition_comes_from_the_location_based_inventory():
    r = scope2_target_conformance(["LCE_ALIGNMENT"], coverage_pct=100.0)
    assert "LOCATION-BASED" in r["ambition_basis_note"]
    assert "never set ambition" in r["ambition_basis_note"]


# --- company categorisation ---------------------------------------------------

def test_turnover_alone_reaches_category_a():
    assert company_category(turnover_eur=CATEGORY_A_TURNOVER_EUR)["category"] == "A"


def test_headcount_alone_reaches_category_a():
    assert company_category(fte=CATEGORY_A_FTE)["category"] == "A"


def test_high_income_emissions_route_to_category_a():
    r = company_category(scope12_tco2e=15_000, high_income_country=True,
                         turnover_eur=1_000_000, fte=50)
    assert r["category"] == "A"
    assert any("high-income" in b for b in r["basis"])


def test_high_income_two_of_three_size_tests():
    r = company_category(turnover_eur=60_000_000, fte=300,
                         balance_sheet_eur=10_000_000, high_income_country=True)
    assert r["category"] == "A"


def test_one_of_three_size_tests_is_not_enough():
    r = company_category(turnover_eur=60_000_000, fte=100,
                         balance_sheet_eur=10_000_000, high_income_country=True)
    assert r["category"] == "B"


def test_categorisation_refuses_with_no_inputs():
    """Defaulting to B would exempt a large company from criteria that bind only A."""
    r = company_category()
    assert r["determinable"] is False
    assert "exempt a large company" in r["reason"]


def test_categorisation_states_it_is_assessed_on_the_group():
    r = company_category(turnover_eur=500_000_000)
    assert "consolidated group" in r["assessed_on"]
    assert "two most recent" in r["assessed_on"]


# --- version applicability ----------------------------------------------------

def test_v2_effective_date():
    assert SBTI_V2_EFFECTIVE == "2027-02-01"
    assert version_applicability("2027-06-01")["v2_in_force_for_period"] is True
    assert version_applicability("2026-06-01")["v2_in_force_for_period"] is False


def test_version_is_not_treated_as_a_single_switch():
    """Several V2.0 innovations are back-ported into V1, so a boolean mis-routes."""
    r = version_applicability("2027-01-01")
    assert "back-ported" in r["note"]


def test_the_v1_cutoff_is_configuration_not_a_hardcoded_assumption():
    r = version_applicability("2027-01-01", v1_cutoff="2028-01-31")
    assert r["v1_submission_cutoff"] == "2028-01-31"
    assert "disagree" in r["cutoff_note"]


def test_an_undated_period_yields_no_verdict():
    assert version_applicability(None)["v2_in_force_for_period"] is None


# --- endpoints ----------------------------------------------------------------

@pytest.fixture
def env():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from fastapi.testclient import TestClient
    from app.database import Base
    from app import main as main_mod
    from app.models import ActivityRecord, EmissionFactor, Organisation
    from app.services.calc import compute_co2e

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    def override_get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    main_mod.app.dependency_overrides[main_mod.get_db] = override_get_db
    client = TestClient(main_mod.app)
    key = client.post("/organisations", params={"name": "A"}).json()["api_key"]
    hdr = {"X-API-Key": key}

    seed = TestingSession()
    org = seed.query(Organisation).filter(Organisation.name == "A").one()
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="purchased_goods", subcategory="", unit="kg",
                       gwp_set="AR6", value=2.0, method_type="average_data")
    seed.add(f); seed.commit(); seed.refresh(f)
    for cat, qty in ((1, 100000.0), (4, 20000.0), (6, 1000.0)):
        seed.add(ActivityRecord(organisation_id=org.id, date="2027-06-01",
                                category="purchased_goods", subcategory="",
                                description="d", quantity=qty, unit="kg", geo="GB",
                                factor_id=f.id, scope="3", ghgp_category=cat,
                                mapping_basis="exact", mapping_status="approved"))
    seed.commit()
    compute_co2e(seed, org.id)
    seed.close()
    yield client, hdr
    main_mod.app.dependency_overrides.clear()


def test_report_endpoint_computes_significance(env):
    client, hdr = env
    body = client.get("/reports/sbti_v2", headers=hdr,
                      params={"turnover_eur": 500_000_000}).json()
    assert body["company_category"]["category"] == "A"
    sig = body["significance"]
    assert sig["determinable"] is True
    assert 1 in sig["significant"] and 4 in sig["significant"]
    assert 6 not in sig["significant"]        # under 5%
    assert sig["no_aggregate_floor"] is True


def test_scope2_conformance_endpoint(env):
    client, hdr = env
    r = client.post("/sbti_v2/scope2_conformance", headers=hdr,
                    params={"target_types": "LCE_ALIGNMENT"})
    assert r.status_code == 200
    assert r.json()["conformant"] is True
    assert r.json()["dual_target_required"] is False


def test_scope2_endpoint_rejects_category_c(env):
    client, hdr = env
    r = client.post("/sbti_v2/scope2_conformance", headers=hdr,
                    params={"target_types": "LCE_ALIGNMENT", "company_category": "C"})
    assert r.status_code == 400
    assert "no Category C" in r.json()["detail"]
