"""Independent verification that the numbers are RIGHT.

Existing tests mostly check the engine against itself (call a helper, assert the helper's
answer). These check it four other ways, each of which can fail even when every unit test
passes:

  A. ORACLE — recompute the expected total in the test with arithmetic that shares NO code
     with the engine (plain quantity x factor, summed in Python), and require the engine to
     match to the cent. Units are chosen to match the factor exactly so the oracle stays
     genuinely independent; unit conversion is verified separately in C.
  B. CONSERVATION — the immutable-inventory invariants: the headline total is exactly the sum
     of its location line items, the per-scope figures re-sum to the total, and the market
     total is built from market lines only. A drift here means a disclosed figure and its own
     audit trail disagree.
  C. METAMORPHIC — properties that must hold for ANY dataset: scaling every quantity by k
     scales the total by k; splitting a row in two leaves the total unchanged; row order does
     not matter; a zero row adds nothing. These catch whole classes of error (ordering,
     accumulation, rounding, double counting) that fixed examples cannot.
  D. CROSS-RENDERER — every framework that reports the same underlying quantity must report
     the SAME NUMBER from one run. Divergence means at least one disclosure is wrong.
"""
import itertools

import pytest

from app.models import (
    EmissionFactor, ActivityRecord, EmissionLineItem, FinancedPosition, Organisation,
)
from app.services.calc import compute_co2e


# ---------------------------------------------------------------------------------------
# Fixture helpers. Units deliberately match each factor's own unit so the ORACLE can be
# `quantity * value` with no unit-conversion code shared with the engine.

def _org(db, name="VerifyCo"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category, unit, value, subcategory="", **kw):
    f = EmissionFactor(source="VERIFY", version="1", geography="GB", year=2024,
                       category=category, subcategory=subcategory, unit=unit,
                       gwp_set="AR6", value=value, **kw)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, factor, quantity, category=None, unit=None, date="2025-06-15"):
    a = ActivityRecord(organisation_id=org_id, date=date,
                       category=category or factor.category, subcategory="", description="",
                       quantity=quantity, unit=unit or factor.unit, geo="GB",
                       factor_id=factor.id)
    db.add(a); db.commit(); db.refresh(a)
    return a


# A mixed, deliberately awkward dataset: three scopes, uneven magnitudes, repeated
# categories, and values that do not round cleanly.
_DATASET = [
    ("electricity", "kWh", 0.17,     [1234.5, 98.76, 4321.0]),     # Scope 2
    ("gas",         "kWh", 0.18293,  [800.25, 1199.75]),           # Scope 1
    ("diesel",      "L",   2.66155,  [37.3]),                      # Scope 1
    ("waste",       "kg",  0.4805,   [250.5, 749.5]),              # Scope 3
]


def _build(db, org):
    """Create the dataset and return the ORACLE total computed independently."""
    oracle = 0.0
    for category, unit, value, quantities in _DATASET:
        f = _factor(db, category, unit, value)
        for q in quantities:
            _activity(db, org.id, f, q)
            oracle += q * value          # <- the whole oracle: no engine code involved
    return oracle


# ---------------------------------------------------------------------------------------
# A. Oracle

def test_engine_total_matches_an_independent_recomputation(db):
    org = _org(db)
    oracle = _build(db, org)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    assert run.total_co2e == pytest.approx(oracle, rel=1e-9), (
        f"engine {run.total_co2e} vs independent {oracle}")


def test_every_line_item_matches_its_own_quantity_times_factor(db):
    """Not just the total — each individual line must be right, or two errors could cancel."""
    org = _org(db)
    _build(db, org)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    lines = db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id, EmissionLineItem.method == "location").all()
    assert lines
    for li in lines:
        act = db.get(ActivityRecord, li.activity_id)
        fac = db.get(EmissionFactor, act.factor_id)
        assert li.co2e == pytest.approx(act.quantity * fac.value, rel=1e-9), (
            f"line {li.id}: {li.co2e} != {act.quantity} x {fac.value}")


# ---------------------------------------------------------------------------------------
# B. Conservation

def test_total_equals_sum_of_location_line_items(db):
    """The immutable-inventory invariant: the headline number IS its audit trail."""
    org = _org(db)
    _build(db, org)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    summed = sum(li.co2e for li in db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id, EmissionLineItem.method == "location").all())
    assert run.total_co2e == pytest.approx(summed, rel=1e-12)


