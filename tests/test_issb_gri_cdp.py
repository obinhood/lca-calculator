import pytest

from app.models import EmissionFactor, ActivityRecord, Organisation
from app.services.calc import compute_co2e
from app.reports.issb_s2 import issb_s2_report, JURISDICTION_PROFILES
from app.reports.gri import gri_report
from app.reports.cdp import cdp_export


def _org(db, name="DemoOrg"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category, unit, value, subcategory="", **kw):
    f = EmissionFactor(source="DEFRA_DEMO", version="2024.1", geography="GB", year=2024,
                       category=category, subcategory=subcategory, unit=unit,
                       gwp_set="AR6", value=value, **kw)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor_id, category, quantity, unit):
    a = ActivityRecord(organisation_id=org_id, date="2025-01-01", category=category,
                       subcategory="", description="", quantity=quantity, unit=unit,
                       geo="GB", factor_id=factor_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


@pytest.fixture
def seeded(db):
    """electricity 1200 kWh (S2), gas 800 kWh (S1), waste 250 kg (S3, Cat 5).

    Period-scoped, fully-screened, disclosure-ready run."""
    from tests.scope3_util import ready_run
    org = _org(db)
    _activity(db, org.id, _factor(db, "electricity", "kWh", 0.170).id,
              "electricity", 1200, "kWh")
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 800, "kWh")
    waste = _activity(db, org.id, _factor(db, "waste", "kg", 0.480).id, "waste", 250, "kg")
    waste.ghgp_category = 5
    db.commit()
    run, _period = ready_run(db, org.id)
    return org, run


# --- ISSB S2 ---

def test_issb_s2_golden_values_and_profiles(db, seeded):
    org, run = seeded
    r = issb_s2_report(db, org.id, run_id=run.id, jurisdiction="JP_SSBJ")
    g = r["ghg_emissions_tco2e"]
    assert g["scope1_gross"] == pytest.approx(0.1472)          # gas 800*0.184
    assert g["scope2_location_based_gross"] == pytest.approx(0.204)
    assert g["scope2_market_based_information"] == pytest.approx(0.204)
    assert g["scope3_gross"] == pytest.approx(0.120)
    assert g["biogenic_co2_separate"] == pytest.approx(0.0)
    assert r["jurisdiction_profile"]["key"] == "JP_SSBJ"
    assert "SSBJ" in r["jurisdiction_profile"]["name"]
    assert r["disclosure_ready"] is True
    # every profile renders the same numbers
    for j in JURISDICTION_PROFILES:
        assert issb_s2_report(db, org.id, run_id=run.id, jurisdiction=j)[
            "ghg_emissions_tco2e"]["scope1_gross"] == g["scope1_gross"]


def test_issb_s2_gates_ar5_run(db, seeded):
    """S2 expects latest IPCC GWPs — an AR5 run must be gated, not passed."""
    org, _ = seeded
    f = _factor(db, "electricity", "kWh", 0.17, kg_co2=0.168337, kg_ch4=0.00001,
                kg_n2o=0.000005, ch4_origin="fossil")
    _activity(db, org.id, f.id, "electricity", 100, "kWh")
    run5 = compute_co2e(db, org.id, gwp_set="AR5")
    r = issb_s2_report(db, org.id, run_id=run5.id)
    assert any("AR6" in b for b in r["blockers"])


def test_issb_s2_unknown_jurisdiction_blocked(db, seeded):
    org, run = seeded
    r = issb_s2_report(db, org.id, run_id=run.id, jurisdiction="MARS")
    assert r["disclosure_ready"] is False
    assert any("unknown jurisdiction" in b for b in r["blockers"])


# --- GRI ---

def test_gri_golden_values(db, seeded):
    org, run = seeded
    # The denominator carries its own period (the seeded run is FY25, 365 days) — 305-4
    # over a denominator covering a different span is a ratio out by that ratio.
    r = gri_report(db, org.id, run_id=run.id, intensity_denominator=2.0,
                   intensity_denominator_unit="FTE (hundreds)",
                   intensity_denominator_period_days=365)
    assert r["gri_305_1_scope1"]["gross_tco2e"] == pytest.approx(0.1472)
    assert r["gri_305_2_scope2"]["location_based_tco2e"] == pytest.approx(0.204)
    assert r["gri_305_3_scope3"]["by_ghgp_category_tco2e"]["5"] == pytest.approx(0.120)
    # 302-1: scope 1/2 energy only (gas + electricity), waste has none
    assert r["gri_302_1_energy"]["total_mwh"] == pytest.approx(2.0)
    assert r["gri_305_4_intensity"]["tco2e_per_unit"] == pytest.approx(0.4712 / 2.0)
    assert r["disclosure_ready"] is True


