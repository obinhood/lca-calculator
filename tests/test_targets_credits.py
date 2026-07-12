import pytest

from app.models import (
    EmissionFactor, ActivityRecord, Organisation, EmissionsTarget, CarbonCredit,
)
from app.services.calc import compute_co2e
from app.services.sbti import (
    linear_pathway, implied_annual_rate, assess_ambition, run_scoped_emissions_kg,
)
from app.services.neutrality import neutrality_assessment
from app.reports.sbti import sbti_report


def _org(db, name="DemoOrg"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category, unit, value, subcategory=""):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category=category, subcategory=subcategory, unit=unit,
                       gwp_set="AR6", value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor_id, category, quantity, unit):
    a = ActivityRecord(organisation_id=org_id, date="2025-01-01", category=category,
                       subcategory="", description="", quantity=quantity, unit=unit,
                       geo="GB", factor_id=factor_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


# --- SBTi pathway maths ---

def test_linear_pathway_and_endpoints():
    # base 1000, 42% reduction over 10 years
    assert linear_pathway(1000, 2020, 2030, 0.42, 2020) == pytest.approx(1000)
    assert linear_pathway(1000, 2020, 2030, 0.42, 2025) == pytest.approx(790)   # half way
    assert linear_pathway(1000, 2020, 2030, 0.42, 2030) == pytest.approx(580)
    assert linear_pathway(1000, 2020, 2030, 0.42, 2035) == pytest.approx(580)   # clamps


def test_ambition_meets_1p5c_minimum():
    # 42% over 10y = 4.2%/yr -> exactly the 1.5C minimum
    a = assess_ambition(0.42, 2020, 2030, "1.5C")
    assert a["implied_annual_linear_rate"] == pytest.approx(0.042)
    assert a["meets_minimum"] is True
    # 30% over 10y = 3.0%/yr -> below 1.5C minimum
    b = assess_ambition(0.30, 2020, 2030, "1.5C")
    assert b["meets_minimum"] is False


def test_run_scoped_emissions(db):
    org = _org(db)
    _activity(db, org.id, _factor(db, "electricity", "kWh", 0.17).id, "electricity", 1000, "kWh")  # S2
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 1000, "kWh")  # S1
    _activity(db, org.id, _factor(db, "waste", "kg", 0.48).id, "waste", 100, "kg")  # S3
    run = compute_co2e(db, org.id)
    assert run_scoped_emissions_kg(db, run.id, "1+2") == pytest.approx(170 + 184)
    assert run_scoped_emissions_kg(db, run.id, "1+2+3") == pytest.approx(170 + 184 + 48)


def test_sbti_report_trajectory_on_and_off_track(db):
    org = _org(db)
    f = _factor(db, "gas", "kWh", 0.184)
    _activity(db, org.id, f.id, "gas", 10000, "kWh")           # 1840 kg S1
    base_run = compute_co2e(db, org.id)
    t = EmissionsTarget(organisation_id=org.id, name="NT", target_type="near_term",
                        scope_coverage="1+2", base_run_id=base_run.id, base_year=2025,
                        target_year=2035, target_reduction_pct=0.42, ambition="1.5C")
    db.add(t); db.commit(); db.refresh(t)
    # A later run reduced to 5000 kWh (920 kg): pathway at 2030 = 1840*(1-0.21)=1453.6
    a = db.query(ActivityRecord).first(); a.quantity = 5000; db.commit()
    cur = compute_co2e(db, org.id)
    r = sbti_report(db, org.id, t.id, current_run_id=cur.id, current_year=2030)
    assert r["ok"] is True
    assert r["ambition_assessment"]["meets_minimum"] is True
    assert r["trajectory"]["pathway_allowed_tco2e"] == pytest.approx(1.4536)
    assert r["trajectory"]["actual_tco2e"] == pytest.approx(0.92)
    assert r["trajectory"]["on_track"] is True
    # Now overshoot: current run back to base -> off track
    a.quantity = 10000; db.commit()
    over = compute_co2e(db, org.id)
    r2 = sbti_report(db, org.id, t.id, current_run_id=over.id, current_year=2030)
    assert r2["trajectory"]["on_track"] is False


