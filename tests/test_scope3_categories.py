"""GHG Protocol Scope 3 15-category dimension + completeness gate.

The behaviour this whole change exists to produce: a firm uploading only
electricity/gas/flights can no longer read as a complete Scope 3 inventory, and
a run's category breakdown + exclusion statement reproduce from frozen state.
"""
import json
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
import app.models  # noqa: F401
from app.models import (
    Organisation, ActivityRecord, EmissionFactor, ReportingPeriod,
    Scope3CategoryDeclaration, RunScope3Declaration,
)
from app.services.calc import compute_co2e
from app.services.ghgp import derive_ghgp_category, scope3_completeness
from app.reports.scope3 import scope3_by_ghgp_category, scope3_inventory_report
from app.reports.summary import summary
from app import main as main_mod
from tests.scope3_util import make_period, screen_complete, ready_run


def _org(db, name="Co"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category, unit="kWh", value=0.2, lca_boundary=None):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category=category, subcategory="", unit=unit, gwp_set="AR6",
                       value=value, lca_boundary=lca_boundary)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _act(db, org_id, factor_id, category, quantity=100.0, unit="kWh",
         scope=None, ghgp_category=None):
    a = ActivityRecord(organisation_id=org_id, date="2025-03-01", category=category,
                       subcategory="", description="", quantity=quantity, unit=unit,
                       geo="GB", factor_id=factor_id, scope=scope, ghgp_category=ghgp_category)
    db.add(a); db.commit(); db.refresh(a)
    return a


# --- Derivation ---------------------------------------------------------------

def test_derivation_rules():
    assert derive_ghgp_category("3", "flight", None) == (6, "category_rule", None)
    assert derive_ghgp_category("3", "business_travel", None) == (6, "category_rule", None)
    assert derive_ghgp_category("3", "commuting", None) == (7, "category_rule", None)
    # ambiguous -> unassigned WITH candidates, never a guess
    assert derive_ghgp_category("3", "waste", None) == (None, "ambiguous_unassigned", [5, 12])
    # unknown free text -> unassigned
    assert derive_ghgp_category("3", "mystery", None) == (None, "unassigned", None)
    # explicit wins
    assert derive_ghgp_category("3", "waste", 12) == (12, "explicit", None)
    # explicit out of range -> invalid (blocks), never clamped
    assert derive_ghgp_category("3", "waste", 99)[1] == "invalid_explicit"
    # a category on a non-Scope-3 line is a contradiction
    assert derive_ghgp_category("1", "gas", 6)[1] == "conflict_non_scope3"
    assert derive_ghgp_category("1", "gas", None) == (None, "n/a_scope1", None)


def test_derivation_is_frozen_into_the_line(db):
    org = _org(db)
    _act(db, org.id, _factor(db, "flight", "pkm", 0.15).id, "flight", 1000, "pkm")
    run = compute_co2e(db, org.id)
    inv = scope3_by_ghgp_category(db, run)
    assert inv["categories"]["6"]["line_count"] == 1
    assert inv["categories"]["6"]["name"] == "Business travel"


# --- Freezing / reproduction --------------------------------------------------

def test_every_run_freezes_exactly_15_declarations(db):
    org = _org(db)
    _act(db, org.id, _factor(db, "electricity").id, "electricity", 100)
    run = compute_co2e(db, org.id)
    rows = db.query(RunScope3Declaration).filter(
        RunScope3Declaration.run_id == run.id).all()
    assert len(rows) == 15
    assert all(r.status == "undeclared" for r in rows)   # nothing screened yet


def test_breakdown_reproduces_after_activity_is_remapped(db):
    """Re-mapping an activity after the run must not change the run's frozen breakdown."""
    org = _org(db)
    a = _act(db, org.id, _factor(db, "flight", "pkm", 0.15).id, "flight", 1000, "pkm")
    run = compute_co2e(db, org.id)
    before = scope3_by_ghgp_category(db, run)["categories"]["6"]["tco2e"]
    a.ghgp_category = 9            # re-categorise the LIVE activity
    a.category = "freight"
    db.commit()
    after = scope3_by_ghgp_category(db, run)["categories"]["6"]["tco2e"]
    assert after == before        # the FROZEN run is unchanged
    assert scope3_by_ghgp_category(db, run)["categories"]["9"]["line_count"] == 0


