"""ISO 14025 / EN 15804 EPD renderer — the GWP indicator in module form.

Honest by construction: the quantitative core a verifier would check, GWP only, never a
verified declaration. Same fail-closed gate doctrine as every other renderer.
"""
import pytest

from app.models import Organisation, EmissionFactor, LcaAssessment, LcaItem
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
from app import main as main_mod
from app.reports.epd import epd_report


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
    key = c.post("/organisations", params={"name": "EPDCo"}).json()["api_key"]
    yield c, {"X-API-Key": key}, Session
    main_mod.app.dependency_overrides.clear()


def _org(db, name="Maker"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, value, biogenic=None):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="material", subcategory="", unit="kg", gwp_set="AR6",
                       value=value, kg_co2_biogenic=biogenic)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _assess(db, org_id, standard="en_15804", fu="1 m2 GFA", fu_qty=100.0):
    a = LcaAssessment(organisation_id=org_id, name="Concrete panel", standard=standard,
                      functional_unit=fu, functional_unit_quantity=fu_qty, gwp_set="AR6")
    db.add(a); db.commit(); db.refresh(a)
    return a


def _item(db, aid, stage, qty, unit, factor_id, alloc=1.0):
    it = LcaItem(assessment_id=aid, stage=stage, quantity=qty, unit=unit,
                 factor_id=factor_id, allocation_factor=alloc)
    db.add(it); db.commit(); db.refresh(it)
    return it


def _full_epd_assessment(db):
    org = _org(db)
    a = _assess(db, org.id, "en_15804", fu="1 m2 GFA", fu_qty=100.0)
    f = _factor(db, 1.0).id
    _item(db, a.id, "A1-A3", 6000, "kg", f)      # 6000 — product stage
    _item(db, a.id, "A4", 400, "kg", f)          # 400  — transport to site
    _item(db, a.id, "C3", 2000, "kg", f)         # 2000 — end of life
    _item(db, a.id, "D", -1500, "kg", f)         # -1500 — beyond boundary (credit)
    return org, a


# --- The declaration ----------------------------------------------------------------

def test_a_full_en15804_assessment_declares_by_module(db):
    org, a = _full_epd_assessment(db)
    r = epd_report(db, org.id, a.id, pcr_reference="EN 16757 (concrete)")
    assert r["disclosure_ready"] is True
    m = r["gwp_fossil_by_module_kg"]
    assert m["A1-A3"]["gwp_fossil_kg"] == pytest.approx(6000.0)
    assert m["A1-A3"]["gwp_fossil_per_unit_kg"] == pytest.approx(60.0)   # /100 m2
    assert m["A4"]["gwp_fossil_kg"] == pytest.approx(400.0)
    assert m["C3"]["gwp_fossil_kg"] == pytest.approx(2000.0)
    # declared A-C total EXCLUDES module D (never netted in)
    assert r["declared_modules_gwp_fossil_kg"] == pytest.approx(8400.0)
    assert r["module_D_beyond_boundary_kg"] == pytest.approx(-1500.0)
    assert r["declaration"]["declared_unit"] == "1 m2 GFA"
    assert r["declaration"]["pcr_reference"] == "EN 16757 (concrete)"


def test_undeclared_modules_are_MND_not_absent(db):
    """An assurer must see the WHOLE life cycle: modules the assessment did not cover are
    a first-class 'module not declared', not a missing row."""
    org, a = _full_epd_assessment(db)
    r = epd_report(db, org.id, a.id, pcr_reference="EN 16757")
    m = r["gwp_fossil_by_module_kg"]
    assert m["B4"]["declared"] is False and m["B4"]["status"] == "MND"
    assert m["B4"]["gwp_fossil_kg"] is None
    # every EN module in declaration order is present as a row
    assert set(m) >= {"A1-A3", "A4", "A5", "B1", "C1", "C4"}


def test_biogenic_co2_is_reported_separately(db):
    org = _org(db)
    a = _assess(db, org.id, "en_15804")
    f = _factor(db, 0.5, biogenic=1.6).id            # timber: fossil 0.5, biogenic 1.6 / kg
    _item(db, a.id, "A1-A3", 1000, "kg", f)
    r = epd_report(db, org.id, a.id, pcr_reference="EN 16485 (timber)")
    assert r["gwp_fossil_by_module_kg"]["A1-A3"]["gwp_fossil_kg"] == pytest.approx(500.0)  # fossil
    assert r["biogenic_co2_kg_separate"] == pytest.approx(1600.0)                  # separate


