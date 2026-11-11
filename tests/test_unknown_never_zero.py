"""An absence must never be reported as a finding.

Four places had collapsed "we did not measure this" into "this is zero", each producing
a confident positive statement out of missing data:

  * a crosswalk hop that fans out to 25 codes with no factors loaded reported sigma 0.0
    and the note "an unambiguous mapping adds no uncertainty" — about a 25-way fan-out;
  * an un-inventoried Scope 3 category left the SBTi denominator, took a 0.0% share, was
    declared not significant and dropped out of the required C14.1 target boundary,
    inflating every surviving category's share as the denominator shrank;
  * ESOS discarded the carriers its own energy reader had MEASURED as omitted and
    published the remainder as "total energy consumption";
  * three own-operations energy disclosures certified their kWh inventory COMPLETE
    because the Scope 1/2 filter they read it through matched NO LINE ITEMS, so no
    carrier could be recorded as omitted — an empty measurement read as a complete one.

The doctrine is stated everywhere in this codebase and was enforced at the primitive
level only. These pin it at the four dict boundaries where it failed.
"""
import json

import pytest

from app.models import (
    Organisation, EmissionFactor, ActivityRecord, Crosswalk, CrosswalkMapping,
    ReportingPeriod,
)
from app.services.crosswalk import dispersion_sigma, hop_uncertainty, chain_uncertainty
from app.services.sbti_v2 import significance, target_boundary
from app.services.calc import compute_co2e
from app.reports.compliance_extra import esos_report


# --- crosswalk: ambiguous but unmeasured is not clean ------------------------------------

def test_an_ambiguous_hop_with_no_factors_reports_unknown_not_zero():
    """sigma 0.0 was the SAME answer given to a genuinely one-to-one mapping."""
    one_to_one = dispersion_sigma([], cardinality=1)
    assert one_to_one["sigma"] == 0.0, "an unambiguous hop really does add no uncertainty"

    ambiguous = dispersion_sigma([], cardinality=25)
    assert ambiguous["sigma"] is None, (
        "a hop fanning out to 25 codes with no factors loaded has UNKNOWN dispersion; "
        "reporting 0.0 states that the mapping is unambiguous, which is the opposite of "
        "what was observed")
    assert ambiguous["basis"] == "unmeasurable_dispersion"
    assert "25" in ambiguous["note"]


def test_a_measured_hop_is_unaffected():
    d = dispersion_sigma([1.0, 4.0], cardinality=2)
    assert d["sigma"] > 0 and d["basis"] == "measured_candidate_dispersion"


def _ambiguous_chain(db):
    """A hop that RESOLVES to several codes for which no factor exists."""
    org = Organisation(name="XwOrg"); db.add(org); db.commit(); db.refresh(org)
    # resolve() upper-cases the lookup, so the row is stored the same way.
    xw = Crosswalk(from_scheme="NAICS", to_scheme="ISIC", table_version="v1",
                   source="test")
    db.add(xw); db.commit(); db.refresh(xw)
    for code in ("1001", "1002", "1003"):
        db.add(CrosswalkMapping(crosswalk_id=xw.id, from_code="500", to_code=code))
    db.commit()
    return org


def test_a_chain_containing_an_unmeasurable_hop_is_not_quantifiable(db):
    _ambiguous_chain(db)
    out = chain_uncertainty(db, [{"from_scheme": "naics", "from_code": "500",
                                  "to_scheme": "isic", "table_version": "v1"}])
    assert out["quantifiable"] is False, (
        f"a chain whose only hop has unmeasured dispersion cannot be quantified; got "
        f"total_sigma={out['total_sigma']}")
    assert out["unmeasurable_hops"] == ["naics->isic"]
    assert out["total_sigma"] is None


# --- SBTi: a category never inventoried cannot be shown to sit below 5% ------------------

def test_an_un_inventoried_scope3_category_suspends_significance():
    """The defect end to end: category 1 is 80% of the real inventory but was never
    screened, so reading its absence as zero removed it from the denominator and inflated
    every other category roughly fivefold."""
    measured = {2: 10.0, 3: 10.0}          # cat 1 exists in reality but was never screened
    screened = {2, 3}                       # ...and the screen only decided on 2 and 3

    loose = significance(measured)
    assert loose["determinable"] is True
    assert loose["shares"][2] == pytest.approx(50.0), "old behaviour, kept for legacy runs"

    strict = significance(measured, inventoried=screened)
    assert strict["determinable"] is False, (
        "twelve of the fourteen denominator categories were never inventoried; no share "
        "computed against that denominator can be trusted")
    assert 1 in strict["categories_not_inventoried"]
    assert strict["denominator_tco2e"] is None


def test_a_category_measured_at_zero_is_not_the_same_as_one_never_measured():
    """The distinction the whole fix rests on."""
    everything = set(range(1, 15))
    measured_zero = significance({c: 0.0 for c in range(1, 15)} | {5: 100.0},
                                 inventoried=everything)
    assert measured_zero["determinable"] is True, (
        "every category WAS screened; thirteen of them genuinely are zero")
    assert measured_zero["significant"] == [5]