def test_legacy_run_is_not_rendered_as_complete(db):
    org = _org(db)
    _act(db, org.id, _factor(db, "flight", "pkm", 0.15).id, "flight", 1000, "pkm")
    run = compute_co2e(db, org.id)
    run.ghgp_standard_version = None      # simulate a pre-dimension run
    db.commit()
    inv = scope3_by_ghgp_category(db, run)
    assert inv["assessable"] is False
    assert scope3_completeness(db, run)["assessable"] is False


# --- The gate: what blocks ----------------------------------------------------

def test_three_of_fifteen_no_longer_reads_as_complete(db):
    """The headline finding: electricity+gas+flight is NOT a complete Scope 3 inventory."""
    org = _org(db)
    p = make_period(db, org.id)
    _act(db, org.id, _factor(db, "electricity").id, "electricity", 1000)
    _act(db, org.id, _factor(db, "gas").id, "gas", 1000)
    _act(db, org.id, _factor(db, "flight", "pkm", 0.15).id, "flight", 1000, "pkm")
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    inv = summary(db, organisation_id=org.id, run_id=run.id)["coverage"]["inventory_coverage"]
    assert inv["inventory_coverage_pct"] < 100.0
    assert set(inv["categories_undeclared"]) >= {1, 2, 3, 4, 5}
    gate = scope3_completeness(db, run)
    assert any("UNDECLARED" in b for b in gate["blockers"])


def test_fully_screened_run_is_ready(db):
    org = _org(db)
    _act(db, org.id, _factor(db, "electricity").id, "electricity", 1000)
    _act(db, org.id, _factor(db, "flight", "pkm", 0.15).id, "flight", 1000, "pkm")
    run, _p = ready_run(db, org.id)
    gate = scope3_completeness(db, run)
    assert gate["blockers"] == []
    assert gate["inventory_coverage_pct"] == 100.0


def test_not_measured_blocks_as_a_known_gap(db):
    org = _org(db)
    _act(db, org.id, _factor(db, "electricity").id, "electricity", 1000)
    run, p = ready_run(db, org.id)
    assert scope3_completeness(db, run)["blockers"] == []
    # flip one category to not_measured, recompute
    d = db.query(Scope3CategoryDeclaration).filter_by(
        organisation_id=org.id, reporting_period_id=p.id, category=11).first()
    d.status, d.justification = "not_measured", \
        "Use-of-sold-products data collection is planned for next year."
    db.commit()
    run2 = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert any("NOT MEASURED" in b for b in scope3_completeness(db, run2)["blockers"])


def test_boilerplate_justification_blocks(db):
    org = _org(db)
    p = make_period(db, org.id)
    _act(db, org.id, _factor(db, "electricity").id, "electricity", 1000)
    db.add(Scope3CategoryDeclaration(organisation_id=org.id, reporting_period_id=p.id,
           category=1, status="not_applicable", justification="n/a",
           screened_at="2025-06-30", updated_at="2025-06-30"))
    db.commit()
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert any("boilerplate" in b for b in scope3_completeness(db, run)["blockers"])


def test_unassigned_scope3_line_blocks(db):
    org = _org(db)
    _act(db, org.id, _factor(db, "waste", "kg", 0.5).id, "waste", 100, "kg")  # ambiguous
    run, _p = ready_run(db, org.id)   # screen_complete can't include an unassigned line
    gate = scope3_completeness(db, run)
    assert any("carry no GHGP category" in b for b in gate["blockers"])
    assert gate["unassigned_sources"].get("ambiguous_unassigned") == 1


# --- Table 5.4 minimum-boundary check (B12/W1) now has teeth (factor boundary backfill) ---