# --- Honest scope -------------------------------------------------------------------

def test_it_is_never_presented_as_a_verified_epd(db):
    org, a = _full_epd_assessment(db)
    r = epd_report(db, org.id, a.id, pcr_reference="EN 16757")
    assert r["verification_status"] == "unverified_data_report"
    assert "not a verified" in r["verification_note"].lower()
    assert any("GWP-total" in nc for nc in r["not_covered"])            # GWP-fossil-only stated
    assert r["indicator"].startswith("GWP-fossil")


# --- The gate -----------------------------------------------------------------------

def test_a_pcr_is_required(db):
    org, a = _full_epd_assessment(db)
    r = epd_report(db, org.id, a.id)                 # no PCR
    assert r["disclosure_ready"] is False
    assert any("Product Category Rule" in b for b in r["blockers"])


def test_a_non_en15804_assessment_cannot_be_an_epd(db):
    org = _org(db)
    a = _assess(db, org.id, "iso_14067")             # product PCF, not EN 15804
    _item(db, a.id, "raw_materials", 1000, "kg", _factor(db, 1.0).id)
    r = epd_report(db, org.id, a.id, pcr_reference="EN 16757")
    assert r["disclosure_ready"] is False
    assert any("en_15804" in b for b in r["blockers"])


def test_a_partial_assessment_blocks(db):
    org = _org(db)
    a = _assess(db, org.id, "en_15804")
    _item(db, a.id, "A1-A3", 10, "kg", None)         # unmapped -> incomplete
    r = epd_report(db, org.id, a.id, pcr_reference="EN 16757")
    assert r["disclosure_ready"] is False
    assert any("INCOMPLETE" in b for b in r["blockers"])


def test_a_missing_product_stage_blocks(db):
    org = _org(db)
    a = _assess(db, org.id, "en_15804")
    _item(db, a.id, "C3", 1000, "kg", _factor(db, 1.0).id)   # only end-of-life
    r = epd_report(db, org.id, a.id, pcr_reference="EN 16757")
    assert r["disclosure_ready"] is False
    assert any("A1-A3" in b and "mandatory" in b for b in r["blockers"])


def test_it_is_org_scoped(db):
    org_a = _org(db, "A")
    org_b, a = _full_epd_assessment(db)
    r = epd_report(db, org_a.id, a.id, pcr_reference="EN 16757")   # A reading B's assessment
    assert r["disclosure_ready"] is False
    assert any("not found" in b for b in r["blockers"])


