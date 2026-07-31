"""EU PEF single-category coverage map.

The platform computes 1 of PEF's 16 EF 3.1 impact categories (Climate change), and only its
GWP-fossil sub-indicator. These tests pin: all 16 categories listed with only Climate change
carrying a value, the GWP-fossil figure matching the LCA engine, the loud "not a PEF profile"
framing, biogenic kept separate, wrong-standard and incomplete-assessment gates, and that the
category-ready signal is never a whole-profile 'ready'.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
from app import main as main_mod
from app.models import Organisation, EmissionFactor, LcaAssessment, LcaItem
from app.services.lca import compute_assessment
from app.reports.pef import pef_report


def _org(db, name="Maker"):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, value, unit="kg", biogenic=None):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024, category="material",
                       subcategory="", unit=unit, gwp_set="AR6", value=value,
                       kg_co2_biogenic=biogenic)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _assess(db, org_id, standard="iso_14067", fu="1 kg product", fu_qty=1.0):
    a = LcaAssessment(organisation_id=org_id, name="Widget", standard=standard,
                      functional_unit=fu, functional_unit_quantity=fu_qty, gwp_set="AR6")
    db.add(a); db.commit(); db.refresh(a)
    return a


def _item(db, aid, stage, qty, unit, factor_id):
    it = LcaItem(assessment_id=aid, stage=stage, quantity=qty, unit=unit, factor_id=factor_id,
                 allocation_factor=1.0)
    db.add(it); db.commit(); db.refresh(it)
    return it


def test_sixteen_categories_only_climate_populated(db):
    org = _org(db)
    a = _assess(db, org.id, fu_qty=10.0)
    _item(db, a.id, "raw_materials", 100, "kg", _factor(db, 2.0).id)     # 200 kg CO2e
    r = pef_report(db, org.id, a.id)
    cats = r["impact_categories_ef_3_1"]
    assert len(cats) == 16
    climate = next(c for c in cats if c["category"] == "Climate change")
    assert climate["computed"] == "partial"
    assert climate["gwp_fossil_kg"] == pytest.approx(200.0)
    assert climate["gwp_fossil_per_declared_unit_kg"] == pytest.approx(20.0)   # /10 units
    assert climate["gwp_fossil_ready"] is True
    # the other 15 are explicitly not computed, value None
    others = [c for c in cats if c["category"] != "Climate change"]
    assert len(others) == 15
    assert all(c["computed"] is False and c["value"] is None for c in others)


def test_matches_lca_engine_number(db):
    org = _org(db)
    a = _assess(db, org.id)
    _item(db, a.id, "raw_materials", 50, "kg", _factor(db, 2.0).id)
    r = pef_report(db, org.id, a.id)
    engine = compute_assessment(db, a)
    climate = next(c for c in r["impact_categories_ef_3_1"] if c["category"] == "Climate change")
    assert climate["gwp_fossil_kg"] == pytest.approx(engine["total_co2e_kg"])


def test_never_reads_as_a_pef_profile(db):
    org = _org(db)
    a = _assess(db, org.id)
    _item(db, a.id, "raw_materials", 10, "kg", _factor(db, 2.0).id)
    r = pef_report(db, org.id, a.id)
    assert r["pef_profile_status"].startswith("single_impact_category_only")
    assert "disclosure_ready" not in r                      # no misreadable whole-profile flag
    assert "NOT the PEF Climate change" in r["climate_change_indicator"]
    assert len(r["not_produced"]) >= 5


def test_biogenic_separate_never_in_climate_figure(db):
    org = _org(db)
    a = _assess(db, org.id)
    # timber: 0.5 fossil + 1.6 biogenic per kg
    _item(db, a.id, "raw_materials", 10, "kg", _factor(db, 0.5, biogenic=1.6).id)
    r = pef_report(db, org.id, a.id)
    climate = next(c for c in r["impact_categories_ef_3_1"] if c["category"] == "Climate change")
    assert climate["gwp_fossil_kg"] == pytest.approx(5.0)   # fossil only
    assert r["biogenic_co2_kg_separate"] == pytest.approx(16.0)


def test_wrong_standard_blocks(db):
    """A transport-chain assessment is not a PEF product study."""
    org = _org(db)
    a = _assess(db, org.id, standard="iso_14083", fu="1 t.km", fu_qty=1000.0)
    _item(db, a.id, "leg1", 1000, "tkm", _factor(db, 0.1, unit="tkm").id)
    r = pef_report(db, org.id, a.id)
    assert r["climate_change_gwp_fossil_ready"] is False
    assert any("PRODUCT footprint" in b for b in r["blockers"])


def test_incomplete_assessment_blocks(db):
    org = _org(db)
    a = _assess(db, org.id)
    it = LcaItem(assessment_id=a.id, stage="raw_materials", quantity=10, unit="kg",
                 factor_id=None, allocation_factor=1.0)   # unmapped -> incomplete
    db.add(it); db.commit()
    r = pef_report(db, org.id, a.id)
    assert r["climate_change_gwp_fossil_ready"] is False
    assert any("INCOMPLETE" in b for b in r["blockers"])


def test_empty_assessment_blocks(db):
    """An assessment with NO items is complete-looking (nothing to exclude) with a 0 kg total.
    It must not read as a soundly-computed, ready Climate change result."""
    org = _org(db)
    a = _assess(db, org.id)                                 # no items added
    r = pef_report(db, org.id, a.id)
    climate = next(c for c in r["impact_categories_ef_3_1"] if c["category"] == "Climate change")
    assert climate["gwp_fossil_kg"] == 0.0
    assert r["climate_change_gwp_fossil_ready"] is False    # 0 kg is not a computed value
    assert any("NO computable emission lines" in b for b in r["blockers"])


def test_every_category_row_carries_value_key_uniformly(db):
    """All 16 rows expose `value` (the full GWP-total category result) — None on every row,
    including Climate change, whose full-category value the platform does not produce. A generic
    consumer reading row['value'] must never KeyError."""
    org = _org(db)
    a = _assess(db, org.id)
    _item(db, a.id, "raw_materials", 10, "kg", _factor(db, 2.0).id)
    r = pef_report(db, org.id, a.id)
    cats = r["impact_categories_ef_3_1"]
    assert all("value" in c for c in cats)                  # uniform schema, no KeyError
    assert all(c["value"] is None for c in cats)            # full-category value never produced
    climate = next(c for c in cats if c["category"] == "Climate change")
    assert climate["gwp_fossil_kg"] == pytest.approx(20.0)  # sub-indicator in its own field


def test_tenant_isolation(db):
    org_a, org_b = _org(db, "A"), _org(db, "B")
    a = _assess(db, org_b.id)
    _item(db, a.id, "raw_materials", 10, "kg", _factor(db, 2.0).id)
    r = pef_report(db, org_a.id, a.id)                     # A asks for B's assessment
    assert r["climate_change_gwp_fossil_ready"] is False
    assert any("not found" in b for b in r["blockers"])


# --- endpoint ---

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
    key = c.post("/organisations", params={"name": "Maker"}).json()["api_key"]
    yield c, {"X-API-Key": key}, Session
    main_mod.app.dependency_overrides.clear()


def test_endpoint_and_guidance(client):
    c, hdr, Session = client
    db = Session()
    org = db.query(Organisation).first()
    a = _assess(db, org.id)
    _item(db, a.id, "raw_materials", 10, "kg", _factor(db, 2.0).id)
    aid = a.id
    db.close()
    resp = c.get(f"/reports/pef/{aid}", headers=hdr)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["impact_categories_ef_3_1"]) == 16
    assert body.get("guidance", {}).get("key") == "pef"
