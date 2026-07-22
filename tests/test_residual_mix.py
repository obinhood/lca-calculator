"""GHG Protocol Scope 2 Guidance: the RESIDUAL MIX for uncovered market-based load.

The defect: uncovered consumption was priced at the LOCATION grid average. Residual mix
is always >= the grid average (other purchasers' clean attributes are stripped out), so
the old behaviour double counted those attributes and UNDERSTATED the market figure.
"""
import pytest

from app.models import (
    Organisation, ActivityRecord, EmissionFactor, MarketInstrument,
    ResidualMixRate, RunResidualMixStatement, CalculationRun,
)
from app.services.calc import compute_co2e
from app.services.residual_mix import (
    scope2_residual_mix_completeness, residual_mix_comparable, market_key,
    resolve_reference_rate, RESIDUAL_MIX_VERSION,
)


def _org(db, name="Co"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _elec(db, org_id, kwh=1000.0, rate=0.4, geo="DE", date="2025-06-01"):
    f = EmissionFactor(source="T", version="1", geography=geo, year=2025,
                       category="electricity", subcategory="", unit="kWh",
                       gwp_set="AR6", value=rate)
    db.add(f); db.commit(); db.refresh(f)
    a = ActivityRecord(organisation_id=org_id, date=date, category="electricity",
                       subcategory="", description="", quantity=kwh, unit="kWh",
                       geo=geo, factor_id=f.id)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _rate(db, market="DE", year=2025, kg=0.55, **kw):
    kw.setdefault("publisher", "AIB_RESIDUAL_MIX")
    kw.setdefault("status", "published")
    kw.setdefault("gas_basis", "co2e")
    r = ResidualMixRate(market=market, year=year, kg_co2e_per_kwh=kg, **kw)
    db.add(r); db.commit(); db.refresh(r)
    return r


def _stmts(db, run):
    return db.query(RunResidualMixStatement).filter(
        RunResidualMixStatement.run_id == run.id).all()


# --- The change that makes the fix reach anyone -------------------------------------

def test_an_org_with_no_instruments_at_all_is_repriced(db):
    """THE POPULATION THAT MATTERED. The market branch was guarded by
    `if is_electricity and instruments:`, so an org holding ZERO contractual instruments
    never entered the allocator: its ENTIRE market figure was just the location figure.
    That is where the understatement is 100%, and a residual leg inside the allocator
    would have been dead code for them."""
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4)          # location: 400 kg
    _rate(db, "DE", 2025, 0.55)                       # residual: 550 kg
    run = compute_co2e(db, org.id)
    assert run.total_co2e == pytest.approx(400.0)     # location UNTOUCHED
    assert run.total_co2e_market == pytest.approx(550.0)
    st = _stmts(db, run)
    assert len(st) == 1 and st[0].status == "reference_rate"
    assert st[0].kwh_priced_at_residual == pytest.approx(1000.0)
    assert st[0].kwh_priced_at_grid == pytest.approx(0.0)


def test_with_no_rate_on_file_the_number_is_unchanged_bit_for_bit(db):
    """Fail-open on the NUMBER: with no residual mix resolvable the previous
    grid-average arithmetic is kept EXACTLY — a zero-instrument org's market total must
    not drift by a ULP purely because it now walks the allocator path."""
    org = _org(db)
    _elec(db, org.id, kwh=1234.567, rate=0.4137)
    run = compute_co2e(db, org.id)
    assert run.total_co2e_market == run.total_co2e      # bit equality, not approx
    g = scope2_residual_mix_completeness(db, run)
    assert g["blockers"] == []                           # absence never blocks
    assert any("no residual mix" in w for w in g["warnings"])


def test_partial_rec_coverage_prices_the_remainder_at_residual_not_grid(db):
    """The headline case: a REC covers part of the load and the REST must take the
    residual mix. Pricing it at the grid average double counts the attributes other
    purchasers claimed."""
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=600.0, market="DE",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    _rate(db, "DE", 2025, 0.55)
    run = compute_co2e(db, org.id)
    # 600 kWh @ 0.0 + 400 kWh @ 0.55 residual = 220 kg (grid average would give 160)
    assert run.total_co2e_market == pytest.approx(220.0)
    st = _stmts(db, run)[0]
    assert st.kwh_contractual == pytest.approx(600.0)
    assert st.kwh_priced_at_residual == pytest.approx(400.0)
    assert st.status == "reference_rate"


