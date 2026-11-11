import pytest

from app.models import (EmissionFactor, ActivityRecord, Organisation, MarketInstrument,
                        ReportingPeriod)
from app.services.calc import compute_co2e
from app.reports.secr import secr_report, DIESEL_KWH_PER_LITRE_DEMO


def _org(db, name="DemoOrg"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category, unit, value, geo="GB", subcategory=""):
    f = EmissionFactor(source="DEFRA_DEMO", version="2024.1", geography=geo, year=2024,
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


@pytest.fixture
def seeded(db):
    """Sample-equivalent org: electricity 1200 kWh, gas 800 kWh, diesel 150 L."""
    org = _org(db)
    f_el = _factor(db, "electricity", "kWh", 0.170)
    f_gas = _factor(db, "gas", "kWh", 0.184)
    f_d = _factor(db, "diesel", "L", 2.676)
    _activity(db, org.id, f_el.id, "electricity", 1200, "kWh")
    _activity(db, org.id, f_gas.id, "gas", 800, "kWh")
    _activity(db, org.id, f_d.id, "diesel", 150, "L")
    # Scoped to a reporting year: SECR's intensity ratio divides a period-scoped total by
    # a per-period denominator, so the numerator's span has to be knowable.
    p = ReportingPeriod(organisation_id=org.id, label="FY25", start_date="2025-01-01",
                        end_date="2025-12-31", frozen=False)
    db.add(p); db.commit(); db.refresh(p)
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    return org, run


def test_secr_golden_values(db, seeded):
    org, run = seeded
    r = secr_report(db, org.id, run_id=run.id,
                    intensity_denominator=2.0, intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365)
    e = r["emissions_tco2e"]
    # Scope 1: gas 800*0.184 + diesel 150*2.676 = 147.2 + 401.4 = 548.6 kg
    assert e["scope1"] == pytest.approx(0.5486)
    # Scope 2: electricity 1200*0.170 = 204 kg (dual-reported, no instrument)
    assert e["scope2_location_based"] == pytest.approx(0.204)
    assert e["scope2_market_based"] == pytest.approx(0.204)
    assert e["scope1_and_2_location"] == pytest.approx(0.7526)
    # Energy: 1200 + 800 + 150 L * demo constant
    en = r["energy_use_kwh"]
    assert en["electricity"] == pytest.approx(1200.0)
    assert en["gas"] == pytest.approx(800.0)
    assert en["diesel"] == pytest.approx(150 * DIESEL_KWH_PER_LITRE_DEMO)
    assert en["total_kwh"] == pytest.approx(2000 + 1500)
    # Intensity: 0.7526 t / 2 = 0.3763 t per £M
    assert r["intensity_ratio"]["tco2e_scope1_and_2_location"] == pytest.approx(0.3763)
    assert r["disclosure_ready"] is True
    assert r["blockers"] == []
    assert "GHG Protocol" in r["methodology_statement"]
    assert "run #" + str(run.id) in r["methodology_statement"]


def test_secr_blocks_without_intensity_ratio(db, seeded):
    org, run = seeded
    r = secr_report(db, org.id, run_id=run.id)
    assert r["disclosure_ready"] is False
    assert any("intensity ratio" in b for b in r["blockers"])


def test_secr_blocks_partial_run(db, seeded):
    org, run0 = seeded
    _activity(db, org.id, None, "widgets", 5, "kg")     # unmapped
    run = compute_co2e(db, org.id)
    r = secr_report(db, org.id, run_id=run.id, intensity_denominator=1.0,
                    intensity_denominator_period_days=365)
    assert r["disclosure_ready"] is False
    assert any("PARTIAL" in b for b in r["blockers"])
    assert any("coverage" in b for b in r["blockers"])


def test_secr_blocks_stale_run(db, seeded):
    org, run = seeded
    f_el = db.query(EmissionFactor).filter(EmissionFactor.category == "electricity").first()
    _activity(db, org.id, f_el.id, "electricity", 999, "kWh")   # added AFTER run
    r = secr_report(db, org.id, run_id=run.id, intensity_denominator=1.0,
                    intensity_denominator_period_days=365)
    assert r["disclosure_ready"] is False
    assert any("STALE" in b for b in r["blockers"])


def test_secr_market_based_reflects_rec(db, seeded):
    org, _ = seeded
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=1200.0,
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    run = compute_co2e(db, org.id)
    r = secr_report(db, org.id, run_id=run.id, intensity_denominator=1.0,
                    intensity_denominator_period_days=365)
    e = r["emissions_tco2e"]
    assert e["scope2_location_based"] == pytest.approx(0.204)
    assert e["scope2_market_based"] == pytest.approx(0.0)       # REC-covered
    assert e["total_market_based"] == pytest.approx(0.5486)     # scope 1 only


def test_secr_no_run_yet(db):
    org = _org(db)
    r = secr_report(db, org.id)
    assert r["disclosure_ready"] is False
    assert any("no calculation run" in b for b in r["blockers"])


# --- The scope-declaration gate ------------------------------------------------
# A preparer whose activity categories fall outside the ~28-key SCOPE_RULES table got
# every line classified `assumed_scope3`, and this renderer published Scope 1 = 0.0,
# Scope 2 = 0.0 and an intensity ratio of 0.0 as disclosure_ready with blockers == [] —
# printing the caveat that named those very activities in the same payload.

def _unrecognised_run(db):
    """A run whose every category MISSES SCOPE_RULES: real sources, local names."""
    org = _org(db, "UnrecognisedCats")
    for cat, unit, val, qty in [("purchased_steam_site_a", "kWh", 0.20, 50_000),
                                ("r410a_topup", "kg", 2088.0, 12),
                                ("site_generator_fuel", "L", 2.68, 3_000)]:
        f = _factor(db, cat, unit, val)
        _activity(db, org.id, f.id, cat, qty, unit)
    p = ReportingPeriod(organisation_id=org.id, label="FY25", start_date="2025-01-01",
                        end_date="2025-12-31", frozen=False)
    db.add(p); db.commit(); db.refresh(p)
    return org, compute_co2e(db, org.id, reporting_period_id=p.id), p


def test_secr_refuses_a_zero_scope1_2_it_inferred(db):
    """The gate. Reverting it to the caveat-only form must fail this test."""
    org, run, _ = _unrecognised_run(db)
    r = secr_report(db, org.id, run_id=run.id, intensity_denominator=2.0,
                    intensity_denominator_unit="GBP M revenue",
                    intensity_denominator_period_days=365)
    e = r["emissions_tco2e"]
    # The three statutorily mandated figures really are all zero...
    assert e["scope1"] == 0.0
    assert e["scope2_location_based"] == 0.0
    assert r["intensity_ratio"]["tco2e_scope1_and_2_location"] == 0.0
    # ...while the whole inventory sits in Scope 3, so the zeros are a GUESS.
    assert e["scope3_voluntary"] > 0.0
    # Therefore: refused, with a reason naming the assumed activities.
    assert r["disclosure_ready"] is False
    _b = [b for b in r["blockers"] if "ASSUMED" in b]
    assert _b, f"SECR certified three inferred zeros; blockers were {r['blockers']}"
    for cat in ("purchased_steam_site_a", "r410a_topup", "site_generator_fuel"):
        assert cat in _b[0], f"{cat} was assumed but is not named in the refusal"


def test_secr_still_ready_when_every_scope_is_known(db, seeded):
    """Negative control on the gate itself: it must not block a clean run.

    A gate that refused everything would pass the test above while making the
    renderer useless — the failure mode this codebase calls failing open in reverse.
    """
    org, run = seeded
    r = secr_report(db, org.id, run_id=run.id, intensity_denominator=2.0,
                    intensity_denominator_unit="GBP M revenue",
                    intensity_denominator_period_days=365)
    assert r["disclosure_ready"] is True
    assert r["blockers"] == []
    assert not any("ASSUMED" in b for b in r["blockers"])


def test_declaring_the_scope_clears_the_gate_and_restores_the_figures(db):
    """The discharge must actually work end-to-end, or the gate is a wall.

    This is the branch `ActivityRecord.scope` documented but nothing could write.
    """
    org, run, p = _unrecognised_run(db)
    assert secr_report(db, org.id, run_id=run.id, intensity_denominator=2.0,
                       intensity_denominator_period_days=365)["disclosure_ready"] is False

    # What POST /activities/scope writes: the preparer's own declaration.
    for cat, scope in [("purchased_steam_site_a", "2"), ("r410a_topup", "1"),
                       ("site_generator_fuel", "1")]:
        for a in db.query(ActivityRecord).filter(ActivityRecord.organisation_id == org.id,
                                                 ActivityRecord.category == cat).all():
            a.scope = scope
    db.commit()
    run2 = compute_co2e(db, org.id, reporting_period_id=p.id)

    r2 = secr_report(db, org.id, run_id=run2.id, intensity_denominator=2.0,
                     intensity_denominator_unit="GBP M revenue",
                     intensity_denominator_period_days=365)
    e2 = r2["emissions_tco2e"]
    # Scope 1: refrigerant 12 * 2088 + generator fuel 3000 * 2.68 = 25056 + 8040 = 33096 kg
    assert e2["scope1"] == pytest.approx(33.096)
    # Scope 2: purchased steam 50000 * 0.20 = 10000 kg
    assert e2["scope2_location_based"] == pytest.approx(10.0)
    assert e2["scope3_voluntary"] == pytest.approx(0.0)
    # The mandatory total is now the whole inventory, not zero.
    assert e2["scope1_and_2_location"] == pytest.approx(43.096)
    assert r2["disclosure_ready"] is True, r2["blockers"]
    assert r2["scope_assumptions"] is None


def test_a_declared_scope_makes_an_existing_run_stale(db):
    """The endpoint's own promise: the declaration is in the activity fingerprint.

    Without it, a preparer could re-declare a scope and the staleness gate would
    report the filed run as current while the live declaration contradicted it.
    """
    from app.reports.summary import summary
    org, run, p = _unrecognised_run(db)
    assert summary(db, org.id, run_id=run.id)["coverage"]["stale"] is False
    for a in db.query(ActivityRecord).filter(
            ActivityRecord.organisation_id == org.id,
            ActivityRecord.category == "r410a_topup").all():
        a.scope = "1"
    db.commit()
    cov = summary(db, org.id, run_id=run.id)["coverage"]
    assert cov["staleness_assessable"] is True
    assert cov["stale"] is True, "a scope declaration left the filed run reading as current"


def test_secr_refuses_when_scope_source_was_never_frozen(db, seeded):
    """A line predating the scope_source freeze is UNASSESSABLE, never a pass.

    Reading the absence of the key as 'not assumed' is the exact fail-open shape this
    gate exists to close, so the legacy case must refuse too.
    """
    import json as _json
    from app.models import EmissionLineItem
    org, run = seeded
    for li in db.query(EmissionLineItem).filter(EmissionLineItem.run_id == run.id,
                                                EmissionLineItem.method == "location").all():
        d = _json.loads(li.details) if isinstance(li.details, str) else dict(li.details or {})
        d.pop("scope_source", None)
        li.details = _json.dumps(d)
    db.commit()
    r = secr_report(db, org.id, run_id=run.id, intensity_denominator=2.0,
                    intensity_denominator_period_days=365)
    assert r["disclosure_ready"] is False
    assert any("predate the scope_source freeze" in b for b in r["blockers"]), r["blockers"]