def test_scope_subtotals_resum_to_the_total(db):
    org = _org(db)
    _build(db, org)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    by_scope = {}
    for li in db.query(EmissionLineItem).filter(
            EmissionLineItem.run_id == run.id, EmissionLineItem.method == "location").all():
        by_scope[li.scope] = by_scope.get(li.scope, 0.0) + li.co2e
    assert sum(by_scope.values()) == pytest.approx(run.total_co2e, rel=1e-12)
    assert set(by_scope) <= {"1", "2", "3"}


def test_market_total_is_built_from_market_lines_only(db):
    """A market-based total that silently mixed in location lines would double count."""
    org = _org(db)
    _build(db, org)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    market = sum(li.co2e for li in db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id, EmissionLineItem.method == "market").all())
    location_non_s2 = sum(li.co2e for li in db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id, EmissionLineItem.method == "location",
        EmissionLineItem.scope != "2").all())
    assert run.total_co2e_market == pytest.approx(market + location_non_s2, rel=1e-9)


def test_no_duplicate_line_per_activity_and_method(db):
    """Accumulation across runs/recomputes would inflate every total."""
    org = _org(db)
    _build(db, org)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    seen = [(li.activity_id, li.method) for li in db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id).all()]
    assert len(seen) == len(set(seen))


# ---------------------------------------------------------------------------------------
# C. Metamorphic properties — must hold for ANY dataset

def test_scaling_every_quantity_scales_the_total_proportionally(db):
    """Linearity: doubling consumption must exactly double emissions."""
    org = _org(db)
    _build(db, org)
    base = compute_co2e(db, org.id, gwp_set="AR6").total_co2e

    k = 2.5
    for a in db.query(ActivityRecord).filter(ActivityRecord.organisation_id == org.id).all():
        a.quantity = a.quantity * k
    db.commit()
    scaled = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
    assert scaled == pytest.approx(base * k, rel=1e-9)


def test_splitting_one_row_into_two_halves_leaves_the_total_unchanged(db):
    """Additivity: how the data is chunked must not change the answer."""
    org = _org(db)
    _build(db, org)
    before = compute_co2e(db, org.id, gwp_set="AR6").total_co2e

    victim = db.query(ActivityRecord).filter(
        ActivityRecord.organisation_id == org.id).order_by(ActivityRecord.id).first()
    half = victim.quantity / 2.0
    victim.quantity = half
    db.commit()
    _activity(db, org.id, db.get(EmissionFactor, victim.factor_id), half,
              category=victim.category, unit=victim.unit, date=victim.date)
    after = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
    assert after == pytest.approx(before, rel=1e-9)


def test_a_zero_quantity_row_changes_nothing(db):
    org = _org(db)
    _build(db, org)
    before = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
    f = db.query(EmissionFactor).filter(EmissionFactor.category == "electricity").first()
    _activity(db, org.id, f, 0.0)
    after = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
    assert after == pytest.approx(before, rel=1e-12)


def test_row_order_does_not_change_the_total(db):
    """Order independence — the engine allocates Scope 2 instruments in row order, so an
    order-sensitive total would be a real reproducibility defect."""
    org_a, org_b = _org(db, "OrderA"), _org(db, "OrderB")
    rows = [(c, u, v, q) for c, u, v, qs in _DATASET for q in qs]

    for cat, unit, val, q in rows:                       # forward
        _activity(db, org_a.id, _factor(db, cat, unit, val), q)
    for cat, unit, val, q in reversed(rows):             # reverse
        _activity(db, org_b.id, _factor(db, cat, unit, val), q)

    a = compute_co2e(db, org_a.id, gwp_set="AR6").total_co2e
    b = compute_co2e(db, org_b.id, gwp_set="AR6").total_co2e
    assert a == pytest.approx(b, rel=1e-9)


def test_emissions_are_monotonic_in_quantity(db):
    """More consumption can never mean fewer emissions."""
    org = _org(db)
    f = _factor(db, "electricity", "kWh", 0.17)
    a = _activity(db, org.id, f, 100.0)
    totals = []
    for q in (100.0, 250.0, 1000.0, 5000.0):
        a.quantity = q; db.commit()
        totals.append(compute_co2e(db, org.id, gwp_set="AR6").total_co2e)
    assert totals == sorted(totals)
    assert all(t > 0 for t in totals)


def test_recomputing_the_same_data_gives_the_same_number(db):
    """Determinism: two runs over identical inputs must agree exactly."""
    org = _org(db)
    _build(db, org)
    first = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
    second = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
    assert first == second


# ---------------------------------------------------------------------------------------
# D. Cross-renderer agreement — one run, one truth