def test_gri_305_5_reductions_between_immutable_runs(db, seeded):
    org, base_run = seeded
    # Retire the waste activity's emissions by remapping to a lower factor.
    lower = _factor(db, "waste", "kg", 0.100)
    a = db.query(ActivityRecord).filter(ActivityRecord.category == "waste").first()
    a.factor_id = lower.id; db.commit()
    new_run = compute_co2e(db, org.id)
    r = gri_report(db, org.id, run_id=new_run.id, base_run_id=base_run.id,
                   intensity_denominator=1.0,
                   intensity_denominator_period_days=365)
    red = r["gri_305_5_reductions"]
    # base waste 120 kg -> new 25 kg: reduction 95 kg = 0.095 t
    assert red["reduction_location_based_tco2e"] == pytest.approx(0.095)
    assert red["base_run_id"] == base_run.id


def test_gri_305_5_blocks_cross_gwp_comparison(db, seeded):
    org, base_run = seeded                                     # AR6
    f = _factor(db, "electricity", "kWh", 0.17, kg_co2=0.168337, kg_ch4=0.00001,
                kg_n2o=0.000005, ch4_origin="fossil")
    _activity(db, org.id, f.id, "electricity", 100, "kWh")
    run5 = compute_co2e(db, org.id, gwp_set="AR5")
    r = gri_report(db, org.id, run_id=run5.id, base_run_id=base_run.id,
                   intensity_denominator=1.0,
                   intensity_denominator_period_days=365)
    assert any("GWP" in b for b in r["blockers"])


def test_gri_base_run_is_org_scoped(db, seeded):
    org, run = seeded
    other = _org(db, "Other")
    f = _factor(db, "electricity", "kWh", 0.17)
    _activity(db, other.id, f.id, "electricity", 10, "kWh")
    other_run = compute_co2e(db, other.id)
    r = gri_report(db, org.id, run_id=run.id, base_run_id=other_run.id,
                   intensity_denominator=1.0,
                   intensity_denominator_period_days=365)
    assert any("base_run_id not found" in b for b in r["blockers"])


# --- CDP ---

def test_cdp_export_golden_values(db, seeded):
    org, run = seeded
    r = cdp_export(db, org.id, run_id=run.id, intensity_denominator=2.0,
                   intensity_denominator_period_days=365,
                   verification_status="limited_assurance")
    a = r["answers"]
    assert a["C6.1_scope1_gross_tco2e"] == pytest.approx(0.1472)
    assert a["C6.3_scope2_location_tco2e"] == pytest.approx(0.204)
    assert a["C6.3_scope2_market_tco2e"] == pytest.approx(0.204)
    assert a["C6.5_scope3_tco2e"] == pytest.approx(0.120)
    # CDP C6.10 is gross global combined SCOPE 1 AND 2 per unit revenue — NOT the whole
    # inventory. This assertion previously read 0.4712/2 (S1+S2+S3), which inflated the
    # filed ratio by the entire value chain.
    assert a["C6.10_intensity"]["tco2e_per_unit"] == pytest.approx((0.1472 + 0.204) / 2.0)
    assert a["C6.10_intensity"]["numerator_tco2e"] == pytest.approx(0.3512)
    assert "NOT the whole inventory" in a["C6.10_intensity"]["numerator_basis"]
    assert a["C10.1_verification_status"] == "limited_assurance"
    assert r["submission_ready"] is True
    assert "verify mapping" in r["questionnaire_note"]


def test_cdp_blocks_partial_and_missing_denominator(db, seeded):
    org, _ = seeded
    _activity(db, org.id, None, "widgets", 5, "kg")            # unmapped
    run = compute_co2e(db, org.id)
    r = cdp_export(db, org.id, run_id=run.id)
    assert r["submission_ready"] is False
    assert any("PARTIAL" in b for b in r["blockers"])
    assert any("intensity_denominator" in b for b in r["blockers"])


# --- The scope-declaration gate ------------------------------------------------
# Every figure these three frameworks key by scope was published over a split the
# ENGINE GUESSED: an activity category outside SCOPE_RULES defaults to Scope 3, so
# Scope 1 and Scope 2 both read 0.0 and the whole inventory lands in Scope 3 — with
# disclosure_ready true and blockers empty, beside a scope_assumptions caveat naming
# the very activities that were guessed.
#
# Each refusal test below also asserts that NO Scope-3-category blocker is present.
# On a bare fixture these renderers do block, but on the GHGP category gate — a
# different fact with a different remedy, whose own instruction ("set
# activities.ghgp_category") clears it while leaving Scope 2 at 0.0. The fixture
# discharges it on purpose, so a test that passed because of it would be lying.