def test_sbti_blocks_cross_gwp_trajectory(db):
    org = _org(db)
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 1000, "kWh")
    base = compute_co2e(db, org.id)                            # AR6
    t = EmissionsTarget(organisation_id=org.id, name="x", target_type="near_term",
                        scope_coverage="1+2", base_run_id=base.id, base_year=2025,
                        target_year=2035, target_reduction_pct=0.42, ambition="1.5C")
    db.add(t); db.commit(); db.refresh(t)
    fpg = _factor(db, "gas", "kWh", 0.184)  # aggregate AR6 -> AR5 run mismatches
    # use a per-gas factor so the AR5 run actually computes
    fpg.kg_co2 = 0.18233; fpg.kg_ch4 = 0.0003; fpg.kg_n2o = 0.00001
    fpg.ch4_origin = "fossil"; db.commit()
    a = db.query(ActivityRecord).first(); a.factor_id = fpg.id; db.commit()
    ar5 = compute_co2e(db, org.id, gwp_set="AR5")
    r = sbti_report(db, org.id, t.id, current_run_id=ar5.id, current_year=2030)
    assert any("GWP" in b for b in r["blockers"])


# --- Carbon credits + ISO 14068 neutrality ---

def _mixed_run(db):
    org = _org(db)
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 10000, "kWh")  # 1840 kg = 1.84 t
    run = compute_co2e(db, org.id)
    return org, run


def _credit(db, org_id, qty, ctype="removal", ccp=True, retired=False, run_id=None):
    c = CarbonCredit(organisation_id=org_id, registry="verra", quantity_tco2e=qty,
                     credit_type=ctype, ccp_approved=ccp, retired=retired,
                     applied_to_run_id=run_id, vintage_year=2024)
    db.add(c); db.commit(); db.refresh(c)
    return c


def test_neutrality_only_counts_retired_applied_credits(db):
    org, run = _mixed_run(db)   # gross 1.84 t
    _credit(db, org.id, 1.0, retired=False)                    # not retired -> ignored
    _credit(db, org.id, 1.0, retired=True, run_id=None)        # retired, not applied -> ignored
    n = neutrality_assessment(db, org.id, run)
    assert n["gross_tco2e"] == pytest.approx(1.84)
    assert n["credits_applied_tco2e"] == 0.0
    assert n["neutral"] is False
    assert n["unretired_credits_in_register"] == 1


def test_neutrality_removals_conformant(db):
    org, run = _mixed_run(db)
    _credit(db, org.id, 2.0, ctype="removal", ccp=True, retired=True, run_id=run.id)
    n = neutrality_assessment(db, org.id, run)
    assert n["neutral"] is True
    assert n["residual_tco2e"] == pytest.approx(-0.16)         # 2.0 - 1.84
    assert n["iso14068_conformant_claim"] is True
    # ECGT product-claim caution surfaces even when conformant
    assert any("ECGT" in w for w in n["claim_warnings"])


def test_neutrality_avoidance_and_non_ccp_flagged_not_conformant(db):
    org, run = _mixed_run(db)
    _credit(db, org.id, 2.0, ctype="avoidance", ccp=False, retired=True, run_id=run.id)
    n = neutrality_assessment(db, org.id, run)
    assert n["neutral"] is True                                # arithmetically covered
    assert n["iso14068_conformant_claim"] is False             # but avoidance + non-CCP
    assert any("avoidance" in w for w in n["claim_warnings"])
    assert any("CCP" in w for w in n["claim_warnings"])