def _all_renderers(db, org_id, run_id):
    """Every renderer that restates one run, keyed for cross-renderer comparison."""
    from app.reports.secr import secr_report
    from app.reports.sb253 import sb253_report
    from app.reports.esrs_e1 import esrs_e1_report
    from app.reports.issb_s2 import issb_s2_report
    from app.reports.gri import gri_report
    from app.reports.cdp import cdp_export
    from app.reports.ecovadis import ecovadis_readiness

    return {
        "secr": secr_report(db, org_id, run_id=run_id, intensity_denominator=1.0,
                            intensity_denominator_unit="unit"),
        "sb253": sb253_report(db, org_id, run_id=run_id, assurance_level="limited"),
        "esrs": esrs_e1_report(db, org_id, run_id=run_id, net_revenue_millions=1.0),
        "issb": issb_s2_report(db, org_id, run_id=run_id),
        "gri": gri_report(db, org_id, run_id=run_id, intensity_denominator=1.0,
                          intensity_denominator_unit="unit"),
        "cdp": cdp_export(db, org_id, run_id=run_id, intensity_denominator=1.0,
                          intensity_denominator_unit="unit"),
        "ecovadis": ecovadis_readiness(db, org_id, run_id=run_id,
                                       intensity_denominator=1.0),
    }


def _financed_position(db, org_id, investee_tco2e):
    """A PCAF position attributed 1:1, so the frozen financed figure is exactly its
    investee emissions. Goes through the real freeze path (RunFinancedLine rows), which
    is what the Cat 15 gate reads — setting run.financed_co2e by hand would not."""
    db.add(FinancedPosition(
        organisation_id=org_id, investee_name="Acme", asset_class="listed_equity",
        currency="GBP", outstanding_amount=100.0, attribution_denominator=100.0,
        investee_scope1_tco2e=investee_tco2e, investee_scope2_tco2e=0.0,
        data_quality_score=2, as_of_date="2025-01-01"))
    db.commit()


def _agree(name, named_values):
    """Assert every renderer reports the same value, naming the culprits when they don't."""
    vals = list(named_values.values())
    assert all(v == pytest.approx(vals[0], rel=1e-9) for v in vals), \
        f"{name} disagrees across renderers: {named_values}"


def test_all_renderers_report_the_same_scope_totals(db):
    """SECR / SB 253 / ESRS / ISSB / GRI / CDP / EcoVadis each restate the same inventory.
    If any two disagree, at least one published disclosure is wrong.

    Covers, beyond the scope figures: the financed-emissions treatment behind
    `total_location_based` and `scope3` (the three frameworks whose scope includes GHGP
    Cat 15 must agree, and the two that exclude it must SAY so), and the kWh behind
    "energy consumption" (GRI 302-1 and the EcoVadis Results KPI). Each of those was a
    real divergence: two renderers publishing different numbers under one field name."""
    org = _org(db)
    _build(db, org)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    r = _all_renderers(db, org.id, run.id)
    secr_e = r["secr"]["emissions_tco2e"]
    sb_e = r["sb253"]["emissions_tco2e"]
    esrs_e = r["esrs"]["e1_6_gross_ghg_emissions_tco2e"]
    issb_e = r["issb"]["ghg_emissions_tco2e"]

    _agree("scope 1", {
        "secr": secr_e["scope1"],
        "sb253": sb_e["scope1"],
        "esrs": esrs_e["scope1"],
        "issb": issb_e["scope1_gross"],
        "gri": r["gri"]["gri_305_1_scope1"]["gross_tco2e"],
        "cdp": r["cdp"]["answers"]["C6.1_scope1_gross_tco2e"],
        "ecovadis": r["ecovadis"]["kpis"]["scope1_tco2e"],
    })
    _agree("scope 2 (location)", {
        "secr": secr_e["scope2_location_based"],
        "sb253": sb_e["scope2_location_based"],
        "esrs": esrs_e["scope2_location_based"],
        "issb": issb_e["scope2_location_based_gross"],
        "gri": r["gri"]["gri_305_2_scope2"]["location_based_tco2e"],
        "cdp": r["cdp"]["answers"]["C6.3_scope2_location_tco2e"],
        "ecovadis": r["ecovadis"]["kpis"]["scope2_location_tco2e"],
    })
    _agree("scope 2 (market)", {
        "secr": secr_e["scope2_market_based"],
        "sb253": sb_e["scope2_market_based"],
        "esrs": esrs_e["scope2_market_based"],
        "issb": issb_e["scope2_market_based_information"],
        "gri": r["gri"]["gri_305_2_scope2"]["market_based_tco2e"],
        "cdp": r["cdp"]["answers"]["C6.3_scope2_market_tco2e"],
        "ecovadis": r["ecovadis"]["kpis"]["scope2_market_tco2e"],
    })
    # Activity-derived Scope 3: every renderer, on whichever field it reports it under.
    _agree("scope 3 excl. financed", {
        "secr": secr_e["scope3_voluntary"],
        "sb253": sb_e["scope3_excl_financed"],
        "esrs": esrs_e["scope3_excl_financed"],
        "issb": issb_e["scope3_gross_excl_financed"],
        "gri": r["gri"]["gri_305_3_scope3"]["gross_tco2e"],
        "cdp": r["cdp"]["answers"]["C6.5_scope3_excl_financed_tco2e"],
        "ecovadis": r["ecovadis"]["kpis"]["scope3_tco2e"],
    })
    # `total_location_based` means ONE thing across the renderers that publish that name.
    _agree("total (location-based)", {
        "sb253": sb_e["total_location_based"],
        "esrs": esrs_e["total_location_based"],
        "issb": issb_e["total_location_based"],
        # SECR excludes financed emissions by design, so it agrees only with the
        # excl-financed line — and must flag the exclusion (asserted below).
        "secr": secr_e["total_location_based"],
    })
    _agree("total (market-based)", {
        "sb253": sb_e["total_market_based"],
        "esrs": esrs_e["total_market_based"],
        "issb": issb_e["total_market_based"],
        "secr": secr_e["total_market_based"],
    })
    # "Energy consumption" for one run is ONE kWh figure, on one basis.
    _agree("energy (kWh, own operations, consolidated)", {
        "gri": r["gri"]["gri_302_1_energy"]["total_mwh"] * 1000.0,
        "esrs": r["esrs"]["e1_5_energy_consumption"]["total_mwh"] * 1000.0,
        "ecovadis": r["ecovadis"]["kpis"]["energy_kwh"],
    })
    _agree("energy basis", {
        "gri": r["gri"]["gri_302_1_energy"]["basis"],
        "ecovadis": r["ecovadis"]["kpis"]["energy_basis"],
    })