def test_the_line_ledger_closes(db):
    """Assurer invariant: sum(allocation kwh_covered) + kwh_grid_fallback == kwh."""
    import json
    from app.models import EmissionLineItem
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=600.0, market="DE",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    _rate(db, "DE", 2025, 0.55)
    run = compute_co2e(db, org.id)
    d = json.loads(db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id, EmissionLineItem.method == "market").first().details)
    assert sum(x["kwh_covered"] for x in d["allocations"]) + d["kwh_grid_fallback"] \
        == pytest.approx(d["kwh"])
    assert d["grid_rate_kg_per_kwh"] == pytest.approx(0.4)   # frozen for the first time


# --- Anti-cliff ---------------------------------------------------------------------

def test_a_run_predating_the_requirement_is_warned_never_blocked(db):
    """The gate is re-evaluated at RENDER time on already-filed runs, so without the
    NULL-version short circuit this change would retroactively block history."""
    org = _org(db)
    _elec(db, org.id)
    run = compute_co2e(db, org.id)
    run.scope2_residual_mix_version = None
    db.commit()
    g = scope2_residual_mix_completeness(db, run)
    assert g["legacy"] is True and g["blockers"] == []
    assert any("predates" in w for w in g["warnings"])


# --- Blockers -----------------------------------------------------------------------

def test_claiming_instruments_while_grid_pricing_the_rest_blocks(db):
    """RM-B2: the org takes credit for attributes in a market while pricing the rest as
    if the grid still held average attributes — that IS the double count, and here the
    org's own claim creates it."""
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=600.0, market="DE",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()                                   # NO residual mix on file
    run = compute_co2e(db, org.id)
    b = " ".join(scope2_residual_mix_completeness(db, run)["blockers"])
    assert "double counts the attributes others claimed" in b


def test_market_unknown_blocks_because_it_is_org_fixable(db):
    """RM-B1: the platform cannot look up a market it was never told."""
    org = _org(db)
    _elec(db, org.id, geo=None)
    run = compute_co2e(db, org.id)
    b = " ".join(scope2_residual_mix_completeness(db, run)["blockers"])
    assert "no market (set activities.geo)" in b


def test_a_residual_below_the_grid_average_blocks(db):
    """RM-B3: arithmetically impossible for a correct residual mix — it has other
    purchasers' CLEAN attributes removed, so it cannot be cleaner than the average."""
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4)
    _rate(db, "DE", 2025, 0.20)                    # below the 0.4 grid average
    run = compute_co2e(db, org.id)
    b = " ".join(scope2_residual_mix_completeness(db, run)["blockers"])
    assert "is BELOW the grid average" in b
    # ...and the rate is still applied AS PUBLISHED — never max()-ed up, which would
    # be inventing a number.
    assert run.total_co2e_market == pytest.approx(200.0)


def test_an_org_rate_undercutting_the_published_one_blocks(db):
    """RM-B4, frozen-vs-frozen: the reference rate is resolved and frozen even when an
    org instrument supplied the rate actually applied."""
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="residual_mix",
                            kg_co2e_per_kwh=0.45, coverage_kwh=None, market="DE",
                            rate_source="Supplier letter 2026-01", gwp_set="AR6",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    _rate(db, "DE", 2025, 0.55)                    # published is HIGHER than the org's
    run = compute_co2e(db, org.id)
    st = _stmts(db, run)[0]
    assert st.status == "org_instrument"
    assert st.reference_rate_kg_co2e_per_kwh == pytest.approx(0.55)
    b = " ".join(scope2_residual_mix_completeness(db, run)["blockers"])
    assert "BELOW the published residual mix" in b


def test_an_in_place_edit_of_the_append_only_table_blocks(db):
    """RM-B5: residual_mix_rates is append-only BY CONTRACT — a correction must be an
    INSERT, or a filed figure no longer reproduces from the series as entered."""
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4)
    r = _rate(db, "DE", 2025, 0.55)
    run = compute_co2e(db, org.id)
    assert scope2_residual_mix_completeness(db, run)["blockers"] == []
    r.kg_co2e_per_kwh = 0.60                        # edited IN PLACE
    db.commit()
    b = " ".join(scope2_residual_mix_completeness(db, run)["blockers"])
    assert "EDITED IN PLACE" in b and "append-only" in b


# --- Resolution -------------------------------------------------------------------

