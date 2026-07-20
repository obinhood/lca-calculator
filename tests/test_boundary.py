"""GHG Protocol Ch.3 organisational boundary — the consolidation approach applied.

The audit finding: a 40%-owned JV was counted at 100% (a 2.5x overstatement) because
the declared approach was never applied. These tests pin the weight table, the
fail-open/fail-closed doctrine, the frozen boundary, and the market-branch trap.
"""
import pytest

from app.models import (
    Organisation, ActivityRecord, EmissionFactor, MarketInstrument,
    ReportingEntity, RunEntityBoundary, EmissionLineItem,
)
from app.services.calc import compute_co2e, activities_fingerprint, activities_in_scope
from app.services.boundary import entity_weight, boundary_completeness
from app.reports.summary import summary


def _org(db, name="Group", approach="equity_share", reason=None):
    o = Organisation(name=name, consolidation_approach=approach,
                     consolidation_approach_reason=reason or
                     "Equity share chosen to reflect our economic interest in joint ventures.")
    db.add(o); db.commit(); db.refresh(o)
    return o


def _entity(db, org_id, name="JV", category="joint_venture_incorporated", **kw):
    kw.setdefault("in_consolidated_accounting_group", False)
    e = ReportingEntity(organisation_id=org_id, name=name, accounting_category=category, **kw)
    db.add(e); db.commit(); db.refresh(e)
    return e


def _factor(db, value=0.5, geo="GB"):
    f = EmissionFactor(source="T", version="1", geography=geo, year=2024, category="electricity",
                       subcategory="", unit="kWh", gwp_set="AR6", value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _act(db, org_id, factor_id, entity_id=None, kwh=1000.0, geo="GB"):
    a = ActivityRecord(organisation_id=org_id, date="2025-06-01", category="electricity",
                       subcategory="", description="", quantity=kwh, unit="kWh",
                       geo=geo, factor_id=factor_id, entity_id=entity_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


# --- The weight table (accounting_category appears in NO branch) --------------

class _E:
    """A bare stand-in for a ReportingEntity — proves the weight is a pure function
    of the asserted facts, with no DB and no category involvement."""
    def __init__(self, **kw):
        for k in ("equity_share_pct", "financial_control", "joint_financial_control",
                  "operational_control"):
            setattr(self, k, kw.get(k))


def test_reporting_entity_itself_is_always_whole():
    for ap in ("equity_share", "financial_control", "operational_control"):
        assert entity_weight(ap, None) == (1.0, "reporting_entity_itself", True)


def test_forty_percent_jv_is_forty_percent_under_equity_share():
    """THE AUDIT FINDING: this JV was counted at 100%."""
    jv = _E(equity_share_pct=40.0, joint_financial_control=True, operational_control=True)
    w, basis, resolved = entity_weight("equity_share", jv)
    assert w == pytest.approx(0.40) and basis == "equity_share_pct" and resolved


def test_same_20pct_associate_flips_on_asserted_control_not_on_equity():
    """IFRS S2 educational material Ex. 2A vs 2B: identical 20% associate, opposite
    outcomes under a control approach. This is why control is an asserted column and
    never derived from equity % — the single most important property of the design."""
    a_2a = _E(equity_share_pct=20.0, financial_control=False, operational_control=True)
    a_2b = _E(equity_share_pct=20.0, financial_control=False, operational_control=False)
    assert entity_weight("operational_control", a_2a)[0] == 1.0     # Ex. 2A -> 100%
    assert entity_weight("operational_control", a_2b)[0] == 0.0     # Ex. 2B -> 0%
    # ...and under equity share both are 20%, regardless of control.
    assert entity_weight("equity_share", a_2a)[0] == pytest.approx(0.20)
    assert entity_weight("equity_share", a_2b)[0] == pytest.approx(0.20)


def test_financial_control_can_be_true_below_fifty_percent():
    e = _E(equity_share_pct=30.0, financial_control=True)
    assert entity_weight("financial_control", e) == (1.0, "financial_control", True)


def test_joint_financial_control_falls_back_to_equity():
    e = _E(equity_share_pct=40.0, joint_financial_control=True)
    w, basis, _ = entity_weight("financial_control", e)
    assert w == pytest.approx(0.40) and basis == "joint_financial_control_equity_share"


def test_operating_lease_zero_equity_but_operated():
    e = _E(equity_share_pct=0.0, financial_control=False, operational_control=True)
    assert entity_weight("equity_share", e)[0] == 0.0
    assert entity_weight("operational_control", e)[0] == 1.0


def test_unasserted_facts_fail_open_at_100pct_and_unresolved():
    """A missing fact is NEVER treated as 0% — understating must not happen silently."""
    blank = _E()
    for ap in ("equity_share", "financial_control", "operational_control"):
        w, basis, resolved = entity_weight(ap, blank)
        assert w == 1.0 and resolved is False and basis.startswith("unresolved_")


# --- End-to-end weighting -----------------------------------------------------

def test_jv_is_consolidated_at_its_share_end_to_end(db):
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, equity_share_pct=40.0, joint_financial_control=True,
                 equity_share_basis="40% of the ordinary shares per the JV agreement.")
    _act(db, org.id, _factor(db, 0.5).id, entity_id=jv.id, kwh=1000)   # 500 kg gross
    run = compute_co2e(db, org.id)
    assert run.total_co2e == pytest.approx(200.0)                      # 500 x 0.40
    assert run.total_co2e_non_consolidated == pytest.approx(300.0)     # the excluded 60%


def test_backward_compatible_when_no_entities(db):
    """Every pre-existing activity has entity_id NULL -> share 1.0 under all approaches."""
    for ap in ("equity_share", "financial_control", "operational_control"):
        org = _org(db, name=f"Co-{ap}", approach=ap)
        _act(db, org.id, _factor(db, 0.5).id, entity_id=None, kwh=1000)
        run = compute_co2e(db, org.id)
        assert run.total_co2e == pytest.approx(500.0)                  # unweighted
        assert run.total_co2e_non_consolidated == pytest.approx(0.0)


def test_sum_of_line_items_equals_total_under_weighting(db):
    """The invariant an assurer walks — preserved BY CONSTRUCTION by weighting the line."""
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, equity_share_pct=40.0, joint_financial_control=True)
    _act(db, org.id, _factor(db, 0.5).id, entity_id=jv.id)
    _act(db, org.id, _factor(db, 0.2).id, entity_id=None)
    run = compute_co2e(db, org.id)
    lines = db.query(EmissionLineItem).filter_by(run_id=run.id, method="location").all()
    assert sum(l.co2e for l in lines) == pytest.approx(run.total_co2e)