def test_financed_emissions_are_in_or_flagged_out_of_every_total(db):
    """Cat 15 financed emissions must be either INSIDE a renderer's Scope 3 and totals
    (ESRS ¶51-52, IFRS S2 ¶B58-B63, SB 253 via § 38532's GHG Protocol reference) or
    flagged as excluded (SECR, GRI 305-3). SB 253 used to do neither: it omitted them
    from `scope3` and `total_location_based` with no flag, while ISSB S2 put them INTO
    the identically-named fields for the same run."""
    org = _org(db)
    _build(db, org)
    financed_t = 12.345
    _financed_position(db, org.id, financed_t)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    assert run.financed_co2e == pytest.approx(financed_t * 1000.0)

    r = _all_renderers(db, org.id, run.id)
    sb_e = r["sb253"]["emissions_tco2e"]
    esrs_e = r["esrs"]["e1_6_gross_ghg_emissions_tco2e"]
    issb_e = r["issb"]["ghg_emissions_tco2e"]

    # The three frameworks that include Cat 15 agree, field for field.
    _agree("scope 3 incl. financed", {"sb253": sb_e["scope3"], "esrs": esrs_e["scope3"],
                                      "issb": issb_e["scope3_gross"]})
    _agree("total (location) incl. financed",
           {"sb253": sb_e["total_location_based"], "esrs": esrs_e["total_location_based"],
            "issb": issb_e["total_location_based"]})
    _agree("total (market) incl. financed",
           {"sb253": sb_e["total_market_based"], "esrs": esrs_e["total_market_based"],
            "issb": issb_e["total_market_based"]})
    # ...and each is the activity-derived figure PLUS the financed one, not a coincidence.
    assert sb_e["scope3"] == pytest.approx(sb_e["scope3_excl_financed"] + financed_t)
    assert sb_e["total_location_based"] == pytest.approx(
        sb_e["total_location_based_excl_financed"] + financed_t)
    assert sb_e["scope3_cat15_financed"]["included_in_scope3_and_totals"] is True

    # The renderers that exclude Cat 15 must SAY so — an unflagged omission is the defect.
    assert r["secr"]["emissions_tco2e"]["financed_emissions_excluded"] is True
    assert r["gri"]["gri_305_3_scope3"]["financed_emissions_excluded"] is True
    assert r["gri"]["gri_305_3_scope3"]["financed_emissions_tco2e"] == \
        pytest.approx(financed_t)

    # IFRS S2's honest-scope statement must not contradict the numbers beside it.
    from app.reports.issb_s2 import NOT_COVERED
    assert not any("financed emissions" in n and "facilitated" not in n
                   for n in NOT_COVERED), NOT_COVERED