def test_boundaryless_factor_warns_not_assessable_w1(db):
    """Baseline: a Cat 5 line whose factor carries NO lca_boundary can't be checked
    against Table 5.4 — the gate warns (W1), it must never silently pass."""
    org = _org(db)
    _act(db, org.id, _factor(db, "waste", "kg", 0.5, lca_boundary=None).id,
         "waste", 100, "kg", ghgp_category=5)
    run, _p = ready_run(db, org.id)
    gate = scope3_completeness(db, run)
    assert gate["blockers"] == []
    assert any("NOT ASSESSABLE" in w and "category 5" in w for w in gate["warnings"])


def test_backfilled_boundary_clears_the_w1_warning(db):
    """The point of the DEFRA boundary backfill: a Cat 5 waste line whose factor now
    carries the derived `waste_treatment` boundary MEETS Table 5.4 — no W1, no B12."""
    org = _org(db)
    _act(db, org.id, _factor(db, "waste", "kg", 0.5, lca_boundary="waste_treatment").id,
         "waste", 100, "kg", ghgp_category=5)
    run, _p = ready_run(db, org.id)
    gate = scope3_completeness(db, run)
    assert gate["blockers"] == []
    assert not any("category 5" in w and "NOT ASSESSABLE" in w for w in gate["warnings"])


def test_below_minimum_boundary_blocks_b12(db):
    """A factor whose boundary is BELOW the category minimum is a partial figure, not a
    compliant Cat-5 number — B12 must block. (cradle_to_gate is not a waste-treatment
    boundary; teeth cut both ways.)"""
    org = _org(db)
    _act(db, org.id, _factor(db, "waste", "kg", 0.5, lca_boundary="cradle_to_gate").id,
         "waste", 100, "kg", ghgp_category=5)
    run, _p = ready_run(db, org.id)
    gate = scope3_completeness(db, run)
    assert any("category 5" in b and "minimum boundary" in b and "Table 5.4" in b
               for b in gate["blockers"])


def test_anti_gaming_cat3_cannot_be_not_applicable_with_energy(db):
    org = _org(db)
    p = make_period(db, org.id)
    _act(db, org.id, _factor(db, "electricity").id, "electricity", 1000)
    db.add(Scope3CategoryDeclaration(organisation_id=org.id, reporting_period_id=p.id,
           category=3, status="not_applicable",
           justification="We believe upstream fuel emissions do not apply to us here.",
           screened_at="2025-06-30", updated_at="2025-06-30"))
    db.commit()
    run = compute_co2e(db, org.id, reporting_period_id=p.id)
    assert any("category 3" in b and "NOT APPLICABLE" in b
               for b in scope3_completeness(db, run)["blockers"])


def test_editing_the_screen_after_the_run_is_detected(db):
    org = _org(db)
    _act(db, org.id, _factor(db, "electricity").id, "electricity", 1000)
    run, p = ready_run(db, org.id)
    assert scope3_completeness(db, run)["blockers"] == []
    # tamper with the live screen without recomputing
    d = db.query(Scope3CategoryDeclaration).filter_by(
        organisation_id=org.id, reporting_period_id=p.id, category=1).first()
    d.status = "included"
    d.method_description = "Retroactively changed after filing."
    db.commit()
    assert any("EDITED since this run" in b for b in scope3_completeness(db, run)["blockers"])


def test_org_wide_run_can_never_be_ready(db):
    org = _org(db)
    _act(db, org.id, _factor(db, "electricity").id, "electricity", 1000)
    run = compute_co2e(db, org.id)   # no reporting period
    assert any("not scoped to a reporting period" in b
               for b in scope3_completeness(db, run)["blockers"])


# --- API ----------------------------------------------------------------------

@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    def override():
        d = Session()
        try:
            yield d
        finally:
            d.close()
    main_mod.app.dependency_overrides[main_mod.get_db] = override
    c = TestClient(main_mod.app)
    key = c.post("/organisations", params={"name": "A"}).json()["api_key"]
    yield c, {"X-API-Key": key}, Session
    main_mod.app.dependency_overrides.clear()