def test_the_endpoint_is_reachable_and_org_scoped(client):
    c, hdr, _ = client
    aid = c.post("/lca/assessments",
                 params={"name": "Panel", "standard": "en_15804",
                         "functional_unit": "1 m2", "functional_unit_quantity": 1.0},
                 headers=hdr).json()["id"]
    r = c.get(f"/reports/epd/{aid}", params={"pcr_reference": "EN 16757"}, headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["framework"].startswith("ISO 14025")
    assert body["verification_status"] == "unverified_data_report"
    # another org cannot read it
    key2 = c.post("/organisations", params={"name": "Other"}).json()["api_key"]
    r2 = c.get(f"/reports/epd/{aid}", params={"pcr_reference": "EN 16757"},
               headers={"X-API-Key": key2})
    assert r2.json()["disclosure_ready"] is False


def test_product_stage_declared_as_separate_A1_A2_A3_folds_into_A1_A3(db):
    """Regression: A1, A2, A3 are valid EN modules but are not the A1-A3 aggregate the EPD
    table reports. Left unfolded, a product declared as A1+A2+A3 dropped out of the table
    entirely and read as a zero-total, no-product-stage EPD — a silent understatement."""
    org = _org(db)
    a = _assess(db, org.id, "en_15804", fu="1 m2", fu_qty=1.0)
    f = _factor(db, 1.0).id
    for st in ("A1", "A2", "A3"):
        _item(db, a.id, st, 1000, "kg", f)          # 3000 total, declared separately
    r = epd_report(db, org.id, a.id, pcr_reference="EN 16757")
    assert r["gwp_fossil_by_module_kg"]["A1-A3"]["declared"] is True
    assert r["gwp_fossil_by_module_kg"]["A1-A3"]["gwp_fossil_kg"] == pytest.approx(3000.0)
    assert r["declared_modules_gwp_fossil_kg"] == pytest.approx(3000.0)
    assert r["disclosure_ready"] is True


def test_the_module_table_always_conserves_the_assessment_ac_total(db):
    """The declared total must equal compute_assessment's own A-C total (Module D aside);
    a stage that failed to map to a table cell blocks rather than silently understating."""
    from app.services.lca import compute_assessment
    org, a = _full_epd_assessment(db)
    r = epd_report(db, org.id, a.id, pcr_reference="EN 16757")
    ac = compute_assessment(db, a)["total_co2e_kg"]
    assert r["declared_modules_gwp_fossil_kg"] == pytest.approx(ac)      # 8400, D excluded
    assert not any("understate" in b for b in r["blockers"])


def test_the_figure_is_labelled_gwp_fossil_not_gwp_total(db):
    """Regression (review, HIGH): compute_assessment's module total is GWP-FOSSIL (biogenic
    excluded), so labelling it 'GWP-total' misstated every bio-based product by its whole
    biogenic flux. The indicator is named honestly and GWP-total is a stated omission."""
    org = _org(db)
    a = _assess(db, org.id, "en_15804", fu="1 kg", fu_qty=1.0)
    f = _factor(db, 0.5, biogenic=1.6).id                 # timber: fossil 0.5, biogenic 1.6
    _item(db, a.id, "A1-A3", 1000, "kg", f)
    r = epd_report(db, org.id, a.id, pcr_reference="EN 16485")
    assert "gwp_fossil_kg" in r["gwp_fossil_by_module_kg"]["A1-A3"]
    assert "gwp_total_kg" not in r["gwp_fossil_by_module_kg"]["A1-A3"]
    assert r["gwp_fossil_by_module_kg"]["A1-A3"]["gwp_fossil_kg"] == pytest.approx(500.0)
    assert r["declared_modules_gwp_fossil_kg"] == pytest.approx(500.0)   # NOT 500+1600
    assert r["indicator"].startswith("GWP-fossil")
    assert any("GWP-total" in nc and "GWP-biogenic" in nc for nc in r["not_covered"])
    # the biogenic figure is present but flagged as NOT the EN GWP-biogenic sub-indicator
    assert r["biogenic_co2_kg_separate"] == pytest.approx(1600.0)
    assert "not the en 15804+a2 gwp-biogenic" in r["biogenic_co2_note"].lower()


def test_a_full_14_module_epd_is_not_false_blocked_by_rounding(db):
    """Regression (review, MEDIUM): the conservation guard re-summed 6dp-rounded module
    values against a round-of-sum total with a 1e-6 tolerance, false-blocking ~46% of
    complete multi-module EPDs on accumulated round-off. The guard is now an EXACT
    set-membership check with no floating point."""
    org = _org(db)
    a = _assess(db, org.id, "en_15804", fu="1 m2", fu_qty=1.0)
    vals = [0.4030927, 2.5423012, 2.2913239, 0.7652071, 1.4863053, 1.3484732, 1.9547789,
            2.3661701, 0.2815788, 0.0850424, 2.5072953, 1.2983012, 2.2868402, 0.0063182]
    stages = ["A1-A3", "A4", "A5", "B1", "B2", "B3", "B4", "B5", "B6", "B7",
              "C1", "C2", "C3", "C4"]
    for st, v in zip(stages, vals):
        _item(db, a.id, st, 1, "kg", _factor(db, v).id)
    r = epd_report(db, org.id, a.id, pcr_reference="EN 16757")
    assert r["disclosure_ready"] is True
    assert not any("platform defect" in b for b in r["blockers"])


def test_a_whole_building_assessment_is_not_a_product_epd(db):
    """Regression (review, MEDIUM): en_15978 is a whole-BUILDING assessment, not a product
    EPD — labelling one as an ISO 14025 product EPD overclaimed its scope."""
    org = _org(db)
    a = _assess(db, org.id, "en_15978", fu="1 building")
    _item(db, a.id, "A1-A3", 1000, "kg", _factor(db, 1.0).id)
    r = epd_report(db, org.id, a.id, pcr_reference="EN 16757")
    assert r["disclosure_ready"] is False
    assert any("PRODUCT declaration" in b and "en_15978" in b for b in r["blockers"])