def test_market_key_is_opaque_and_shared_with_the_instrument_matcher():
    assert market_key(" de ") == "DE"
    assert market_key("") is None and market_key(None) is None
    # No hierarchy: broadening would invent a rate for a grid nobody published.
    assert market_key("DE-BW") == "DE-BW"


def test_exact_gwp_vintage_beats_a_vintage_less_row_regardless_of_insertion_order(db):
    """A single id-ordered query would let a NEWER vintage-less row shadow an older
    EXACT-vintage one purely by when it was typed."""
    _rate(db, "DE", 2025, 0.50, gwp_set="AR6")
    _rate(db, "DE", 2025, 0.90, gwp_set=None)       # inserted LATER, no vintage
    got = resolve_reference_rate(db, "DE", 2025, "AR6")
    assert got["rate"] == pytest.approx(0.50) and got["gwp_match"] == "matched"


def test_no_broadening_across_market_or_year(db):
    _rate(db, "DE", 2025, 0.55)
    assert resolve_reference_rate(db, "DE-BW", 2025, "AR6")["rate"] is None
    assert resolve_reference_rate(db, "DE", 2024, "AR6")["rate"] is None


def test_an_attested_absence_is_a_first_class_fact(db):
    org = _org(db)
    _elec(db, org.id)
    _rate(db, "DE", 2025, None, status="not_published",
          publication="AIB publishes no residual mix for this market in 2025.")
    run = compute_co2e(db, org.id)
    st = _stmts(db, run)[0]
    assert st.status == "not_published" and st.rate_kg_co2e_per_kwh is None
    g = scope2_residual_mix_completeness(db, run)
    assert g["blockers"] == [] and any("no residual mix is published" in w
                                       for w in g["warnings"])


# --- Comparability (the second-consumer trap) ---------------------------------------

def test_reductions_across_a_residual_mix_policy_change_are_refused(db):
    """A year-on-year market-based 'reduction' spanning the methodology change is an
    artefact, not abatement — the same trap the GWP-vintage guard already closes."""
    org = _org(db)
    _elec(db, org.id)
    base = compute_co2e(db, org.id)                  # no rate on file -> nothing repriced
    _rate(db, "DE", 2025, 0.55)                      # the table gains a rate...
    run = compute_co2e(db, org.id)                   # ...so THIS run prices at residual
    assert run.total_co2e_market != base.total_co2e_market
    assert residual_mix_comparable(db, base, run) is not None
    from app.reports.gri import gri_report
    r = gri_report(db, org.id, run_id=run.id, base_run_id=base.id,
                   intensity_denominator=1.0)
    assert any("not comparable" in b for b in r["blockers"])
    assert residual_mix_comparable(db, run, run) is None


def test_comparability_fires_on_EVIDENCE_not_on_the_version_stamp(db):
    """Regression (review, HIGH): keying on the stamp alone blocked the 305-5 disclosure of
    EVERY organisation, because nothing is back-filled so every pre-existing base run is
    NULL — including orgs with no electricity at all and the day-one case where no rate
    exists and therefore nothing moved."""
    org = _org(db)
    _elec(db, org.id)
    base = compute_co2e(db, org.id)
    base.scope2_residual_mix_version = None          # a pre-requirement base year
    db.commit()
    run = compute_co2e(db, org.id)                   # still no rate on file
    assert run.scope2_residual_mix_version == RESIDUAL_MIX_VERSION
    # Versions differ, but NOTHING was priced at a residual in either run, so the market
    # figure did not move and the comparison is perfectly valid.
    assert run.total_co2e_market == pytest.approx(base.total_co2e_market)
    assert residual_mix_comparable(db, base, run) is None
    from app.reports.gri import gri_report
    r = gri_report(db, org.id, run_id=run.id, base_run_id=base.id,
                   intensity_denominator=1.0)
    assert not any("not comparable" in b for b in r["blockers"])


def test_an_org_with_no_electricity_is_never_blocked_on_comparability(db):
    org = _org(db)
    f = EmissionFactor(source="T", version="1", geography="GB", year=2025, category="gas",
                       subcategory="", unit="kWh", gwp_set="AR6", value=0.2)
    db.add(f); db.commit(); db.refresh(f)
    db.add(ActivityRecord(organisation_id=org.id, date="2025-06-01", category="gas",
                          subcategory="", description="", quantity=100, unit="kWh",
                          geo="GB", factor_id=f.id))
    db.commit()
    base = compute_co2e(db, org.id); run = compute_co2e(db, org.id)
    base.scope2_residual_mix_version = None; db.commit()
    assert residual_mix_comparable(db, base, run) is None