def test_issb_s2_refuses_a_scope_split_it_guessed(db):
    """IFRS S2 29(a) mandates Scope 1/2/3 separately. Reverting the gate must fail this."""
    from tests.scope3_util import assumed_scope_run, ASSUMED_CATEGORIES
    org, run = assumed_scope_run(db)
    r = issb_s2_report(db, org.id, run_id=run.id)
    g = r["ghg_emissions_tco2e"]
    # The two mandated gross figures really are zero...
    assert g["scope1_gross"] == 0.0
    assert g["scope2_location_based_gross"] == 0.0
    # ...while the entire inventory sits in Scope 3, so those zeros are a GUESS.
    assert g["scope3_gross"] > 0.0
    assert r["disclosure_ready"] is False
    _b = [b for b in r["blockers"] if "ASSUMED" in b]
    assert _b, f"S2 certified a guessed scope split; blockers were {r['blockers']}"
    for cat in ASSUMED_CATEGORIES:
        assert cat in _b[0], f"{cat} was assumed but is not named in the refusal"
    assert not any("ghgp_category" in b for b in r["blockers"]), \
        "fixture did not discharge the Scope 3 category gate — wrong blocker under test"


def test_issb_s2_still_ready_when_every_scope_is_known(db):
    """Negative control: the same three sources under RECOGNISED category names."""
    from tests.scope3_util import declared_scope_run
    org, run = declared_scope_run(db)
    r = issb_s2_report(db, org.id, run_id=run.id)
    assert r["disclosure_ready"] is True, r["blockers"]
    assert r["blockers"] == []
    # The split is derived from a rule, so the figures come back rather than collapsing.
    g = r["ghg_emissions_tco2e"]
    assert g["scope1_gross"] > 0.0
    assert g["scope2_location_based_gross"] > 0.0


def test_gri_refuses_a_scope_split_it_guessed(db):
    """305-1/-2/-3 are three disclosures by scope and 305-4 divides 305-1 + 305-2."""
    from tests.scope3_util import assumed_scope_run, ASSUMED_CATEGORIES
    org, run = assumed_scope_run(db)
    r = gri_report(db, org.id, run_id=run.id, intensity_denominator=2.0,
                   intensity_denominator_unit="revenue_musd",
                   intensity_denominator_period_days=365)
    assert r["gri_305_1_scope1"]["gross_tco2e"] == 0.0
    assert r["gri_305_2_scope2"]["location_based_tco2e"] == 0.0
    assert r["gri_305_3_scope3"]["gross_tco2e"] > 0.0
    assert r["disclosure_ready"] is False
    _b = [b for b in r["blockers"] if "ASSUMED" in b]
    assert _b, f"GRI certified a guessed scope split; blockers were {r['blockers']}"
    for cat in ASSUMED_CATEGORIES:
        assert cat in _b[0], f"{cat} was assumed but is not named in the refusal"
    assert not any("ghgp_category" in b for b in r["blockers"]), \
        "fixture did not discharge the Scope 3 category gate — wrong blocker under test"


def test_gri_still_ready_when_every_scope_is_known(db):
    from tests.scope3_util import declared_scope_run
    org, run = declared_scope_run(db)
    r = gri_report(db, org.id, run_id=run.id, intensity_denominator=2.0,
                   intensity_denominator_unit="revenue_musd",
                   intensity_denominator_period_days=365)
    assert r["disclosure_ready"] is True, r["blockers"]
    assert r["blockers"] == []
    assert r["gri_305_1_scope1"]["gross_tco2e"] > 0.0
    assert r["gri_305_2_scope2"]["location_based_tco2e"] > 0.0


def test_cdp_refuses_a_scope_split_it_guessed(db):
    """C6.1/C6.3/C6.5 are scope-keyed questionnaire rows: the response IS the split."""
    from tests.scope3_util import assumed_scope_run, ASSUMED_CATEGORIES
    org, run = assumed_scope_run(db)
    r = cdp_export(db, org.id, run_id=run.id, intensity_denominator=2.0,
                   intensity_denominator_unit="revenue_musd",
                   intensity_denominator_period_days=365)
    m = r["answers"]
    assert m["C6.1_scope1_gross_tco2e"] == 0.0
    assert m["C6.3_scope2_location_tco2e"] == 0.0
    assert m["C6.5_scope3_excl_financed_tco2e"] > 0.0
    assert r["submission_ready"] is False
    _b = [b for b in r["blockers"] if "ASSUMED" in b]
    assert _b, f"CDP certified a guessed scope split; blockers were {r['blockers']}"
    for cat in ASSUMED_CATEGORIES:
        assert cat in _b[0], f"{cat} was assumed but is not named in the refusal"
    assert not any("ghgp_category" in b for b in r["blockers"]), \
        "fixture did not discharge the Scope 3 category gate — wrong blocker under test"


def test_cdp_still_ready_when_every_scope_is_known(db):
    from tests.scope3_util import declared_scope_run
    org, run = declared_scope_run(db)
    r = cdp_export(db, org.id, run_id=run.id, intensity_denominator=2.0,
                   intensity_denominator_unit="revenue_musd",
                   intensity_denominator_period_days=365)
    assert r["submission_ready"] is True, r["blockers"]
    assert r["blockers"] == []
    m = r["answers"]
    assert m["C6.1_scope1_gross_tco2e"] > 0.0
    assert m["C6.3_scope2_location_tco2e"] > 0.0