def test_esrs_e1_7_reflects_retired_credits(db):
    from app.reports.esrs_e1 import esrs_e1_report
    org, run = _mixed_run(db)
    _credit(db, org.id, 2.0, ctype="removal", ccp=True, retired=True, run_id=run.id)
    r = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=1.0)
    e17 = r["e1_6_gross_ghg_emissions_tco2e"]  # sanity: gross untouched by credits
    assert e17["scope1"] == pytest.approx(1.84)
    e7 = r["e1_7_removals_and_credits"]
    assert e7["removals_retired_tco2e"] == pytest.approx(2.0)
    assert e7["credit_count"] == 1


# --- Phase 11 verification-panel hardening ---

def test_ambition_respects_target_type():
    # net-zero 90% by 2050 must PASS on the net-zero criterion, not the 4.2%/yr floor
    nz = assess_ambition(0.90, 2020, 2050, "1.5C", target_type="net_zero")
    assert nz["meets_minimum"] is True
    assert nz["minimum_reduction_pct"] == 0.90
    # a weak net-zero (70%) fails
    weak = assess_ambition(0.70, 2020, 2050, "1.5C", target_type="net_zero")
    assert weak["meets_minimum"] is False
    # near-term still uses the annual floor
    nt = assess_ambition(0.42, 2020, 2030, "1.5C", target_type="near_term")
    assert nt["meets_minimum"] is True


def test_scopes_from_coverage_rejects_bad_tokens():
    from app.services.sbti import scopes_from_coverage
    assert scopes_from_coverage("1+2+3") == {"1", "2", "3"}
    assert scopes_from_coverage(" 1 + 2 ") == {"1", "2"}
    with pytest.raises(ValueError):
        scopes_from_coverage("1+4")
    with pytest.raises(ValueError):
        scopes_from_coverage("1+foo")


def test_current_year_before_base_is_blocked(db):
    org = _org(db)
    _activity(db, org.id, _factor(db, "gas", "kWh", 0.184).id, "gas", 10000, "kWh")
    base = compute_co2e(db, org.id)
    t = EmissionsTarget(organisation_id=org.id, name="x", target_type="near_term",
                        scope_coverage="1+2", base_run_id=base.id, base_year=2025,
                        target_year=2035, target_reduction_pct=0.42, ambition="1.5C")
    db.add(t); db.commit(); db.refresh(t)
    r = sbti_report(db, org.id, t.id, current_run_id=base.id, current_year=1999)
    assert r["ok"] is False
    assert any("before the base year" in b for b in r["blockers"])
    assert r["trajectory"] is None                             # never silently "on track"


def test_duplicate_credit_serial_rejected(db):
    org = _org(db)
    _credit_kw = dict(organisation_id=org.id, registry="verra",
                      serial_number="VCS-1-A", quantity_tco2e=1.0, credit_type="removal")
    db.add(CarbonCredit(**_credit_kw)); db.commit()
    from sqlalchemy.exc import IntegrityError
    db.add(CarbonCredit(**_credit_kw))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_e1_7_as_of_freezes_credits_section(db):
    from app.reports.esrs_e1 import esrs_e1_report
    org, run = _mixed_run(db)
    c1 = _credit(db, org.id, 1.0, retired=True, run_id=run.id)
    c1.retirement_date = "2026-01-01T00:00:00+00:00"; db.commit()
    # A later retirement after the filing cutoff must not change the frozen section.
    c2 = _credit(db, org.id, 5.0, retired=True, run_id=run.id)
    c2.retirement_date = "2026-06-01T00:00:00+00:00"; db.commit()
    frozen = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=1.0,
                            credits_as_of="2026-03-01T00:00:00+00:00")
    e7 = frozen["e1_7_removals_and_credits"]
    assert e7["credit_count"] == 1                             # only the pre-cutoff credit
    assert e7["credits_retired_total_tco2e"] == pytest.approx(1.0)
    # without a cutoff, both count (live ledger)
    live = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=1.0)
    assert live["e1_7_removals_and_credits"]["credit_count"] == 2