def test_declaration_endpoint_enforces_evidence(client):
    c, hdr, _ = client
    pid = c.post("/reporting_periods", params={"label": "FY25", "start_date": "2025-01-01",
                                               "end_date": "2025-12-31"}, headers=hdr).json()["id"]
    base = {"reporting_period_id": pid, "category": 1}
    # excluding without a real justification is rejected at the boundary
    assert c.post("/scope3/declarations", params={**base, "status": "not_applicable",
                  "justification": "n/a"}, headers=hdr).status_code == 400
    # not_material without screening evidence is rejected
    assert c.post("/scope3/declarations", params={**base, "status": "not_material",
                  "justification": "Screened and found to be small relative to total."},
                  headers=hdr).status_code == 400
    # a proper exclusion is accepted
    ok = c.post("/scope3/declarations", params={**base, "status": "not_applicable",
                "justification": "The entity purchases no capital goods in the period."},
                headers=hdr)
    assert ok.status_code == 200


def test_bulk_assign_and_inventory_endpoint(client):
    c, hdr, Session = client
    # seed a waste (ambiguous) activity directly
    seed = Session()
    org = seed.query(Organisation).filter(Organisation.name == "A").one()
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024, category="waste",
                       subcategory="", unit="kg", gwp_set="AR6", value=0.5)
    seed.add(f); seed.commit(); seed.refresh(f)
    seed.add(ActivityRecord(organisation_id=org.id, date="2025-03-01", category="waste",
                            subcategory="", description="", quantity=100, unit="kg",
                            geo="GB", factor_id=f.id))
    seed.commit(); seed.close()
    # resolve the ambiguity by bulk-assigning waste -> Cat 5
    r = c.post("/activities/ghgp-categories",
               params={"category": "waste", "ghgp_category": 5}, headers=hdr).json()
    assert r["updated"] == 1
    c.post("/calculate/run", headers=hdr)
    inv = c.get("/reports/scope3_inventory", headers=hdr).json()
    assert inv["scope3"]["categories"]["5"]["line_count"] == 1
    # still not disclosure_ready — the other 14 categories are undeclared
    assert inv["disclosure_ready"] is False


# --- Table 5.4 acceptance vocabulary, versioned apart from the standard (s3bnd-v2) ---

def test_boundary_policy_v1_is_the_frozen_taxonomy_verbatim():
    """s3bnd-v1 must BE the token sets that shipped — it is the historical record, so
    replaying a pre-change run under it reproduces the same TOKEN-SET membership.
    (Blank-ish boundary strings are a deliberate, documented normalisation correction that
    sits outside the policy; see boundary_policy_for_run.) Proven at import, re-proven here
    against the PINNED cut, not the live standard version."""
    from app.services.ghgp import (BOUNDARY_POLICIES, GHGP_TAXONOMIES,
                                   BOUNDARY_POLICY_TAXONOMY)
    tax = GHGP_TAXONOMIES[BOUNDARY_POLICY_TAXONOMY["s3bnd-v1"]]
    for c in range(1, 16):
        t, v1 = tax[c]["accepts_boundary"], BOUNDARY_POLICIES["s3bnd-v1"][c]
        assert (t is None) == (v1 is None)
        assert t is None or set(t) == set(v1)


def test_v2_is_a_monotone_broadening_so_no_filed_line_gains_a_blocker():
    from app.services.ghgp import BOUNDARY_POLICIES
    for c in range(1, 16):
        v1, v2 = BOUNDARY_POLICIES["s3bnd-v1"][c], BOUNDARY_POLICIES["s3bnd-v2"][c]
        assert (v1 is None) == (v2 is None)
        assert v1 is None or set(v1) <= set(v2)


