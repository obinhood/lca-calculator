"""Scope 3 Category 15 = PCAF financed emissions, frozen into the run.

The invariants: total_co2e and the pedigree data-quality score are NEVER touched;
the DISCLOSED total (ESRS/ISSB/CDP) adds financed emissions; a filed run reproduces
its Cat 15 even after the live ledger changes; and the pcaf as_of `<=` fix means an
as_of matching no rows is a blocker, not a silent zero.
"""
import pytest

from app.models import (
    Organisation, ActivityRecord, EmissionFactor, FinancedPosition, RunFinancedLine,
    Scope3CategoryDeclaration,
)
from app.services.calc import compute_co2e
from app.services.pcaf import portfolio_financed
from app.services.ghgp import scope3_completeness
from app.reports.summary import summary
from app.reports.esrs_e1 import esrs_e1_report
from app.reports.scope3 import scope3_by_ghgp_category
from tests.scope3_util import ready_run, make_period


def _org(db, name="Bank"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category="electricity", value=1.0):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024, category=category,
                       subcategory="", unit="kWh", gwp_set="AR6", value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _act(db, org_id, factor_id, category="electricity", qty=1000.0):
    a = ActivityRecord(organisation_id=org_id, date="2025-03-01", category=category,
                       subcategory="", description="", quantity=qty, unit="kWh",
                       geo="GB", factor_id=factor_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _pos(db, org_id, s1=1.0, dq=2, as_of="2025-01-01", outstanding=100.0, denom=100.0):
    p = FinancedPosition(organisation_id=org_id, investee_name="Acme", asset_class="listed_equity",
                         currency="GBP", outstanding_amount=outstanding, attribution_denominator=denom,
                         investee_scope1_tco2e=s1, investee_scope2_tco2e=0.0,
                         data_quality_score=dq, as_of_date=as_of)
    db.add(p); db.commit(); db.refresh(p)
    return p


# --- pcaf as_of fix -----------------------------------------------------------

def test_as_of_uses_le_not_exact_match(db):
    org = _org(db)
    _pos(db, org.id, as_of="2025-01-01")
    # a cutoff LATER than the position date must still include it (was == before)
    pf = portfolio_financed(db, org.id, as_of="2025-12-31")
    assert pf["positions"] == 1
    assert pf["financed_emissions_tco2e"]["total"] == pytest.approx(1.0)
    assert pf["as_of_filtered_empty"] is False


def test_as_of_before_all_positions_is_flagged_not_silent(db):
    org = _org(db)
    _pos(db, org.id, as_of="2025-06-01")
    pf = portfolio_financed(db, org.id, as_of="2024-01-01")   # before the position exists
    assert pf["positions"] == 0
    assert pf["positions_available"] == 1
    assert pf["as_of_filtered_empty"] is True                 # surfaced, not a silent zero


# --- Freezing + invariants ----------------------------------------------------

def test_financed_lines_frozen_in_kg_no_unit_error(db):
    org = _org(db)
    _act(db, org.id, _factor(db).id)          # Scope 2 electricity, 1000 kg
    _pos(db, org.id, s1=1.0)                   # 1.0 tCO2e financed
    run = compute_co2e(db, org.id)
    rfl = db.query(RunFinancedLine).filter_by(run_id=run.id).all()
    assert len(rfl) == 1 and rfl[0].co2e == pytest.approx(1000.0)   # 1.0 tCO2e x1000
    inv = scope3_by_ghgp_category(db, run)
    assert inv["categories"]["15"]["financed_emissions"]["tco2e"] == pytest.approx(1.0)
    # gross Scope 3 = activity S3 (0) + unassigned (0) + financed (1000 kg)
    assert inv["totals"]["scope3_gross_kg"] == pytest.approx(1000.0)


def test_run_total_and_dq_unchanged_by_financed(db):
    org1 = _org(db, "NoBank")
    _act(db, org1.id, _factor(db).id)
    run_without = compute_co2e(db, org1.id)

    org2 = _org(db, "WithBank")
    _act(db, org2.id, _factor(db).id)
    _pos(db, org2.id, s1=99.0)
    run_with = compute_co2e(db, org2.id)

    # total_co2e is activity-derived and identical; data-quality score byte-identical.
    assert run_with.total_co2e == pytest.approx(run_without.total_co2e)
    assert run_with.data_quality_score == run_without.data_quality_score
    assert run_with.total_activities == run_without.total_activities
    assert run_with.financed_co2e == pytest.approx(99000.0)   # frozen separately
    assert run_without.financed_co2e is None                  # no positions -> not evaluated


def test_disclosed_total_includes_cat15(db):
    org = _org(db)
    _act(db, org.id, _factor(db, "gas", value=1.0).id, "gas")   # Scope 1, 1000 kg = 1 tCO2e
    _pos(db, org.id, s1=9.0)                                    # 9 tCO2e financed
    run, _p = ready_run(db, org.id)
    r = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=1.0)
    e = r["e1_6_gross_ghg_emissions_tco2e"]
    assert e["total_location_based_excl_financed"] == pytest.approx(1.0)
    assert e["total_location_based"] == pytest.approx(10.0)     # 1 + 9 financed
    assert e["financed_emissions"]["included_in_total"] is True
    # intensity is off the DISCLOSED total (¶52 reconciliation)
    assert e["ghg_intensity"]["tco2e_total_location_per_million_revenue"] == pytest.approx(10.0)
    # summary reconciliation
    s = summary(db, organisation_id=org.id, run_id=run.id)
    assert s["total_co2e"] == pytest.approx(1000.0)            # unchanged
    assert s["total_co2e_incl_financed_kg"] == pytest.approx(10000.0)


