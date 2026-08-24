"""An absence must never be reported as a finding.

Three places had collapsed "we did not measure this" into "this is zero", each producing
a confident positive statement out of missing data:

  * a crosswalk hop that fans out to 25 codes with no factors loaded reported sigma 0.0
    and the note "an unambiguous mapping adds no uncertainty" — about a 25-way fan-out;
  * an un-inventoried Scope 3 category left the SBTi denominator, took a 0.0% share, was
    declared not significant and dropped out of the required C14.1 target boundary,
    inflating every surviving category's share as the denominator shrank;
  * ESOS discarded the carriers its own energy reader had MEASURED as omitted and
    published the remainder as "total energy consumption".

The doctrine is stated everywhere in this codebase and was enforced at the primitive
level only. These pin it at the three dict boundaries where it failed.
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