def test_v2_fixes_the_scope12_family_asymmetry():
    """The defect: identical '<party> scope 1 and 2' bars accepted different direct-emission
    tokens, so a scope-agnostic factor false-blocked on the 'other' family."""
    from app.services.ghgp import boundary_meets_minimum as m
    # A stationary combustion factor on a leased asset / franchise (was False under v1).
    for cat in (8, 13, 14, 10):
        assert m(cat, "combustion") is True
    # A grid-electricity factor on travel/commuting/freight (was False under v1).
    for cat in (4, 6, 7, 9, 10):
        assert m(cat, "generation") is True
    # Every scope1_2-family category now accepts the same direct-operational tier.
    for cat in (4, 5, 6, 7, 8, 9, 10, 12, 13, 14):
        for tok in ("ttw", "tank_to_wheel", "combustion", "generation", "wtw", "scope1_2"):
            assert m(cat, tok) is True, (cat, tok)
    # ...and a factor labelled with the category's own declared minimum passes tautologically.
    from app.services.ghgp import taxonomy
    for cat in (4, 5, 6, 7, 8, 9, 10, 12, 13, 14):
        assert m(cat, taxonomy()[cat]["min_boundary"]) is True


def test_v2_never_admits_an_upstream_only_token_the_false_pass_guard():
    """The understatement direction. An upstream-only factor must NEVER satisfy a
    scope1_2-family category, and Cat 3's upstream bar must never accept operational
    tokens or the catalogue's most common token (cradle_to_gate)."""
    from app.services.ghgp import boundary_meets_minimum as m
    for cat in (4, 5, 6, 7, 8, 9, 10, 12, 13, 14):
        for tok in ("well_to_tank", "wtt", "td_loss", "cradle_to_gate"):
            assert m(cat, tok) is False, (cat, tok)
    for tok in ("combustion", "generation", "ttw", "scope1_2", "cradle_to_gate"):
        assert m(3, tok) is False, tok
    # Cat 1/2 keep the higher cradle bar.
    for tok in ("combustion", "generation", "ttw", "waste_treatment"):
        assert m(1, tok) is False and m(2, tok) is False


def test_verdict_basis_separates_a_fixable_gap_from_an_inherent_one():
    from app.services.ghgp import boundary_verdict
    assert boundary_verdict(6, "ttw")[1] == "accepted"
    assert boundary_verdict(6, "wtt")[1] == "below_minimum"
    assert boundary_verdict(6, None)[1] == "no_boundary_on_factor"
    # Cat 11/15 are not assessable from a factor boundary AT ALL — reported as the
    # inherent limit even when the factor does carry a boundary (ordering matters).
    assert boundary_verdict(11, "combustion")[1] == "not_assessable_by_category"
    assert boundary_verdict(15, None)[1] == "not_assessable_by_category"
    # The normalised token is returned for the record.
    assert boundary_verdict(6, "  TTW ")[2] == "ttw"
    assert boundary_verdict(6, "  ")[2] is None


def test_policy_resolved_against_another_taxonomy_cut_fails_closed():
    """A policy is written against ONE category cut; resolving it against a different one
    is an unverified claim -> not assessable (W1), never True."""
    from app.services.ghgp import boundary_meets_minimum
    assert boundary_meets_minimum(6, "ttw", None, "ghgp-scope3-2099") is None


def test_run_freezes_the_policy_version_and_the_token_it_judged(db):
    """The verdict was already frozen; its INPUT was not, so an assurer could not
    re-derive it without joining the live factor table (which the contract forbids)."""
    org = _org(db)
    _act(db, org.id, _factor(db, "waste", "kg", 0.5, lca_boundary="waste_treatment").id,
         "waste", 100, "kg", ghgp_category=5)
    run, _p = ready_run(db, org.id)
    assert run.ghgp_boundary_policy_version == "s3bnd-v2"
    from app.models import EmissionLineItem
    d = json.loads(db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id, EmissionLineItem.scope == "3").first().details)
    assert d["ghgp_boundary_policy_version"] == "s3bnd-v2"
    assert d["ghgp_boundary_token"] == "waste_treatment"
    assert d["ghgp_boundary_verdict_basis"] == "accepted"
    assert d["ghgp_min_boundary_met"] is True
    # Surfaced on the frozen artifact, not inferred.
    inv = scope3_by_ghgp_category(db, run)
    assert inv["boundary_policy_version"] == "s3bnd-v2"
    assert inv["boundary_policy_version_inferred"] is False