def test_multiple_org_residual_instruments_all_land_in_the_ledger(db):
    """Regression (review): only the FIRST org residual leg was bucketed while ALL
    residual-typed legs were excluded from the contractual sum, so legs 2..n vanished —
    the frozen ledger stopped summing to consumption, grid_rate_avg inflated, and RM-B3
    fired a FALSE inversion blocker on an arithmetically correct run."""
    org = _org(db)
    _elec(db, org.id, kwh=5000.0, rate=0.4)
    for kg, cov in ((0.50, 3000.0), (0.52, None)):
        db.add(MarketInstrument(organisation_id=org.id, instrument_type="residual_mix",
                                kg_co2e_per_kwh=kg, coverage_kwh=cov, market="DE",
                                rate_source="supplier letter", gwp_set="AR6",
                                start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    run = compute_co2e(db, org.id)
    assert run.total_co2e_market == pytest.approx(3000 * 0.50 + 2000 * 0.52)
    st = _stmts(db, run)[0]
    # The frozen ledger accounts for ALL 5000 kWh...
    assert (st.kwh_contractual + st.kwh_priced_at_residual
            + st.kwh_priced_at_grid) == pytest.approx(5000.0)
    # ...and the grid average is the real one, not inflated by a short denominator.
    assert st.grid_rate_avg_kg_per_kwh == pytest.approx(0.4)
    assert scope2_residual_mix_completeness(db, run)["blockers"] == []


def test_a_mixed_bucket_still_reports_its_grid_priced_remainder(db):
    """Regression (review, HIGH): rules keyed on `status` became unreachable for any market
    that had an org residual leg, however much load still fell through to the grid."""
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4, geo="DE")
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="residual_mix",
                            kg_co2e_per_kwh=0.6, coverage_kwh=400.0, market="DE",
                            rate_source="supplier letter", gwp_set="AR6",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()                                       # no reference rate -> 600 kWh at grid
    run = compute_co2e(db, org.id)
    st = _stmts(db, run)[0]
    assert st.kwh_priced_at_residual == pytest.approx(400.0)
    assert st.kwh_priced_at_grid == pytest.approx(600.0)
    g = scope2_residual_mix_completeness(db, run)
    assert any("priced at the grid average" in w for w in g["warnings"])


def test_an_org_rate_equal_to_the_published_one_is_not_blocked(db):
    """Regression (review): a float-derived weighted average compared with NO tolerance
    blocked an org whose rate EQUALS the published one, with an unfixable message."""
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="residual_mix",
                            kg_co2e_per_kwh=0.55, coverage_kwh=None, market="DE",
                            rate_source="supplier letter", gwp_set="AR6",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    _rate(db, "DE", 2025, 0.55)
    run = compute_co2e(db, org.id)
    assert scope2_residual_mix_completeness(db, run)["blockers"] == []


def test_a_claim_in_one_year_does_not_arm_the_blocker_for_another(db):
    """Regression (review, HIGH): claimed_markets ignored the year, so a contractual claim
    in one year armed the double-count blocker for EVERY other year of that market —
    including the just-closed year whose residual mix cannot be published yet, which the
    module docstring says must never block."""
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4, date="2024-06-01")     # claimed year
    _elec(db, org.id, kwh=1000.0, rate=0.4, date="2025-06-01")     # just-closed year
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=1000.0, market="DE",
                            start_date="2024-01-01", end_date="2024-12-31"))
    db.commit()
    _rate(db, "DE", 2024, 0.55)          # 2024 published; 2025 not yet, as is normal
    run = compute_co2e(db, org.id)
    b = " ".join(scope2_residual_mix_completeness(db, run)["blockers"])
    assert "double counts" not in b


def test_an_attested_absence_edited_into_a_published_rate_is_detected(db):
    """Regression (review, LOW): RM-B5 short-circuited when the frozen reference rate was
    NULL, so editing a not_published row INTO a published one was invisible."""
    org = _org(db)
    _elec(db, org.id)
    r = _rate(db, "DE", 2025, None, status="not_published",
              publication="AIB publishes no residual mix for this market in 2025.")
    run = compute_co2e(db, org.id)
    assert scope2_residual_mix_completeness(db, run)["blockers"] == []
    r.status, r.kg_co2e_per_kwh = "published", 0.55        # edited IN PLACE
    db.commit()
    b = " ".join(scope2_residual_mix_completeness(db, run)["blockers"])
    assert "EDITED IN PLACE" in b