def test_an_unmeasured_category_cannot_produce_a_conformant_target_boundary():
    strict = significance({2: 10.0}, inventoried={2})
    tb = target_boundary(strict, covered_categories=[2])
    assert tb["conformant"] is False, (
        "a boundary cannot be shown to cover every significant category while some "
        "categories have never been measured")


# --- ESOS: a total measured as incomplete is not a total ---------------------------------

def _energy_org(db, with_omitted_carrier: bool):
    org = Organisation(name=f"EsosOrg{int(with_omitted_carrier)}")
    db.add(org); db.commit(); db.refresh(org)

    def factor(cat, unit, value):
        f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                           category=cat, subcategory="", unit=unit, gwp_set="AR6",
                           value=value)
        db.add(f); db.commit(); db.refresh(f)
        return f

    def act(cat, qty, unit, f):
        db.add(ActivityRecord(organisation_id=org.id, date="2025-06-01", category=cat,
                              subcategory="", description="", quantity=qty, unit=unit,
                              geo="GB", factor_id=f.id))
        db.commit()

    act("electricity", 3000, "kWh", factor("electricity", "kWh", 0.17))
    act("gas", 1000, "kWh", factor("gas", "kWh", 0.184))
    if with_omitted_carrier:
        # Energy-denominated, emitting, and outside the reported carrier set — the exact
        # case _energy_kwh records in carriers_omitted.
        act("district_heating", 500, "kWh", factor("district_heating", "kWh", 0.21))
    return org, compute_co2e(db, org.id)


def test_esos_refuses_to_call_an_incomplete_figure_the_total(db):
    org, run = _energy_org(db, with_omitted_carrier=True)
    r = esos_report(db, org.id, run_id=run.id)

    assert r["carriers_omitted"], "fixture must actually omit a carrier"
    assert r["report_ready"] is False, (
        f"ESOS assesses TOTAL energy consumption; this figure excludes "
        f"{r['carriers_omitted']} whose emissions are reported beside it. Blockers: "
        f"{r['blockers']}")
    assert any("total" in b.lower() for b in r["blockers"])
    # And the shares must not be labelled as shares of a total they are not shares of.
    assert "significant_energy_use_pct" not in r
    assert "share_of_reported_carriers_pct" in r


def test_esos_is_ready_when_nothing_is_omitted(db):
    org, run = _energy_org(db, with_omitted_carrier=False)
    r = esos_report(db, org.id, run_id=run.id)
    assert r["carriers_omitted"] == []
    assert r["report_ready"] is True, r["blockers"]
    assert r["total_energy_kwh"] == pytest.approx(4000.0)


# --- own-operations energy: zero rows matched is not "nothing omitted" -------------------
# `_energy_kwh(scopes=("1","2"))` reads the run's FROZEN scope to decide what belongs in an
# own-operations energy total. On a run whose categories miss SCOPE_RULES every line is
# defaulted to Scope 3 and frozen `assumed_scope3`, so that filter matches NOTHING: the
# per-carrier totals stay 0.0 and the `omitted` dict — which measures energy-denominated
# activities the total could not include — stays empty because no row was ever examined.
# Three renderers published `not omitted` as their completeness flag, so each certified a
# COMPLETE energy inventory on the strength of having measured none of it.

def _energy_claims(db, org_id, run_id):
    """Every own-operations energy completeness claim in the renderer layer, with the
    readiness key each renderer actually uses — a probe that reads a key a payload does
    not have reports no defect."""
    from app.reports.ecovadis import ecovadis_readiness
    from app.reports.gri import gri_report
    from app.reports.esrs_e1 import esrs_e1_report

    ev = ecovadis_readiness(db, org_id, run_id=run_id)
    gri = gri_report(db, org_id, run_id=run_id, intensity_denominator=10.0,
                     intensity_denominator_unit="t product",
                     intensity_denominator_period_days=365)
    esrs = esrs_e1_report(db, org_id, run_id=run_id, net_revenue_millions=5.0,
                          revenue_period_days=365)
    assert ev["kpis"]["energy_kwh"] is not None
    assert gri["gri_302_3_energy_intensity"] is not None, "control needs the 302-3 block"
    return {
        "ecovadis kpis.energy_complete": (
            ev["kpis"]["energy_complete"], ev["kpis"]["energy_determinable"],
            ev["blockers"], ev["assessment_ready"]),
        "gri 302-1 energy.complete": (
            gri["gri_302_1_energy"]["complete"], gri["gri_302_1_energy"]["determinable"],
            gri["blockers"], gri["disclosure_ready"]),
        "gri 302-3 energy_intensity.complete": (
            gri["gri_302_3_energy_intensity"]["complete"],
            gri["gri_302_3_energy_intensity"]["determinable"],
            gri["blockers"], gri["disclosure_ready"]),
        "esrs e1_5_energy_consumption.complete": (
            esrs["e1_5_energy_consumption"]["complete"],
            esrs["e1_5_energy_consumption"]["determinable"],
            esrs["blockers"], esrs["disclosure_ready"]),
    }