def test_frozen_boundary_closure_invariants(db):
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, equity_share_pct=40.0, joint_financial_control=True)
    _act(db, org.id, _factor(db, 0.5).id, entity_id=jv.id)
    _act(db, org.id, _factor(db, 0.2).id, entity_id=None)
    run = compute_co2e(db, org.id)
    rows = db.query(RunEntityBoundary).filter_by(run_id=run.id).all()
    assert sum(r.consolidated_co2e for r in rows) == pytest.approx(run.total_co2e)
    assert sum(r.gross_co2e - r.consolidated_co2e for r in rows) == \
        pytest.approx(run.total_co2e_non_consolidated)


def test_boundary_is_complete_by_construction(db):
    """A declared entity with no activities still gets a frozen row, plus always 'self'."""
    org = _org(db, approach="equity_share")
    _entity(db, org.id, name="Idle JV", equity_share_pct=10.0, joint_financial_control=True)
    _act(db, org.id, _factor(db).id, entity_id=None)
    run = compute_co2e(db, org.id)
    keys = {r.entity_key for r in db.query(RunEntityBoundary).filter_by(run_id=run.id).all()}
    assert "self" in keys and len(keys) == 2


# --- The market-branch trap ---------------------------------------------------

def test_market_line_runs_the_pool_in_gross_then_weights(db):
    """THE TRAP: the pool prices contractual kWh at the instrument's UNWEIGHTED rate, so
    a pre-weighted grid_rate mixes bases and yields a plausible-but-wrong market line."""
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, equity_share_pct=40.0, joint_financial_control=True)
    _act(db, org.id, _factor(db, 0.5).id, entity_id=jv.id, kwh=1000)   # gross 500 kg
    db.add(MarketInstrument(organisation_id=org.id, instrument_type="supplier_specific",
                            kg_co2e_per_kwh=0.1, coverage_kwh=600.0, gwp_set="AR6",
                            market="GB", start_date="2025-01-01", end_date="2025-12-31"))
    db.commit()
    run = compute_co2e(db, org.id)
    # Correct: gross market = 600*0.1 + 400*0.5 = 260; consolidated = 260 * 0.40 = 104.
    # The bug (grid_rate from the WEIGHTED co2e = 0.2) would give 600*0.1 + 400*0.2 = 140.
    assert run.total_co2e_market == pytest.approx(104.0)
    assert run.total_co2e == pytest.approx(200.0)                      # location, weighted