# --- Frozen against a later ledger edit ---------------------------------------

def test_financed_figure_frozen_against_later_ledger_edit(db):
    org = _org(db)
    _act(db, org.id, _factor(db).id)
    p = _pos(db, org.id, s1=1.0)
    run, _pd = ready_run(db, org.id)
    assert scope3_completeness(db, run)["blockers"] == []
    before = run.financed_co2e
    p.outstanding_amount = 200.0    # double the exposure AFTER the run
    db.commit()
    assert run.financed_co2e == before                        # frozen figure unchanged
    assert any("changed since it was filed" in b
               for b in scope3_completeness(db, run)["blockers"])


def test_positions_without_evaluation_block(db):
    org = _org(db)
    _act(db, org.id, _factor(db).id)
    run, p = ready_run(db, org.id)
    assert scope3_completeness(db, run)["blockers"] == []
    _pos(db, org.id, s1=1.0)         # add a position AFTER the run (not frozen)
    assert any("did not evaluate financed emissions" in b or "ledger changed" in b
               for b in scope3_completeness(db, run)["blockers"])


def test_cat15_not_applicable_with_positions_blocks(db):
    org = _org(db)
    p = make_period(db, org.id)
    _act(db, org.id, _factor(db).id)
    _pos(db, org.id, s1=1.0)
    db.add(Scope3CategoryDeclaration(organisation_id=org.id, reporting_period_id=p.id,
           category=15, status="not_applicable",
           justification="We consider our investments out of scope for this disclosure.",
           screened_at="2025-06-30", updated_at="2025-06-30"))
    db.commit()
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert any("category 15" in b and "NOT APPLICABLE" in b
               for b in scope3_completeness(db, run)["blockers"])


def test_double_count_activity_and_financed_blocks(db):
    org = _org(db)
    # an activity explicitly categorised Cat 15 AND a financed position
    a = _act(db, org.id, _factor(db).id)
    a.scope = "3"; a.ghgp_category = 15
    db.commit()
    _pos(db, org.id, s1=1.0)
    run, _p = ready_run(db, org.id)
    assert any("BOTH activity-derived lines and PCAF financed lines" in b
               for b in scope3_completeness(db, run)["blockers"])


def test_as_of_excludes_all_freezes_none_not_zero(db):
    """Adversarial-review finding #1: the freeze must not turn a filtered-empty
    portfolio into an immutable financed_co2e = 0."""
    org = _org(db)
    _act(db, org.id, _factor(db).id)
    # a real position, but dated AFTER the period the run scopes to
    _pos(db, org.id, s1=500.0, as_of="2026-06-01")
    p = make_period(db, org.id, start="2025-01-01", end="2025-12-31")
    run = compute_co2e(db, org.id, reporting_period_id=p.id)   # as_of defaults to 2025-12-31
    assert run.financed_co2e is None          # NOT 0.0 — the false zero is refused
    assert db.query(RunFinancedLine).filter_by(run_id=run.id).count() == 0
    assert any("excluded every financed position" in b
               for b in scope3_completeness(db, run)["blockers"])


