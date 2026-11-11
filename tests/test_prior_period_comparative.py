"""Prior-period comparatives, and the period scoping that makes them mean anything.

Two defects are pinned here.

**The cumulative run.** The SPA never passed `reporting_period_id`, so every run it
created spanned the whole uploaded activity history. Measured: a second year's upload
produced a headline of exactly 2.0x the true year-two figure, `reporting_period_id` null,
nothing in the payload saying so. `test_two_years_unscoped_is_exactly_double` is that
measurement, kept as a test so the arithmetic that made it 2.0x stays pinned; the gate
tests beside it are why a disclosure can no longer be rendered from such a run.

**The absent comparative.** All eight primary annual disclosures rendered exactly one
run, so a second-year filer could not produce a compliant ESRS E1 or SECR statement at
all. The comparative now exists — and the tests that matter most here are the ones that
check it REFUSES: a comparative is the exact shape in which this codebase has previously
published a divestment as a substantiated reduction with an empty blocker list.
"""
import pytest

from app.models import (ActivityRecord, CalculationRun, EmissionFactor, Organisation,
                        ReportingEntity)
from app.reports.esrs_e1 import esrs_e1_report
from app.reports.secr import secr_report
from app.services.calc import compute_co2e
from tests.scope3_util import make_period, screen_complete