def test_no_renderer_publishes_a_double_counted_cat15_total(db):
    """When Cat 15 is declared through BOTH activity lines and a PCAF portfolio, summing
    them counts the same investee twice. Every renderer that adds financed emissions must
    refuse the sum (None) — not just the ones that happened to have the guard."""
    from tests.scope3_util import ready_run
    org = _org(db)
    _build(db, org)
    for a in db.query(ActivityRecord).filter(ActivityRecord.category == "waste").all():
        a.ghgp_category = 5                            # so only Cat 15 is contested
    # A Scope 3 activity carrying GHGP category 15 — the activity-derived Cat 15 line...
    inv = _activity(db, org.id, _factor(db, "investments", "kg", 1.5), 100.0)
    inv.scope, inv.ghgp_category = "3", 15
    db.commit()
    _financed_position(db, org.id, 9.0)                # ...and a PCAF portfolio too
    run, _period = ready_run(db, org.id)
    assert run.financed_co2e == pytest.approx(9000.0)

    r = _all_renderers(db, org.id, run.id)
    assert r["cdp"]["answers"]["C6.5_cat15_double_count_blocked"] is True
    assert r["cdp"]["answers"]["C6.5_scope3_tco2e"] is None
    for key, block, fields in (
            ("sb253", r["sb253"]["emissions_tco2e"],
             ("scope3", "total_location_based", "total_market_based")),
            ("esrs", r["esrs"]["e1_6_gross_ghg_emissions_tco2e"],
             ("scope3", "total_location_based", "total_market_based")),
            ("issb", r["issb"]["ghg_emissions_tco2e"],
             ("scope3_gross", "total_location_based", "total_market_based"))):
        for f in fields:
            assert block[f] is None, f"{key}.{f} published a double-counted sum: {block[f]}"
    # A refusal always travels with a blocker — never a silent hole in the payload.
    for key, ready in (("sb253", "filing_ready"), ("esrs", "disclosure_ready"),
                       ("issb", "disclosure_ready"), ("cdp", "submission_ready")):
        assert r[key][ready] is False
        assert any("Cat 15" in b for b in r[key]["blockers"]), r[key]["blockers"]
    # The excl-financed figures stay reportable: the inventory is not in doubt, the sum is.
    assert r["esrs"]["e1_6_gross_ghg_emissions_tco2e"][
        "total_location_based_excl_financed"] == round(run.total_co2e / 1000.0, 6)


def test_energy_notes_travel_with_the_energy_figure(db):
    """The incompleteness caveat and the diesel calorific-value assumption live in
    `_energy_kwh`'s notes. GRI 302-1/302-3 and the EcoVadis KPI published the numerator
    and dropped them, so a partial figure read as a complete one."""
    org = _org(db)
    _build(db, org)
    # An energy-bearing carrier OUTSIDE the three reported ones: its emissions are in
    # Scope 1, its energy is not in the kWh total.
    coal = _activity(db, org.id, _factor(db, "coal", "kg", 2.4), 500.0)
    coal.scope = "1"
    db.commit()
    run = compute_co2e(db, org.id, gwp_set="AR6")
    r = _all_renderers(db, org.id, run.id)

    for name, block in (("gri 302-1", r["gri"]["gri_302_1_energy"]),
                        ("gri 302-3", r["gri"]["gri_302_3_energy_intensity"])):
        assert block["complete"] is False, name
        assert "coal" in block["carriers_omitted"], name
        assert any("NOT in this kWh total" in n for n in block["notes"]), name
        assert any("kWh/L" in n for n in block["notes"]), f"{name} dropped the diesel CV note"
        assert block["basis"] == "consolidated_entity_share"

    k = r["ecovadis"]["kpis"]
    assert k["energy_complete"] is False
    assert "coal" in k["energy_carriers_omitted"]
    assert any("kWh/L" in n for n in k["energy_notes"])
    assert any("INCOMPLETE" in g for g in r["ecovadis"]["all_gaps"])