def test_market_equals_location_when_no_instruments_even_when_weighted(db):
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, equity_share_pct=40.0, joint_financial_control=True)
    _act(db, org.id, _factor(db, 0.5).id, entity_id=jv.id)
    run = compute_co2e(db, org.id)
    assert run.total_co2e_market == pytest.approx(run.total_co2e)


# --- Fail-open on the number, fail-closed on the disclosure -------------------

def test_unresolved_share_is_included_at_100pct_and_blocks(db):
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, equity_share_pct=None,          # not asserted
                 joint_financial_control=True)
    _act(db, org.id, _factor(db, 0.5).id, entity_id=jv.id, kwh=1000)
    run = compute_co2e(db, org.id)
    assert run.total_co2e == pytest.approx(500.0)            # 100% — never understated
    g = boundary_completeness(db, run)
    assert any("UNRESOLVED share" in b for b in g["blockers"])


def test_dangling_entity_id_gets_its_own_bucket_not_self(db):
    """A dangling/cross-tenant entity_id must not pollute the reporting entity's row."""
    org = _org(db, approach="equity_share")
    _act(db, org.id, _factor(db, 0.5).id, entity_id=None, kwh=100)     # real self activity
    a = _act(db, org.id, _factor(db, 0.5).id, entity_id=None, kwh=1000)
    a.entity_id = 9999                                                  # points at nothing
    db.commit()
    run = compute_co2e(db, org.id)
    rows = {r.entity_key: r for r in db.query(RunEntityBoundary).filter_by(run_id=run.id).all()}
    assert rows["self"].resolved is True                                # self is untouched
    assert rows["e:9999"].resolved is False
    assert rows["e:9999"].share_basis == "unresolved_entity_not_found"
    assert any("does not exist" in b for b in boundary_completeness(db, run)["blockers"])


def test_cross_tenant_entity_is_never_resolvable(db):
    a_org, b_org = _org(db, name="A"), _org(db, name="B")
    b_ent = _entity(db, b_org.id, name="B JV", equity_share_pct=40.0,
                    joint_financial_control=True)
    act = _act(db, a_org.id, _factor(db).id, entity_id=None)
    act.entity_id = b_ent.id            # another tenant's entity
    db.commit()
    run = compute_co2e(db, a_org.id)
    assert any("does not exist" in b for b in boundary_completeness(db, run)["blockers"])


def test_unclassified_group_blocks(db):
    org = _org(db, approach="equity_share")
    _entity(db, org.id, equity_share_pct=40.0, joint_financial_control=True,
            in_consolidated_accounting_group=None)      # not asserted
    _act(db, org.id, _factor(db).id, entity_id=None)
    run = compute_co2e(db, org.id)
    assert any("financial-statement consolidation status" in b
               for b in boundary_completeness(db, run)["blockers"])


# --- Reproduction / forgery-by-edit ------------------------------------------

def test_filed_run_is_unchanged_by_a_later_ownership_edit(db):
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, equity_share_pct=40.0, joint_financial_control=True)
    _act(db, org.id, _factor(db, 0.5).id, entity_id=jv.id, kwh=1000)
    run = compute_co2e(db, org.id)
    assert run.total_co2e == pytest.approx(200.0)
    jv.equity_share_pct = 100.0                     # restate ownership AFTER filing
    db.commit()
    assert run.total_co2e == pytest.approx(200.0)   # the FILED figure is unchanged
    frozen = db.query(RunEntityBoundary).filter_by(run_id=run.id, entity_key=f"e:{jv.id}").one()
    assert frozen.share_factor == pytest.approx(0.40)
    assert any("EDITED since this run froze it" in b
               for b in boundary_completeness(db, run)["blockers"])