def test_position_after_as_of_does_not_false_flag_stale(db):
    """Adversarial-review finding #3: fingerprint only the as_of-included set."""
    org = _org(db)
    _act(db, org.id, _factor(db).id)
    _pos(db, org.id, s1=1.0, as_of="2025-03-01")     # inside the period
    p = make_period(db, org.id, start="2025-01-01", end="2025-12-31")
    discover = compute_co2e(db, org.id, reporting_period_id=p.id)
    from tests.scope3_util import screen_complete
    screen_complete(db, org.id, p.id, discover)
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert scope3_completeness(db, run)["blockers"] == []
    # add a NEW position dated AFTER the run's as_of — it is NOT in the filed figure,
    # so it must not mark the run stale.
    _pos(db, org.id, s1=999.0, as_of="2026-05-01")
    assert scope3_completeness(db, run)["blockers"] == []
    # but editing an INCLUDED position DOES invalidate the figure
    inc = db.query(FinancedPosition).filter_by(organisation_id=org.id, as_of_date="2025-03-01").first()
    inc.outstanding_amount = 500.0
    db.commit()
    assert any("changed since it was filed" in b
               for b in scope3_completeness(db, run)["blockers"])


def test_double_count_gross_is_not_summed(db):
    """Adversarial-review finding #2: scope3_gross_kg must not sum both accountings."""
    org = _org(db)
    a = _act(db, org.id, _factor(db).id)
    a.scope = "3"; a.ghgp_category = 15       # activity-derived Cat 15
    db.commit()
    _pos(db, org.id, s1=1.0)                   # AND a financed position
    run = compute_co2e(db, org.id)
    inv = scope3_by_ghgp_category(db, run)
    assert inv["totals"]["cat15_double_count_blocked"] is True
    assert inv["totals"]["scope3_gross_kg"] is None   # not a double-counted number


def test_s2_blocks_cat15_without_gross_exposure(db):
    """IFRS S2 B58-B63: financed emissions without their exposure denominator are
    not interpretable — the disclosure must block, not silently omit it."""
    from app.reports.issb_s2 import issb_s2_report
    org = _org(db)
    _act(db, org.id, _factor(db).id)
    _pos(db, org.id, s1=1.0)
    run, _p = ready_run(db, org.id)          # screens all 15, but sets no gross exposure
    r = issb_s2_report(db, org.id, run_id=run.id)
    assert r["disclosure_ready"] is False
    assert any("gross exposure" in b for b in r["blockers"])


def test_s2_gross_exposure_unblocks_and_reports_pct_covered(db):
    from app.reports.issb_s2 import issb_s2_report
    org = _org(db)
    _act(db, org.id, _factor(db).id)
    _pos(db, org.id, s1=1.0, outstanding=100.0)      # 100 of exposure, has investee data
    run, p = ready_run(db, org.id)
    # declare the gross exposure on the Cat 15 screen, then recompute to freeze it
    d = db.query(Scope3CategoryDeclaration).filter_by(
        organisation_id=org.id, reporting_period_id=p.id, category=15).first()
    d.gross_exposure_total = 1000.0
    d.gross_exposure_currency = "GBP"
    db.commit()
    run2 = compute_co2e(db, org.id, reporting_period_id=p.id)
    r = issb_s2_report(db, org.id, run_id=run2.id)
    assert r["disclosure_ready"] is True
    fin = r["ghg_emissions_tco2e"]["scope3_cat15_financed"]
    assert fin["gross_exposure_total"] == pytest.approx(1000.0)
    assert fin["gross_exposure_currency"] == "GBP"
    assert fin["exposure_covered"] == pytest.approx(100.0)
    assert fin["pct_gross_exposure_covered"] == pytest.approx(10.0)   # 100 / 1000


def test_gross_exposure_is_frozen_onto_the_run(db):
    """A later edit to the live screen must not change a filed run's disclosure."""
    org = _org(db)
    _act(db, org.id, _factor(db).id)
    _pos(db, org.id, s1=1.0, outstanding=100.0)
    run, p = ready_run(db, org.id)
    d = db.query(Scope3CategoryDeclaration).filter_by(
        organisation_id=org.id, reporting_period_id=p.id, category=15).first()
    d.gross_exposure_total = 1000.0
    db.commit()
    filed = compute_co2e(db, org.id, reporting_period_id=p.id)
    before = scope3_by_ghgp_category(db, filed)["categories"]["15"][
        "financed_emissions"]["pct_gross_exposure_covered"]
    d.gross_exposure_total = 50_000.0          # restate the live screen after filing
    db.commit()
    after = scope3_by_ghgp_category(db, filed)["categories"]["15"][
        "financed_emissions"]["pct_gross_exposure_covered"]
    assert after == before == pytest.approx(10.0)   # the FILED run is unchanged


def test_non_fi_run_is_unaffected(db):
    org = _org(db, "PlainCo")
    _act(db, org.id, _factor(db).id)
    run = compute_co2e(db, org.id)
    assert run.financed_co2e is None
    assert db.query(RunFinancedLine).filter_by(run_id=run.id).count() == 0
    s = summary(db, organisation_id=org.id, run_id=run.id)
    assert s["total_co2e_incl_financed_kg"] is None            # not evaluated, not zero