def test_legacy_run_infers_v1_without_rewriting_history(db):
    from app.services.ghgp import boundary_policy_for_run
    org = _org(db)
    _act(db, org.id, _factor(db, "electricity").id, "electricity", 1000)
    run, _p = ready_run(db, org.id)
    run.ghgp_boundary_policy_version = None          # a run frozen before the policy existed
    db.commit()
    version, inferred = boundary_policy_for_run(run)
    assert (version, inferred) == ("s3bnd-v1", True)
    assert run.ghgp_boundary_policy_version is None  # never back-filled into history


def test_adding_a_taxonomy_version_cannot_rewrite_a_filed_policy(monkeypatch):
    """Regression (adversarial review, MEDIUM): the policy composed its token sets from the
    LIVE GHGP_STANDARD_VERSION, so performing the append-only extension the module itself
    prescribes — adding a taxonomy cut and bumping the standard version — silently rewrote
    the already-filed s3bnd-v2 (or hard-failed import). Policies are now pinned to the cut
    they were authored against, so a new cut cannot touch them."""
    import importlib
    import app.services.ghgp as g
    before = {c: g.BOUNDARY_POLICIES["s3bnd-v2"][c] for c in range(1, 16)}

    # Append a new taxonomy cut that relabels a min_boundary, and bump the live pointer —
    # exactly the extension the append-only doctrine invites.
    new_cut = {c: dict(v) for c, v in g.GHGP_TAXONOMIES["ghgp-scope3-2011"].items()}
    new_cut[8] = dict(new_cut[8], min_boundary="lessor_scope_1_and_2")
    monkeypatch.setitem(g.GHGP_TAXONOMIES, "ghgp-scope3-2099", new_cut)
    monkeypatch.setattr(g, "GHGP_STANDARD_VERSION", "ghgp-scope3-2099")

    # Re-composing under the bumped pointer must leave the filed policy untouched...
    rebuilt = {c: (g._opset(c, g.BOUNDARY_POLICY_TAXONOMY["s3bnd-v2"])
                   if c in g._SCOPE12_FAMILY_CATS else g.BOUNDARY_POLICIES["s3bnd-v1"][c])
               for c in range(1, 16)}
    assert rebuilt == before
    # ...and Cat 8 must not have silently LOST the minimum it accepted at filing time.
    assert "lessor_scope1_2" in g.BOUNDARY_POLICIES["s3bnd-v2"][8]
    assert g.boundary_meets_minimum(8, "lessor_scope1_2", "s3bnd-v2") is True
    # The module still imports cleanly under the new cut (the v1 proof is pinned too).
    importlib.reload(g)


def test_blank_boundary_is_absent_not_a_token_that_matches_nothing():
    """Regression (adversarial review, LOW): a whitespace-only lca_boundary used to compare
    as a token matching nothing (verdict False -> B12 blocker). It now normalises to absent
    (None -> W1), which is the correct reading and only ever loosens — never a false pass."""
    from app.services.ghgp import boundary_verdict
    for blank in ("", "   ", "\t", None):
        met, basis, token = boundary_verdict(6, blank)
        assert met is None and basis == "no_boundary_on_factor" and token is None


def test_generic_loader_strips_token_fields_so_blanks_never_reach_the_gate():
    from app.ef_catalog.loaders.generic import parse_generic_csv
    csv = (b"category,subcategory,unit,value,lca_boundary,ch4_origin,price_basis\n"
           b"electricity,,kWh,0.17,   ,  ,  \n")
    row = parse_generic_csv(csv)[0]
    assert row.lca_boundary is None and row.ch4_origin is None and row.price_basis is None


def test_policy_integrity_proofs_survive_python_O():
    """The drift/monotonicity/false-pass proofs must not be `assert` — `python -O` strips
    assert statements, which would silently remove them in an optimised deployment."""
    import subprocess, sys
    src = ("import app.services.ghgp as g\n"
           "g._policy_check(False, 'canary')\n")
    r = subprocess.run([sys.executable, "-O", "-c", src], capture_output=True, text=True)
    assert r.returncode != 0 and "boundary policy integrity violated" in r.stderr