def test_two_fingerprints_detect_different_forgeries(db):
    """An entity edit must move the CONSOLIDATION fingerprint and not the activities one."""
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, equity_share_pct=40.0, joint_financial_control=True)
    _act(db, org.id, _factor(db, 0.5).id, entity_id=jv.id)
    run = compute_co2e(db, org.id)
    acts_fp_before = activities_fingerprint(activities_in_scope(db, org.id, None))
    jv.equity_share_pct = 90.0
    db.commit()
    # activities are untouched -> the activity fingerprint is blind to this by design...
    assert activities_fingerprint(activities_in_scope(db, org.id, None)) == acts_fp_before
    # ...which is exactly why the boundary needs its own.
    assert any("EDITED since this run" in b for b in boundary_completeness(db, run)["blockers"])


def test_excluded_residual_with_no_scope3_home_blocks(db):
    """Applying a share without re-routing the excluded operations is a REAL completeness
    hole. The platform measures it and refuses to file — it will NOT invent the routing."""
    org = _org(db, approach="operational_control",
               reason="Operational control chosen; we report what we operate day to day.")
    jv = _entity(db, org.id, name="Associate", category="associate",
                 equity_share_pct=20.0, financial_control=False, operational_control=False)
    _act(db, org.id, _factor(db, 0.5).id, entity_id=jv.id, kwh=1000)   # excluded entirely
    run = compute_co2e(db, org.id)
    assert run.total_co2e == pytest.approx(0.0)                        # 0% consolidated
    assert run.total_co2e_non_consolidated == pytest.approx(500.0)     # measured, not lost
    b = boundary_completeness(db, run)["blockers"]
    assert any("EXCLUDED from the inventory" in x and "category [15]" in x for x in b)


def test_boundary_gate_blocks_the_disclosure(db):
    from app.reports.issb_s2 import issb_s2_report
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, equity_share_pct=None, joint_financial_control=True)
    _act(db, org.id, _factor(db).id, entity_id=jv.id)
    run = compute_co2e(db, org.id)
    r = issb_s2_report(db, org.id, run_id=run.id)
    assert r["disclosure_ready"] is False
    assert any("UNRESOLVED share" in b for b in r["blockers"])


def test_summary_exposes_the_gross_share_consolidated_walk(db):
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, equity_share_pct=40.0, joint_financial_control=True)
    _act(db, org.id, _factor(db, 0.5).id, entity_id=jv.id, kwh=1000)
    run = compute_co2e(db, org.id)
    c = summary(db, organisation_id=org.id, run_id=run.id)["consolidation"]
    assert c["assessable"] is True and c["approach"] == "equity_share"
    row = next(e for e in c["entities"] if e["entity_key"] == f"e:{jv.id}")
    assert row["gross_co2e_kg"] == pytest.approx(500.0)
    assert row["share_factor"] == pytest.approx(0.40)
    assert row["consolidated_co2e_kg"] == pytest.approx(200.0)
    assert c["excluded_by_boundary_kg"] == pytest.approx(300.0)
    # IFRS S2 29(a)(iv): the split is on the FINANCIAL group, not the GHGP category.
    assert set(c["disaggregation_by_accounting_group"]) == {
        "consolidated_accounting_group", "other_investee"}


# --- Adversarial-review fixes -------------------------------------------------

def test_denied_sole_control_with_unasserted_joint_control_never_silently_excludes():
    """Review finding: a falsy check conflated NULL with False, so a 50/50 JV whose
    joint control was never asserted returned 0% with resolved=True — a SILENT
    UNDERSTATEMENT, the one failure the doctrine forbids."""
    jv = _E(equity_share_pct=50.0, financial_control=False, joint_financial_control=None)
    w, basis, resolved = entity_weight("financial_control", jv)
    assert w == 1.0 and resolved is False                  # fail-OPEN, then block
    assert basis == "unresolved_joint_financial_control_not_asserted"
    # ...but once BOTH facts are asserted, 0% is honest and resolved.
    jv2 = _E(equity_share_pct=50.0, financial_control=False, joint_financial_control=False)
    assert entity_weight("financial_control", jv2) == (0.0, "no_financial_control", True)