def test_renderer_totals_match_the_run_and_the_oracle(db):
    """The disclosed tonnage must trace back to the immutable run AND to independent
    arithmetic — closing the loop from raw data to published figure."""
    from app.reports.secr import secr_report
    org = _org(db)
    oracle_kg = _build(db, org)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    secr = secr_report(db, org.id, run_id=run.id, intensity_denominator=1.0,
                       intensity_denominator_unit="unit")
    disclosed_t = secr["emissions_tco2e"]["total_location_based"]
    # Renderers publish tonnes rounded to 6dp (= 1 gram). Assert EXACT equality against that
    # rounding rather than a loose tolerance, so a real drift cannot hide inside the epsilon.
    assert disclosed_t == round(run.total_co2e / 1000.0, 6)
    assert disclosed_t == round(oracle_kg / 1000.0, 6)


def test_scope_split_across_renderers_sums_to_the_disclosed_total(db):
    """Each renderer must be internally consistent too: its own scopes must re-sum to its
    own total."""
    from app.reports.secr import secr_report
    org = _org(db)
    _build(db, org)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    e = secr_report(db, org.id, run_id=run.id, intensity_denominator=1.0,
                    intensity_denominator_unit="unit")["emissions_tco2e"]
    assert (e["scope1"] + e["scope2_location_based"] + e["scope3_voluntary"]) == \
        pytest.approx(e["total_location_based"], rel=1e-9)
    assert (e["scope1"] + e["scope2_location_based"]) == \
        pytest.approx(e["scope1_and_2_location"], rel=1e-9)


def test_rounding_never_moves_a_figure_by_more_than_half_a_gram(db):
    """Disclosure precision contract: tonnes are published to 6dp, so the published figure may
    differ from the underlying kilogram total by at most half a unit in the last place
    (5e-7 t = 0.5 mg). Anything larger would be a calculation error wearing a rounding mask."""
    from app.reports.secr import secr_report
    org = _org(db)
    _build(db, org)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    e = secr_report(db, org.id, run_id=run.id, intensity_denominator=1.0,
                    intensity_denominator_unit="unit")["emissions_tco2e"]
    assert abs(e["total_location_based"] - run.total_co2e / 1000.0) <= 5e-7


def test_rounded_scope_figures_still_resum_within_rounding_tolerance(db):
    """Independent rounding of each scope must not let the parts drift from the whole by more
    than the rounding of the parts themselves (3 scopes x 0.5 ulp)."""
    from app.reports.secr import secr_report
    org = _org(db)
    _build(db, org)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    e = secr_report(db, org.id, run_id=run.id, intensity_denominator=1.0,
                    intensity_denominator_unit="unit")["emissions_tco2e"]
    parts = e["scope1"] + e["scope2_location_based"] + e["scope3_voluntary"]
    assert abs(parts - e["total_location_based"]) <= 4 * 5e-7


# ---------------------------------------------------------------------------------------
# E. Unit conversion — verified against hand-computed factors, then round-tripped

@pytest.mark.parametrize("qty,unit,factor_unit,factor_value,expected", [
    (1.0,    "MWh", "kWh", 0.17,  170.0),        # 1 MWh = 1000 kWh
    (2.5,    "t",   "kg",  0.5,   1250.0),       # 1 t = 1000 kg
    (1000.0, "kg",  "t",   500.0, 500.0),        # inverse direction
    (100.0,  "km",  "km",  0.171, 17.1),         # identity
])
def test_unit_conversion_produces_hand_computed_values(db, qty, unit, factor_unit,
                                                       factor_value, expected):
    org = _org(db)
    f = _factor(db, "electricity" if factor_unit == "kWh" else "material",
                factor_unit, factor_value)
    _activity(db, org.id, f, qty, unit=unit)
    run = compute_co2e(db, org.id, gwp_set="AR6")
    assert run.total_co2e == pytest.approx(expected, rel=1e-9)


def test_equivalent_quantities_in_different_units_give_the_same_answer(db):
    """1 MWh and 1000 kWh are the same physical quantity — they must produce the same number."""
    org_a, org_b = _org(db, "UnitA"), _org(db, "UnitB")
    fa = _factor(db, "electricity", "kWh", 0.17)
    fb = _factor(db, "electricity", "kWh", 0.17)
    _activity(db, org_a.id, fa, 1.0, unit="MWh")
    _activity(db, org_b.id, fb, 1000.0, unit="kWh")
    a = compute_co2e(db, org_a.id, gwp_set="AR6").total_co2e
    b = compute_co2e(db, org_b.id, gwp_set="AR6").total_co2e
    assert a == pytest.approx(b, rel=1e-12)