def test_the_disclosed_kwh_account_for_all_electricity(db):
    """Regression (review): pricing the remainder at residual moved it OUT of
    kwh_grid_fallback without adding it to kwh_contractual, so a reader computing
    contractual coverage from the disclosed pair saw 100% for an org that covered 60%."""
    from app.reports.summary import summary
    from app.reports.issb_s2 import issb_s2_report
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=600.0, market="DE",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    _rate(db, "DE", 2025, 0.55)
    run = compute_co2e(db, org.id)
    s2 = summary(db, organisation_id=org.id, run_id=run.id)["scope2"]
    assert s2["kwh_contractual"] == pytest.approx(600.0)
    assert s2["kwh_residual_mix"] == pytest.approx(400.0)
    assert s2["kwh_electricity_accounted"] == pytest.approx(1000.0)
    blk = issb_s2_report(db, org.id, run_id=run.id)[
        "ghg_emissions_tco2e"]["scope2_contractual_instruments"]
    assert blk["kwh_residual_mix"] == pytest.approx(400.0)
    assert blk["kwh_electricity_accounted"] == pytest.approx(1000.0)


# --- Second-round review regressions ------------------------------------------------

def test_grid_average_is_weighted_over_the_uncovered_load_only(db):
    """Regression (review 2, HIGH): grid_rate_avg was weighted over ALL electricity
    including REC-covered load on a dirtier factor, then compared against the rate applied
    only to the uncovered remainder — a false RM-B3 inversion on a correct run."""
    org = _org(db)
    a1 = _elec(db, org.id, kwh=9000.0, rate=0.55)     # covered, dirty factor
    a2 = _elec(db, org.id, kwh=1000.0, rate=0.35)     # uncovered, clean factor
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=9000.0, market="DE",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    _rate(db, "DE", 2025, 0.40)                       # above BOTH grid rates: correct
    run = compute_co2e(db, org.id)
    st = _stmts(db, run)[0]
    assert st.grid_rate_avg_kg_per_kwh == pytest.approx(0.35)   # the uncovered load's rate
    assert scope2_residual_mix_completeness(db, run)["blockers"] == []


def test_an_attested_absence_plus_a_claim_is_never_silent(db):
    """Regression (review 2, HIGH): the blocker and the warning were not complementary, so
    the STRONGEST double-count case — claiming attributes in a market whose residual mix is
    attested as unpublished, then grid-pricing the rest — produced no blocker AND no
    warning at all."""
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=600.0, market="DE",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    _rate(db, "DE", 2025, None, status="not_published",
          publication="AIB publishes no residual mix for this market in 2025.")
    run = compute_co2e(db, org.id)
    g = scope2_residual_mix_completeness(db, run)
    assert g["blockers"] or g["warnings"]                  # never silent
    assert any("double counts" in b for b in g["blockers"])


def test_an_org_rate_undercut_is_judged_on_the_org_rate_not_a_blend(db):
    """Regression (review 2): RM-B4 keyed on `status`, which is now only the DOMINANT
    outcome, and compared a bucket blend — so an org rate undercutting the published mix
    escaped whenever reference-priced load shared the market."""
    org = _org(db)
    _elec(db, org.id, kwh=1_000_000.0, rate=0.3)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="residual_mix",
                            kg_co2e_per_kwh=0.10, coverage_kwh=100_000.0, market="DE",
                            rate_source="letter", gwp_set="AR6",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    _rate(db, "DE", 2025, 0.45)
    run = compute_co2e(db, org.id)
    st = _stmts(db, run)[0]
    assert st.status == "reference_rate"                   # reference load dominates...
    assert st.org_rate_kg_co2e_per_kwh == pytest.approx(0.10)   # ...but the org rate is kept
    assert any("BELOW the published residual mix" in b
               for b in scope2_residual_mix_completeness(db, run)["blockers"])