def test_zero_equity_share_is_a_real_share_not_a_missing_fact():
    """0.0 is ASSERTED; None is not. A falsy check would conflate them."""
    e = _E(equity_share_pct=0.0)
    assert entity_weight("equity_share", e) == (0.0, "equity_share_pct", True)


def test_esrs_energy_is_on_the_same_basis_as_its_emissions(db):
    """Review finding: gross kWh next to consolidated tCO2e implies a wrong intensity."""
    from app.reports.esrs_e1 import esrs_e1_report
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, equity_share_pct=40.0, joint_financial_control=True,
                 equity_share_basis="40% of the ordinary shares per the JV agreement.")
    _act(db, org.id, _factor(db, 0.5).id, entity_id=jv.id, kwh=1000)   # gross 500 kg
    run = compute_co2e(db, org.id)
    r = esrs_e1_report(db, org.id, run_id=run.id, net_revenue_millions=1.0)
    e = r["e1_6_gross_ghg_emissions_tco2e"]
    energy = r["e1_5_energy_consumption"]
    assert e["scope2_location_based"] == pytest.approx(0.2)            # consolidated
    assert energy["total_mwh"] == pytest.approx(0.4)                   # 1000 kWh x 0.40
    # the implied intensity must reconcile to the real factor (0.5 kgCO2e/kWh)
    implied = (e["scope2_location_based"] * 1000.0) / (energy["total_mwh"] * 1000.0)
    assert implied == pytest.approx(0.5)


def test_secr_energy_stays_gross_physical_and_says_so(db):
    """SECR reports UK energy USE at operated sites — a physical quantity, not an
    equity share of one. The basis is labelled so it is never read as consolidated."""
    from app.reports.secr import secr_report
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, equity_share_pct=40.0, joint_financial_control=True)
    _act(db, org.id, _factor(db, 0.5).id, entity_id=jv.id, kwh=1000)
    run = compute_co2e(db, org.id)
    r = secr_report(db, org.id, run_id=run.id, intensity_denominator=1.0)
    assert r["energy_use_kwh"]["total_kwh"] == pytest.approx(1000.0)   # gross
    assert r["energy_use_kwh"]["basis"] == "gross_physical_energy"


def test_declared_entity_with_no_activities_does_not_demand_a_scope3_declaration(db):
    """Review finding: B8 must only fire for entities that actually excluded something."""
    org = _org(db, approach="operational_control",
               reason="Operational control chosen; we report what we operate.")
    _entity(db, org.id, name="Idle associate", category="associate",
            equity_share_pct=20.0, financial_control=False, operational_control=False)
    _act(db, org.id, _factor(db).id, entity_id=None)      # only own-operations activity
    run = compute_co2e(db, org.id)
    assert run.total_co2e_non_consolidated == pytest.approx(0.0)
    assert not any("EXCLUDED from the inventory" in b
                   for b in boundary_completeness(db, run)["blockers"])


def test_legacy_run_is_not_assessable(db):
    org = _org(db)
    _act(db, org.id, _factor(db).id)
    run = compute_co2e(db, org.id)
    run.boundary_version = None                     # a pre-boundary run
    db.commit()
    g = boundary_completeness(db, run)
    assert g["assessable"] is False
    assert summary(db, organisation_id=org.id, run_id=run.id)["consolidation"]["assessable"] is False


# --- IFRS S2 ¶29(a)(iv) per-scope disaggregation ------------------------------