def _org(db, name="DemoOrg"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category, unit, value, gwp_set="AR6"):
    f = EmissionFactor(source="DEFRA_DEMO", version="2024.1", geography="GB", year=2024,
                       category=category, subcategory="", unit=unit,
                       gwp_set=gwp_set, value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor_id, date, quantity, category="electricity", unit="kWh"):
    a = ActivityRecord(organisation_id=org_id, date=date, category=category,
                       subcategory="", description="", quantity=quantity, unit=unit,
                       geo="GB", factor_id=factor_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


@pytest.fixture
def two_years(db):
    """One org, 12 months of electricity in 2024 and 12 in 2025, both years screened.

    1000 kWh/month at 0.5 kg/kWh = 6,000 kg per year, so a correctly scoped year is
    6,000 kg and the whole history is 12,000 kg. The round numbers are the point: the
    2.0x in the UI defect is arithmetic, not an estimate.
    """
    org = _org(db)
    f = _factor(db, "electricity", "kWh", 0.5)
    for year in (2024, 2025):
        for month in range(1, 13):
            _activity(db, org.id, f.id, f"{year}-{month:02d}-15", 1000)
    p24 = make_period(db, org.id, label="FY24", start="2024-01-01", end="2024-12-31")
    p25 = make_period(db, org.id, label="FY25", start="2025-01-01", end="2025-12-31")

    def _ready(period):
        discover = compute_co2e(db, org.id, reporting_period_id=period.id)
        screen_complete(db, org.id, period.id, discover)
        run = compute_co2e(db, org.id, reporting_period_id=period.id)
        # Asserted in the FIXTURE, not left to be discovered in an unrelated assertion
        # three tests away. A run with ANY excluded line (unmapped / unit_errors /
        # data_errors / gwp_mismatch) is a partial figure, and the comparative refuses
        # a partial comparative at `prior_run_incomplete` — BEFORE it reaches whichever
        # gate the test was written to exercise. A fixture that quietly starts building
        # partial runs therefore makes tests pass or fail for reasons unconnected to
        # what they assert, which is how this file first broke.
        assert (run.unmapped, run.unit_errors, run.data_errors, run.gwp_mismatch) \
            == (0, 0, 0, 0), f"fixture built a PARTIAL run: {period.label}"
        return run

    return org, p24, p25, _ready(p24), _ready(p25)


def _ar5_prior_run(db, org, p24):
    """FY24 recomputed on the AR5 vintage — a REAL AR5 run, not an empty one.

    The organisation re-based its GWP vintage between the two years, which is the
    scenario `gwp_comparable` exists for. Getting there needs the 2024 activities
    re-pinned to an AR5 factor: an AR5 run over AR6-pinned factors excludes every line
    as `gwp_mismatch` and totals 0.0, which is not an AR5 inventory — it is a run that
    computed nothing. (It also tripped `prior_run_incomplete` first, so the GWP gate
    these tests are about was never reached.)

    Re-pinning does not disturb the FY25 run: `activities_fingerprint` is computed over
    the activities IN SCOPE for each run, and no 2024 activity is in FY25's.
    """
    ar5 = _factor(db, "electricity", "kWh", 0.6, gwp_set="AR5")
    for a in db.query(ActivityRecord).filter(
            ActivityRecord.organisation_id == org.id,
            ActivityRecord.date < "2025-01-01").all():
        a.factor_id = ar5.id
    db.commit()
    run = compute_co2e(db, org.id, gwp_set="AR5", reporting_period_id=p24.id)
    assert run.gwp_mismatch == 0 and run.status == "complete"
    assert run.total_co2e == pytest.approx(7_200.0)      # 12 x 1000 kWh x 0.6
    return run


# --- Defect 1: the cumulative run -------------------------------------------------

def test_two_years_unscoped_is_exactly_double(db, two_years):
    """The measured defect: the figure the SPA showed for 'year two'.

    An unscoped run is still COMPUTED — it is a legitimate whole-history view and every
    run filed before periods reached the UI is one. What changed is that it can no longer
    be rendered as a period's disclosure (the gate tests below).
    """
    org, _p24, p25, _r24, r25 = two_years
    unscoped = compute_co2e(db, org.id)                 # exactly what api.run() sent
    assert unscoped.reporting_period_id is None
    assert unscoped.total_co2e == pytest.approx(12_000.0)
    assert r25.total_co2e == pytest.approx(6_000.0)
    # The headline a second-year customer saw, over the figure that was true.
    assert unscoped.total_co2e / r25.total_co2e == pytest.approx(2.0)


def test_scoped_runs_do_not_accumulate_across_years(db, two_years):
    """The fix, from the engine's side: each year's run carries only that year."""
    _org, _p24, _p25, r24, r25 = two_years
    assert r24.total_co2e == pytest.approx(6_000.0)
    assert r25.total_co2e == pytest.approx(6_000.0)
    assert r24.total_activities == 12 and r25.total_activities == 12


@pytest.mark.parametrize("render,ready_key", [
    (lambda db, org, run, **kw: esrs_e1_report(db, org.id, run_id=run.id,
                                               net_revenue_millions=10.0,
                                               revenue_period_days=365, **kw),
     "CSRD ESRS E1"),
    (lambda db, org, run, **kw: secr_report(db, org.id, run_id=run.id,
                                            intensity_denominator=2.0,
                                            intensity_denominator_unit="£M revenue",
                                            intensity_denominator_period_days=365, **kw),
     "UK SECR"),
])
def test_unscoped_run_cannot_carry_a_period_bound_disclosure(db, two_years, render,
                                                             ready_key):
    """The gate, and the SENTENCE it gives.

    An unscoped run already failed both renderers — but through the Scope 3 completeness
    gate, which says fifteen categories are undeclared. A preparer reading that does not
    learn that their total covers two years. The blocker has to name the period, so the
    assertion is on the wording, not just on `disclosure_ready`.
    """
    org, _p24, _p25, _r24, _r25 = two_years
    r = render(db, org, compute_co2e(db, org.id))
    assert r["disclosure_ready"] is False
    period_blockers = [b for b in r["blockers"]
                       if "NOT scoped to a reporting period" in b]
    assert period_blockers, (
        f"no blocker named the missing reporting period; got {r['blockers']}")
    assert ready_key in period_blockers[0]
    assert "whole of the uploaded activity history" in period_blockers[0]


def test_period_scope_gate_passes_a_scoped_run(db, two_years):
    """Negative control on the gate itself.

    A gate that blocks everything is as useless as one that blocks nothing, and a test
    that only ever asserts refusal cannot tell the two apart.
    """
    org, _p24, _p25, _r24, r25 = two_years
    r = secr_report(db, org.id, run_id=r25.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365)
    # Specifically the SCOPE gate. The comparative gate legitimately fires here (FY24 is
    # on file and was not supplied), and a looser filter would let this test pass for the
    # wrong reason — then keep passing if the scope gate started blocking everything.
    assert not [b for b in r["blockers"]
                if "NOT scoped to a reporting period" in b], r["blockers"]


# --- Defect 2: the comparative ----------------------------------------------------

def test_comparative_computes_on_a_comparable_pair(db, two_years):
    """FY25 against FY24: both scoped, same GWP vintage, boundary and compilation."""
    org, _p24, _p25, r24, r25 = two_years
    r = esrs_e1_report(db, org.id, run_id=r25.id, net_revenue_millions=10.0,
                       revenue_period_days=365, comparative_run_id=r24.id)
    c = r["e1_6_comparative"]
    assert c["status"] == "computed"
    assert c["reason_code"] is None
    assert c["blockers"] == []
    assert c["comparative_run"]["run_id"] == r24.id
    assert c["comparative_run"]["reporting_period"]["label"] == "FY24"
    assert c["reporting_run"]["reporting_period"]["label"] == "FY25"
    # Equal years: the change is zero and the report says so as a COMPUTED zero, which
    # is a different fact from the cannot_determine a refused pair gets.
    f = c["figures"]["total_location_based_tco2e"]
    assert f["current"] == pytest.approx(6.0)
    assert f["prior"] == pytest.approx(6.0)
    assert f["change"] == pytest.approx(0.0)
    assert f["change_pct"] == pytest.approx(0.0)
    assert f["status"] == "computed"
    # Both windows are published beside the delta, not left to be trusted.
    assert c["period_comparability"]["comparative_days"] == 366      # 2024 is a leap year
    assert c["period_comparability"]["reporting_days"] == 365


def test_comparative_reports_a_real_fall(db, two_years):
    """A change the arithmetic must get right and signed correctly."""
    org, p24, p25, _r24, _r25 = two_years
    # Halve FY25 by re-dating six months of it OUT of the period, then recompute both
    # years. Re-dated rather than deleted: an activity carrying frozen line items on an
    # immutable run cannot be deleted, and should not be — that is the reproduction
    # contract holding.
    for a in db.query(ActivityRecord).filter(
            ActivityRecord.organisation_id == org.id,
            ActivityRecord.date >= "2025-07-01").all():
        a.date = a.date.replace("2025-", "2026-")
    db.commit()
    r24 = compute_co2e(db, org.id, reporting_period_id=p24.id)
    r25 = compute_co2e(db, org.id, reporting_period_id=p25.id)
    r = secr_report(db, org.id, run_id=r25.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365,
                    comparative_run_id=r24.id)
    c = r["prior_year_comparative"]
    assert c["status"] == "computed"
    f = c["figures"]["scope1_and_2_location_tco2e"]
    assert f["prior"] == pytest.approx(6.0)
    assert f["current"] == pytest.approx(3.0)
    # current - prior, so a fall is NEGATIVE. Signed the other way a reduction would
    # read as an increase in every consumer of this payload.
    assert f["change"] == pytest.approx(-3.0)
    assert f["change_pct"] == pytest.approx(-50.0)


def test_non_comparable_pair_refuses_with_its_reasons_rather_than_subtracting(db,
                                                                              two_years):
    """THE test. A GWP vintage change moves the totals on its own.

    The requirement is not merely that `disclosure_ready` goes false: it is that no
    difference is published. A delta beside a blocker still gets quoted.
    """
    org, p24, _p25, _r24, r25 = two_years
    r24_ar5 = _ar5_prior_run(db, org, p24)
    assert r24_ar5.gwp_set == "AR5" and r25.gwp_set == "AR6"

    r = esrs_e1_report(db, org.id, run_id=r25.id, net_revenue_millions=10.0,
                       revenue_period_days=365, comparative_run_id=r24_ar5.id)
    c = r["e1_6_comparative"]
    assert c["status"] == "cannot_determine"
    assert c["reason_code"] == "not_comparable"
    # No figures at all — not zeros, not a delta with a caveat.
    assert c["figures"] is None
    # Every reason named, in the renderer's own voice, and surfaced as a blocker.
    assert any("AR5" in b and "AR6" in b for b in c["blockers"]), c["blockers"]
    assert r["disclosure_ready"] is False
    assert any("ESRS E1-6 comparative" in b and "AR5" in b for b in r["blockers"])
    # The two windows are still published: a reader of a REFUSED comparative needs them
    # as much as a reader of a computed one.
    assert c["period_comparability"]["comparative_period"]["label"] == "FY24"


def test_pedigree_compilation_change_blocks_the_comparative(db, two_years):
    """The gate added most recently, and the one a hand-assembled set would omit.

    A run frozen before the SDg95 correction keeps its own wider uncertainty interval —
    the reproduction contract working — but subtracting it from a corrected run reports a
    halving of uncertainty that no change in data quality produced. An absent marker IS
    the superseded compilation, so it is set to None here rather than to a made-up name.
    """
    org, _p24, _p25, r24, r25 = two_years
    db.get(CalculationRun, r24.id).pedigree_method = None
    db.commit()
    r = secr_report(db, org.id, run_id=r25.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365,
                    comparative_run_id=r24.id)
    c = r["prior_year_comparative"]
    assert c["status"] == "cannot_determine"
    assert c["reason_code"] == "not_comparable"
    assert c["figures"] is None
    assert any("pedigree" in b for b in c["blockers"]), c["blockers"]


def test_mismatched_period_lengths_block_the_comparative(db, two_years):
    """A 12-month prior period minus a 3-month current one is a 75% 'fall'.

    Both totals are correct for the span they cover, which is why nothing downstream can
    catch this and why the gate lives in one place.
    """
    org, p24, _p25, r24, _r25 = two_years
    q1 = make_period(db, org.id, label="FY25Q1", start="2025-01-01", end="2025-03-31")
    r_q1 = compute_co2e(db, org.id, reporting_period_id=q1.id)
    r = secr_report(db, org.id, run_id=r_q1.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=90,
                    comparative_run_id=r24.id)
    c = r["prior_year_comparative"]
    assert c["status"] == "cannot_determine"
    assert c["figures"] is None
    assert any("366 days" in b and "90 days" in b for b in c["blockers"]), c["blockers"]


def test_divestment_blocks_the_comparative(db, two_years):
    """The defect class this module exists for: an entity leaving the group.

    A divestment makes the delta look like abatement while the consolidation APPROACH is
    unchanged, so a boundary check written as a bare comparison of approaches waves it
    through. Asserted here on the comparative because that is the renderer shape in which
    it was previously published as a substantiated reduction.
    """
    org, p24, p25, r24, _r25 = two_years
    db.add(ReportingEntity(organisation_id=org.id, name="Disposed Sub",
                           accounting_category="subsidiary", equity_share_pct=100.0,
                           financial_control=True, operational_control=True,
                           in_consolidated_accounting_group=True,
                           control_rationale="wholly owned"))
    db.commit()
    r25_after = compute_co2e(db, org.id, reporting_period_id=p25.id)
    r = secr_report(db, org.id, run_id=r25_after.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365,
                    comparative_run_id=r24.id)
    c = r["prior_year_comparative"]
    assert c["status"] == "cannot_determine", c
    assert c["figures"] is None
    assert c["blockers"]


# --- "no prior run" and "not comparable" are different facts ----------------------

def test_first_reporting_period_is_not_a_failure(db, two_years):
    """A genuine first-year filing: no prior period exists, and that is legitimate.

    `cannot_determine` with a reason, never a zero delta — and NOT a blocker, because
    there is nothing the preparer could supply.
    """
    org = _org(db, name="FirstYearCo")
    f = _factor(db, "electricity", "kWh", 0.5)
    _activity(db, org.id, f.id, "2025-03-01", 1000)
    p = make_period(db, org.id, label="FY25", start="2025-01-01", end="2025-12-31")
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    r = secr_report(db, org.id, run_id=run.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365)
    c = r["prior_year_comparative"]
    assert c["status"] == "cannot_determine"
    assert c["reason_code"] == "no_prior_period_on_file"
    assert c["prior_periods_on_file"] == []
    assert c["figures"] is None
    # Nothing the preparer could have supplied, so this contributes NO blocker — and the
    # block's own list is the same list the renderer got, never a different view of it.
    assert c["blockers"] == []
    assert not [b for b in r["blockers"] if "comparative" in b], r["blockers"]


def test_prior_period_on_file_but_not_supplied_is_a_blocker(db, two_years):
    """The second-year filer this whole stream is about.

    A prior period IS on file; omitting the comparative publishes a single-period
    statement where the framework mandates two. The reason must read differently from
    the first-year case above, and must name the runs that would serve.
    """
    org, _p24, _p25, r24, r25 = two_years
    r = esrs_e1_report(db, org.id, run_id=r25.id, net_revenue_millions=10.0,
                       revenue_period_days=365)
    c = r["e1_6_comparative"]
    assert c["status"] == "cannot_determine"
    assert c["reason_code"] == "no_prior_run_requested"
    assert c["reason_code"] != "no_prior_period_on_file"        # the two must differ
    # Every completed FY24 run is a legitimate candidate (the fixture screens then
    # recomputes, so FY24 has two), and the newest is listed first.
    candidates = [x["run_id"] for x in c["prior_periods_on_file"]]
    assert r24.id in candidates and candidates == sorted(candidates, reverse=True)
    assert all(x["reporting_period"]["label"] == "FY24"
               for x in c["prior_periods_on_file"])
    assert f"#{r24.id}" in c["reason"]
    assert r["disclosure_ready"] is False
    assert any("prior reporting period is on file" in b for b in r["blockers"])


def test_a_comparative_that_computed_nothing_is_refused(db, two_years):
    """A run that excluded EVERY line totals 0.0 — and 0.0 subtracts fine.

    The real case: an AR5 factor file computed on the AR6 default drops every line on
    vintage. Every comparability gate passes (the periods match, the boundary matches,
    the vintages are declared), and the delta publishes the whole of the current year as
    an increase from nothing.

    Gated on the run's own exclusion counters rather than on its status string, because
    the status has historically called such a run "complete": the counters are the fact,
    the status is a summary of it that has been wrong.
    """
    org, p24, _p25, _r24, r25 = two_years
    # AR5 run over AR6-pinned factors: all 12 lines excluded on vintage.
    empty = compute_co2e(db, org.id, gwp_set="AR5", reporting_period_id=p24.id)
    assert empty.gwp_mismatch == 12 and empty.total_co2e == pytest.approx(0.0)

    r = secr_report(db, org.id, run_id=r25.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365,
                    comparative_run_id=empty.id)
    c = r["prior_year_comparative"]
    assert c["reason_code"] == "prior_run_incomplete"
    assert c["figures"] is None                 # NOT a 100% rise from zero
    assert "gwp_mismatch=12" in c["reason"], c["reason"]
    assert r["disclosure_ready"] is False
    # And it is not offered as a remedy either.
    assert empty.id not in [x["run_id"] for x in c["prior_periods_on_file"]]


def test_unknown_comparative_run_is_refused_not_ignored(db, two_years):
    org, _p24, _p25, _r24, r25 = two_years
    r = secr_report(db, org.id, run_id=r25.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365,
                    comparative_run_id=99_999)
    c = r["prior_year_comparative"]
    assert c["reason_code"] == "prior_run_not_found"
    assert c["figures"] is None
    assert r["disclosure_ready"] is False


def test_another_orgs_run_is_not_a_comparative(db, two_years):
    """Tenancy: a comparative is a join across runs, so it is a cross-tenant read."""
    org, _p24, _p25, _r24, r25 = two_years
    other = _org(db, name="OtherCo")
    f = _factor(db, "gas", "kWh", 0.2)
    _activity(db, other.id, f.id, "2024-05-01", 500, category="gas")
    p = make_period(db, other.id, label="FY24", start="2024-01-01", end="2024-12-31")
    foreign = compute_co2e(db, other.id, reporting_period_id=p.id)
    r = secr_report(db, org.id, run_id=r25.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365,
                    comparative_run_id=foreign.id)
    assert r["prior_year_comparative"]["reason_code"] == "prior_run_not_found"
    assert r["prior_year_comparative"]["figures"] is None


def test_a_later_period_is_refused_as_the_comparative(db, two_years):
    """The one a symmetric gate set cannot catch.

    `cross_run_gates` answers "can these two totals be subtracted?", which is the same
    question with the arguments swapped — so FY2025 offered as the "prior" period for an
    FY2024 report passes every one of its five gates. The comparative is directional:
    it publishes current minus prior, so the wrong order reports a rise as a fall with
    both totals correct.
    """
    org, p24, _p25, r24, r25 = two_years
    # Make the years differ, so a sign error would be visible rather than masked by the
    # equal totals the fixture starts with.
    f = _factor(db, "gas", "kWh", 0.2)
    _activity(db, org.id, f.id, "2025-06-01", 5000, category="gas")
    r25 = compute_co2e(db, org.id, reporting_period_id=_p25.id)
    assert r25.total_co2e > r24.total_co2e          # FY25 is the HIGHER year

    # FY2024 as the reporting run, FY2025 offered as its "prior" period.
    r = secr_report(db, org.id, run_id=r24.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365,
                    comparative_run_id=r25.id)
    c = r["prior_year_comparative"]
    assert c["status"] == "cannot_determine"
    assert c["reason_code"] == "not_a_prior_period"
    assert c["figures"] is None                     # no backwards delta published
    assert any("not a PRIOR period" in b for b in c["blockers"]), c["blockers"]
    # And it is not offered as a remedy either — naming a candidate the builder would
    # then refuse sends the preparer in a circle.
    assert r25.id not in [x["run_id"] for x in c["prior_periods_on_file"]]

    # The same two runs, the right way round, ARE comparable.
    ok = secr_report(db, org.id, run_id=r25.id, intensity_denominator=2.0,
                     intensity_denominator_unit="£M revenue",
                     intensity_denominator_period_days=365,
                     comparative_run_id=r24.id)["prior_year_comparative"]
    assert ok["status"] == "computed"
    # A RISE, signed positive.
    assert ok["figures"]["total_location_based_tco2e"]["change"] == pytest.approx(1.0)


def test_overlapping_periods_are_refused(db, two_years):
    """A period that overlaps the reporting one is not prior to it either: the overlap
    is counted in both totals, so part of the 'change' is the same emissions twice."""
    org, _p24, p25, _r24, r25 = two_years
    straddle = make_period(db, org.id, label="H2-24/H1-25",
                           start="2024-07-01", end="2025-06-30")
    r_straddle = compute_co2e(db, org.id, reporting_period_id=straddle.id)
    r = secr_report(db, org.id, run_id=r25.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365,
                    comparative_run_id=r_straddle.id)
    c = r["prior_year_comparative"]
    assert c["reason_code"] == "not_a_prior_period"
    assert c["figures"] is None


def test_same_period_is_not_a_comparative(db, two_years):
    """Two runs of ONE period pass every length gate and differ by zero.

    That zero would be published as 'no change year on year'. The length gate
    structurally cannot ask whether two windows are the same window.
    """
    org, _p24, p25, _r24, r25 = two_years
    again = compute_co2e(db, org.id, reporting_period_id=p25.id)
    r = secr_report(db, org.id, run_id=again.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365,
                    comparative_run_id=r25.id)
    c = r["prior_year_comparative"]
    assert c["reason_code"] == "not_a_prior_period"
    assert c["figures"] is None
    assert any("SAME reporting period" in b for b in c["blockers"])


def test_a_run_is_not_its_own_comparative(db, two_years):
    org, _p24, _p25, _r24, r25 = two_years
    r = secr_report(db, org.id, run_id=r25.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365,
                    comparative_run_id=r25.id)
    assert r["prior_year_comparative"]["reason_code"] == "not_a_prior_period"
    assert r["prior_year_comparative"]["figures"] is None


# --- the cannot-determine discipline inside a computed comparative ----------------

def test_percent_change_off_a_zero_base_is_not_published(db, two_years):
    """An undefined percentage is cannot-determine, not infinity and not 100%."""
    from app.reports.comparative import _delta
    d = _delta(5.0, 0.0)
    assert d["change"] == pytest.approx(5.0)        # the absolute change is knowable
    assert d["change_pct"] is None
    assert "undefined" in d["pct_reason"]


def test_a_figure_the_report_declines_to_state_has_no_delta():
    """NULL and 0.0 stay distinguishable.

    A refused Cat-15 total arrives here as None. Substituting zero would report the
    refusal as a movement of the whole figure.
    """
    from app.reports.comparative import _delta
    d = _delta(None, 4.0)
    assert d["status"] == "cannot_determine"
    assert d["change"] is None and d["change_pct"] is None
    assert "current" in d["reason"]
    assert _delta(4.0, None)["status"] == "cannot_determine"


def test_both_renderers_report_the_same_verdict_on_one_pair(db, two_years):
    """Renderer disagreement is its own defect class: the same two runs must not be
    comparable in one framework and refused in another."""
    org, p24, _p25, _r24, r25 = two_years
    bad = _ar5_prior_run(db, org, p24)
    e = esrs_e1_report(db, org.id, run_id=r25.id, net_revenue_millions=10.0,
                       revenue_period_days=365,
                       comparative_run_id=bad.id)["e1_6_comparative"]
    s = secr_report(db, org.id, run_id=r25.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365,
                    comparative_run_id=bad.id)["prior_year_comparative"]
    assert e["status"] == s["status"] == "cannot_determine"
    assert e["reason_code"] == s["reason_code"] == "not_comparable"
    # Same gate set, so the same number of reasons — only the clause prefix differs.
    assert len(e["blockers"]) == len(s["blockers"])


def test_secr_shows_the_reason_where_the_working_would_be(db, two_years):
    """A refused comparative gets a BLOCKED derivation, not an absent section.

    An absent section reads as nothing to report; the reason belongs beside the figures
    a reader is deciding whether to rely on.
    """
    org, p24, _p25, _r24, r25 = two_years
    bad = _ar5_prior_run(db, org, p24)
    r = secr_report(db, org.id, run_id=r25.id, intensity_denominator=2.0,
                    intensity_denominator_unit="£M revenue",
                    intensity_denominator_period_days=365, comparative_run_id=bad.id)
    assert r["prior_year_comparative"]["reason_code"] == "not_comparable"
    rows = r["derivations"]["figures"]
    blocked = [d for d in rows if "Comparative" in d["figure"]]
    assert blocked, [d["figure"] for d in rows]
    # The REASON is carried into the working, whatever the reason happens to be. The
    # assertion is that the note is the block's own reason — not that it contains one
    # particular sentence, which pins the wording of a message that should be free to
    # get more specific.
    assert blocked[0]["note"] == r["prior_year_comparative"]["reason"]
    assert "cannot be subtracted" in blocked[0]["note"]
    # A refused calculation is counted as refused, so the report's own headline cannot
    # claim every figure reconciles while one has no working at all.
    assert r["derivations"]["all_reconcile"] is False
    assert blocked[0]["figure"] in r["derivations"]["calculations_refused"]
