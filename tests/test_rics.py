"""RICS Whole Life Carbon (2nd ed.) renderer — the carbon figures in RICS groupings.

Honest by construction: the arithmetic RICS asks for over an EN 15978 building assessment,
GWP-fossil only, never a full RICS-compliant WLCA. Same fail-closed doctrine as every other
renderer.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
from app import main as main_mod
from app.models import Organisation, EmissionFactor, LcaAssessment, LcaItem
from app.reports.rics import rics_report


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
    key = c.post("/organisations", params={"name": "Builder"}).json()["api_key"]
    yield c, {"X-API-Key": key}, Session
    main_mod.app.dependency_overrides.clear()


def _org(db, name="Builder"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, value, biogenic=None):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="material", subcategory="", unit="kg", gwp_set="AR6",
                       value=value, kg_co2_biogenic=biogenic)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _assess(db, org_id, standard="en_15978", fu="1 m2 GIA", fu_qty=1000.0):
    a = LcaAssessment(organisation_id=org_id, name="Office block", standard=standard,
                      functional_unit=fu, functional_unit_quantity=fu_qty, gwp_set="AR6")
    db.add(a); db.commit(); db.refresh(a)
    return a


def _item(db, aid, stage, qty, unit, factor_id):
    it = LcaItem(assessment_id=aid, stage=stage, quantity=qty, unit=unit,
                 factor_id=factor_id, allocation_factor=1.0)
    db.add(it); db.commit(); db.refresh(it)
    return it


def _building(db):
    """A full building: upfront A1-A5, use-stage B4, operational B6, end-of-life C3, and D."""
    org = _org(db)
    a = _assess(db, org.id, "en_15978", fu="1 m2 GIA", fu_qty=1000.0)
    f = _factor(db, 1.0).id
    _item(db, a.id, "A1-A3", 5000, "kg", f)      # 5000 — product
    _item(db, a.id, "A5", 1000, "kg", f)         # 1000 — construction  (upfront = 6000)
    _item(db, a.id, "B4", 2000, "kg", f)         # 2000 — replacement (use-stage embodied)
    _item(db, a.id, "B6", 8000, "kg", f)         # 8000 — operational energy
    _item(db, a.id, "C3", 1000, "kg", f)         # 1000 — end of life
    _item(db, a.id, "D", -1500, "kg", f)         # -1500 — beyond boundary
    return org, a


# --- The RICS groupings -------------------------------------------------------------

def test_the_headline_metrics(db):
    org, a = _building(db)
    r = rics_report(db, org.id, a.id, gia_unit="m2 GIA")
    assert r["disclosure_ready"] is True
    assert r["upfront_carbon_kg"] == pytest.approx(6000.0)         # A1-A3 + A5
    assert r["embodied_carbon_kg"] == pytest.approx(9000.0)        # 6000 + 2000 + 1000
    assert r["operational_carbon_kg"] == pytest.approx(8000.0)     # B6
    assert r["whole_life_carbon_kg"] == pytest.approx(17000.0)     # embodied + operational
    # per m2 GIA (/1000)
    assert r["whole_life_carbon_per_area_kg"] == pytest.approx(17.0)
    assert r["upfront_carbon_per_area_kg"] == pytest.approx(6.0)


def test_embodied_plus_operational_equals_whole_life(db):
    org, a = _building(db)
    r = rics_report(db, org.id, a.id)
    assert r["embodied_carbon_kg"] + r["operational_carbon_kg"] == \
        pytest.approx(r["whole_life_carbon_kg"])
    # and the four sub-groupings partition whole-life exactly
    g = r["by_grouping_kg"]
    assert (g["upfront_A1_A5"] + g["use_stage_embodied_B1_B5"] + g["end_of_life_C1_C4"]
            + g["operational_B6_B7"]) == pytest.approx(r["whole_life_carbon_kg"])


def test_whole_life_equals_the_assessment_ac_total_and_excludes_module_d(db):
    """Whole-life carbon is the declared A-C total; Module D is SEPARATE, never netted in
    (a -1500 recycling credit must not shrink whole-life)."""
    from app.services.lca import compute_assessment
    org, a = _building(db)
    r = rics_report(db, org.id, a.id)
    ac = compute_assessment(db, a)["total_co2e_kg"]
    assert r["whole_life_carbon_kg"] == pytest.approx(ac)          # 17000
    assert r["module_D_beyond_boundary_kg"] == pytest.approx(-1500.0)
    assert "understate" not in " ".join(r["blockers"])


# --- Honest scope -------------------------------------------------------------------

def test_the_figure_is_gwp_fossil_with_biogenic_separate(db):
    org = _org(db)
    a = _assess(db, org.id, "en_15978")
    f = _factor(db, 0.5, biogenic=1.6).id                 # timber: fossil 0.5, biogenic 1.6
    _item(db, a.id, "A1-A3", 1000, "kg", f)
    r = rics_report(db, org.id, a.id)
    assert r["upfront_carbon_kg"] == pytest.approx(500.0)          # fossil, NOT 500+1600
    assert r["indicator"].startswith("GWP-fossil")
    assert r["biogenic_co2_kg_separate"] == pytest.approx(1600.0)
    assert "not rics biogenic sequestration" in r["biogenic_co2_note"].lower()
    assert any("sequestration" in nc for nc in r["not_covered"])


def test_it_never_claims_to_be_a_full_rics_wlca(db):
    org, a = _building(db)
    r = rics_report(db, org.id, a.id)
    assert any("benchmarking" in nc.lower() for nc in r["not_covered"])
    assert any("scenario" in nc.lower() for nc in r["not_covered"])
    assert "not a full RICS" not in r  # (sanity: the caveat lives in methodology/not_covered)
    assert "not a full rics-compliant wlca" in r["methodology"].lower() or \
        any("not a full RICS" in nc for nc in r["not_covered"]) or \
        "GWP-FOSSIL" in r["methodology"]


# --- The gate + partition -----------------------------------------------------------

def test_a_product_epd_is_not_a_building_wlca(db):
    org = _org(db)
    a = _assess(db, org.id, "en_15804")           # product, not building
    _item(db, a.id, "A1-A3", 1000, "kg", _factor(db, 1.0).id)
    r = rics_report(db, org.id, a.id)
    assert r["disclosure_ready"] is False
    assert any("en_15978" in b and "whole-building" in b for b in r["blockers"])


def test_a_partial_assessment_blocks(db):
    org = _org(db)
    a = _assess(db, org.id, "en_15978")
    _item(db, a.id, "A1-A3", 10, "kg", None)      # unmapped -> incomplete
    r = rics_report(db, org.id, a.id)
    assert r["disclosure_ready"] is False
    assert any("INCOMPLETE" in b for b in r["blockers"])


def test_separately_declared_a1_a2_a3_fold_into_upfront(db):
    """Regression discipline carried from the EPD renderer: A1/A2/A3 are valid EN modules
    that must fold into the A1-A3 aggregate, not drop out of upfront/whole-life."""
    org = _org(db)
    a = _assess(db, org.id, "en_15978", fu_qty=1.0)
    f = _factor(db, 1.0).id
    for st in ("A1", "A2", "A3"):
        _item(db, a.id, st, 1000, "kg", f)        # 3000 upfront, declared separately
    r = rics_report(db, org.id, a.id)
    assert r["upfront_carbon_kg"] == pytest.approx(3000.0)
    assert r["whole_life_carbon_kg"] == pytest.approx(3000.0)
    assert r["disclosure_ready"] is True


def test_the_module_partition_is_exact_at_import():
    """A future EN module added but not grouped would silently drop from whole-life; the
    import-time proof forbids it."""
    from app.services.lca import EN_MODULES
    from app.reports.rics import _MODULE_GROUP
    assert set(_MODULE_GROUP) == (EN_MODULES - {"A1", "A2", "A3", "D"})


def test_it_is_org_scoped(db):
    org_a = _org(db, "A")
    org_b, a = _building(db)
    r = rics_report(db, org_a.id, a.id)           # A reading B's assessment
    assert r["disclosure_ready"] is False
    assert any("not found" in b for b in r["blockers"])


def test_the_endpoint_is_reachable_and_org_scoped(client):
    c, hdr, _ = client
    aid = c.post("/lca/assessments",
                 params={"name": "Tower", "standard": "en_15978",
                         "functional_unit": "1 m2 GIA", "functional_unit_quantity": 500.0},
                 headers=hdr).json()["id"]
    r = c.get(f"/reports/rics/{aid}", params={"gia_unit": "m2 GIA"}, headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["framework"].startswith("RICS Whole Life Carbon")
    key2 = c.post("/organisations", params={"name": "Rival"}).json()["api_key"]
    r2 = c.get(f"/reports/rics/{aid}", headers={"X-API-Key": key2})
    assert r2.json()["disclosure_ready"] is False


def test_an_assessment_with_no_upfront_carbon_blocks(db):
    """Regression (review, LOW): an empty or Module-D-only en_15978 assessment read as a
    'ready' whole-life carbon of ZERO — physically impossible (every building has upfront
    carbon). Mirrors the EPD renderer's mandatory-core gate; fail-closed, not blessed-zero."""
    org = _org(db)
    a_empty = _assess(db, org.id, "en_15978")                 # no items at all
    r = rics_report(db, org.id, a_empty.id)
    assert r["disclosure_ready"] is False
    assert any("upfront-carbon (A1-A5)" in b for b in r["blockers"])
    # Module-D-only is the sharper case: a recycling credit beside a zero whole-life.
    a_d = _assess(db, org.id, "en_15978", fu="1 m2 GIA")
    _item(db, a_d.id, "D", -1500, "kg", _factor(db, 1.0).id)
    r2 = rics_report(db, org.id, a_d.id)
    assert r2["disclosure_ready"] is False
    assert any("upfront-carbon (A1-A5)" in b for b in r2["blockers"])


def test_an_operational_only_assessment_still_requires_upfront(db):
    """A building modelled with only operational energy (B6) and no embodied stages is not
    a whole-life assessment — upfront is still required."""
    org = _org(db)
    a = _assess(db, org.id, "en_15978")
    _item(db, a.id, "B6", 8000, "kg", _factor(db, 1.0).id)
    r = rics_report(db, org.id, a.id)
    assert r["disclosure_ready"] is False
    assert any("upfront-carbon (A1-A5)" in b for b in r["blockers"])