def _gas_factor(db, value=0.2):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024, category="gas",
                       subcategory="", unit="kWh", gwp_set="AR6", value=value)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _gas_act(db, org_id, factor_id, entity_id=None, kwh=1000.0):
    a = ActivityRecord(organisation_id=org_id, date="2025-06-01", category="gas",
                       subcategory="", description="", quantity=kwh, unit="kWh",
                       geo="GB", factor_id=factor_id, entity_id=entity_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


def test_29a_iv_splits_scope1_and_scope2_by_accounting_group(db):
    """IFRS S2 ¶29(a)(iv) is a Scope 1 / Scope 2 split — not the all-scope figure the
    disaggregation used to report. Scope 2 is on the CONSOLIDATED location basis and
    reconciles, group by group, to the run total."""
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, equity_share_pct=40.0, joint_financial_control=True,
                 equity_share_basis="40% of the ordinary shares per the JV agreement.")
    elec = _factor(db, 0.5).id                       # electricity -> Scope 2
    gas = _gas_factor(db, 0.2).id                     # gas -> Scope 1
    # Reporting org (self, consolidated group, weight 1.0)
    _act(db, org.id, elec, entity_id=None, kwh=1000)          # 500 kg S2
    _gas_act(db, org.id, gas, entity_id=None, kwh=1000)       # 200 kg S1
    # JV (other investee, weight 0.40)
    _act(db, org.id, elec, entity_id=jv.id, kwh=1000)         # 500 gross -> 200 S2
    _gas_act(db, org.id, gas, entity_id=jv.id, kwh=1000)      # 200 gross -> 80 S1
    run = compute_co2e(db, org.id)

    c = summary(db, organisation_id=org.id, run_id=run.id)["consolidation"]
    assert c["disaggregation_scope_split_available"] is True
    assert c["disaggregation_basis"] == "scope1_and_scope2_location_ifrs_s2_29a_iv"
    d = c["disaggregation_by_accounting_group"]
    grp = d["consolidated_accounting_group"]
    assert grp["scope1_co2e_kg"] == pytest.approx(200.0)
    assert grp["scope2_location_co2e_kg"] == pytest.approx(500.0)
    assert grp["scope1_2_co2e_kg"] == pytest.approx(700.0)
    inv = d["other_investee"]
    assert inv["scope1_co2e_kg"] == pytest.approx(80.0)       # 200 * 0.40
    assert inv["scope2_location_co2e_kg"] == pytest.approx(200.0)   # 500 * 0.40
    assert inv["scope1_2_co2e_kg"] == pytest.approx(280.0)
    # The per-scope split reconciles to the consolidated run total (location basis).
    assert grp["scope1_2_co2e_kg"] + inv["scope1_2_co2e_kg"] == pytest.approx(run.total_co2e)
    # And the ISSB report — the clause's own renderer — surfaces the same numbers.
    from app.reports.issb_s2 import issb_s2_report
    r = issb_s2_report(db, org.id, run_id=run.id)
    dd = r["scope1_2_disaggregation_29a_iv"]
    assert dd["scope_split_available"] is True
    assert dd["by_accounting_group"]["other_investee"]["scope1_2_co2e_kg"] == pytest.approx(280.0)


def test_29a_iv_falls_back_for_a_run_frozen_before_the_per_scope_columns(db):
    """Reproduction contract: a run whose boundary rows never froze the per-scope split
    (NULL columns) must NOT report a silent Scope 1/2 of 0 — it falls back to the
    all-scope figure and flags scope_split_available=False."""
    org = _org(db, approach="equity_share")
    jv = _entity(db, org.id, equity_share_pct=40.0, joint_financial_control=True)
    _act(db, org.id, _factor(db, 0.5).id, entity_id=jv.id, kwh=1000)   # 200 kg consolidated
    run = compute_co2e(db, org.id)
    # Simulate a legacy freeze: blank the per-scope columns on every boundary row.
    for r in db.query(RunEntityBoundary).filter(RunEntityBoundary.run_id == run.id).all():
        r.scope1_consolidated_co2e = None
        r.scope2_consolidated_co2e = None
    db.commit()
    c = summary(db, organisation_id=org.id, run_id=run.id)["consolidation"]
    assert c["disaggregation_scope_split_available"] is False
    assert c["disaggregation_basis"] == "all_scopes_only_run_predates_per_scope_freeze"
    inv = c["disaggregation_by_accounting_group"]["other_investee"]
    assert inv["consolidated_all_scopes_co2e_kg"] == pytest.approx(200.0)
    assert "scope1_co2e_kg" not in inv                     # not a silent zero