def test_no_renderer_certifies_an_energy_total_it_could_not_measure(db):
    from tests.scope3_util import assumed_scope_run, ASSUMED_CATEGORIES
    from app.reports.secr import _energy_kwh
    org, run = assumed_scope_run(db)

    # The mechanism, on ONE run read two ways. Unscoped, the reader EXAMINES all three
    # lines and measures all three as omitted carriers. Scope-filtered, it examines none.
    unscoped = _energy_kwh(db, run)
    assert set(unscoped["carriers_omitted"]) == set(ASSUMED_CATEGORIES), (
        "fixture must have energy-denominated carriers to omit once they ARE examined")
    scoped = _energy_kwh(db, run, scopes=("1", "2"), consolidated=True)
    assert scoped["total_kwh"] == 0.0
    assert not scoped.get("carriers_omitted"), (
        "no row matched the Scope 1/2 filter, so nothing could be recorded as omitted — "
        "this empty dict is the absence of a measurement, not a measurement of zero")
    assert scoped["determinable"] is False
    assert scoped["complete"] is None, (
        f"complete={scoped['complete']!r} over 0.0 kWh: the same run reports "
        f"{sorted(unscoped['carriers_omitted'])} as omitted when its lines are actually "
        f"examined. An empty measurement is neither complete nor measured-incomplete")

    # The invariant at the source, for ANY scope filter — so a fourth caller inherits it.
    for subset in (("1",), ("2",), ("1", "2"), ("3",), ("1", "2", "3")):
        assert _energy_kwh(db, run, scopes=subset)["complete"] is not True, subset

    # And no renderer may publish the certification.
    for name, (complete, determinable, blockers, ready) in _energy_claims(
            db, org.id, run.id).items():
        assert complete is None, (
            f"{name} published complete={complete!r} over an own-operations energy total "
            f"of 0.0 that the engine never observed")
        assert determinable is False, name
        assert any("cannot be measured" in b for b in blockers), (
            f"{name}: the refusal must name the ENERGY figure, not only the emissions "
            f"split — a disclosure over an unmeasurable total refuses, it does not "
            f"certify. Blockers: {blockers}")
        assert ready is False, name


def test_a_declared_scope_makes_the_same_energy_figure_measurable_again(db):
    """The negative control for the guard above, with the CONFOUND held still.

    `explicit_scope_run` carries the same category strings as the defect fixture with the
    scope DECLARED on the activity, so the only thing that moves is the scope mechanism.
    `_energy_kwh` buckets carriers BY CATEGORY NAME, so a control that renamed them
    (`declared_scope_run`) would move the carrier mechanism at the same time and could not
    tell a working guard from one that refuses everything.
    """
    from tests.scope3_util import assumed_scope_run, explicit_scope_run, ASSUMED_CATEGORIES
    from app.reports.secr import _energy_kwh
    org, run = explicit_scope_run(db)
    _org_a, run_a = assumed_scope_run(db)

    scoped = _energy_kwh(db, run, scopes=("1", "2"), consolidated=True)
    # Identical carrier verdict on both runs once the rows are examined: the pair differs
    # in the scope mechanism alone.
    assert set(scoped["carriers_omitted"]) == set(ASSUMED_CATEGORIES)
    assert scoped["carriers_omitted"] == _energy_kwh(db, run_a)["carriers_omitted"]
    assert scoped["determinable"] is True
    assert scoped["complete"] is False, (
        "every line was examined and three carriers were measured as omitted — that is a "
        "MEASURED incomplete inventory, which must stay distinguishable from an "
        "unmeasurable one (None)")
    assert scoped["blockers"] == []

    for name, (complete, determinable, blockers, ready) in _energy_claims(
            db, org.id, run.id).items():
        assert complete is False, f"{name} lost a measured verdict: {complete!r}"
        assert determinable is True, name
        assert not any("cannot be measured" in b for b in blockers), (
            f"{name} refused a figure it CAN measure — a gate that refuses both members "
            f"of the pair is refusing everything. Blockers: {blockers}")


def test_the_unscoped_energy_total_is_unaffected_by_a_guessed_scope(db):
    """SECR's and ESOS's total energy use never reads the scope classification, so a
    guessed scope cannot move a line in or out of it — the figure stays determinable and
    its refusal keeps coming from the omission measurement."""
    from tests.scope3_util import assumed_scope_run
    from app.reports.secr import _energy_kwh
    org, run = assumed_scope_run(db)

    e = _energy_kwh(db, run)
    assert e["determinable"] is True
    assert e["complete"] is False
    assert e["blockers"] == []

    r = esos_report(db, org.id, run_id=run.id)
    assert r["report_ready"] is False
    assert any("total" in b.lower() for b in r["blockers"])
    assert not any("cannot be measured" in b for b in r["blockers"])