def test_an_unconvertible_electricity_line_is_not_reported_as_fully_contractual(db):
    """Regression (review 2): the hoisted bucket produced an all-zero row that fell into
    the fully_contractual branch — telling an assurer the market was fully covered by
    contractual instruments when its entire load was priced at the location factor."""
    org = _org(db)
    f = EmissionFactor(source="T", version="1", geography="DE", year=2025,
                       category="electricity", subcategory="", unit="kg",
                       gwp_set="AR6", value=0.4)
    db.add(f); db.commit(); db.refresh(f)
    db.add(ActivityRecord(organisation_id=org.id, date="2025-06-01", category="electricity",
                          subcategory="", description="", quantity=1000, unit="kg",
                          geo="DE", factor_id=f.id))
    db.commit()
    run = compute_co2e(db, org.id)
    st = _stmts(db, run)[0]
    assert st.status == "unpriceable" and st.unpriceable_lines == 1
    assert any("could not be converted to kWh" in w
               for w in scope2_residual_mix_completeness(db, run)["warnings"])


def test_an_edit_to_a_row_the_run_never_consulted_does_not_block(db):
    """Regression (review 2): reference_rate_id is frozen even for a fully-contractual
    bucket, so an in-place correction of a never-applied reference row retroactively
    blocked a filed run over admin-owned data the org cannot fix."""
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=1000.0, market="DE",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    r = _rate(db, "DE", 2025, 0.55)
    run = compute_co2e(db, org.id)
    st = _stmts(db, run)[0]
    assert st.status == "fully_contractual" and st.kwh_priced_at_grid == 0.0
    r.kg_co2e_per_kwh = 0.56                              # edited, but never consulted
    db.commit()
    assert scope2_residual_mix_completeness(db, run)["blockers"] == []


def test_a_rate_under_another_gwp_vintage_is_named_not_reported_as_absent(db):
    """Regression (review 2): the vintage mismatch was computed then discarded, so the
    preparer was told 'no residual mix is on file' and sent to load one that already is."""
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4)
    _rate(db, "DE", 2025, 0.55, gwp_set="AR5")            # run is AR6
    run = compute_co2e(db, org.id)
    st = _stmts(db, run)[0]
    assert st.gwp_vintage_mismatch is True
    assert run.total_co2e_market == pytest.approx(400.0)   # vintages never silently mixed
    assert any("only under a different GWP vintage" in w
               for w in scope2_residual_mix_completeness(db, run)["warnings"])


def test_an_org_residual_instrument_is_not_reported_as_contractual_coverage(db):
    """Regression (review 2): an org-supplied residual_mix instrument is that org's own
    residual RATE, not a contractual attribute claim. Counting it as contractual made
    summary report 100% contractual coverage for an org holding ZERO contractual
    instruments — contradicting the run's own frozen statement."""
    from app.reports.summary import summary
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="residual_mix",
                            kg_co2e_per_kwh=0.50, coverage_kwh=1000.0, market="DE",
                            rate_source="supplier letter", gwp_set="AR6",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    run = compute_co2e(db, org.id)
    s2 = summary(db, organisation_id=org.id, run_id=run.id)["scope2"]
    st = _stmts(db, run)[0]
    assert s2["kwh_contractual"] == pytest.approx(0.0)        # no contractual claim
    assert s2["kwh_residual_mix"] == pytest.approx(1000.0)
    assert s2["kwh_electricity_accounted"] == pytest.approx(1000.0)
    # ...and the two disclosed artifacts now AGREE.
    assert s2["kwh_contractual"] == pytest.approx(st.kwh_contractual)
    assert s2["kwh_residual_mix"] == pytest.approx(st.kwh_priced_at_residual)


def test_a_rec_plus_an_org_residual_instrument_splits_correctly(db):
    from app.reports.summary import summary
    org = _org(db)
    _elec(db, org.id, kwh=1000.0, rate=0.4)
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            kg_co2e_per_kwh=0.0, coverage_kwh=600.0, market="DE",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="residual_mix",
                            kg_co2e_per_kwh=0.50, coverage_kwh=None, market="DE",
                            rate_source="supplier letter", gwp_set="AR6",
                            start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    run = compute_co2e(db, org.id)
    s2 = summary(db, organisation_id=org.id, run_id=run.id)["scope2"]
    st = _stmts(db, run)[0]
    assert s2["kwh_contractual"] == pytest.approx(600.0)      # the REC only
    assert s2["kwh_residual_mix"] == pytest.approx(400.0)
    assert s2["kwh_contractual"] == pytest.approx(st.kwh_contractual)
    assert s2["kwh_residual_mix"] == pytest.approx(st.kwh_priced_at_residual)
