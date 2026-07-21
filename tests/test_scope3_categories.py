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
