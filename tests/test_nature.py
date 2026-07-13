"""Nature module: TNFD LEAP assessment + SBTN targets.

Service-level tests run against the `db` fixture; the API tests exercise
fail-closed validation and cross-tenant isolation through the real endpoints.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
import app.models  # noqa: F401
from app.models import Organisation, NatureSite, NatureImpactDependency, NatureTarget
from app import main as main_mod
from app.services.nature import (
    leap_assessment, sbtn_report, site_is_sensitive, sensitivity_reasons, valid_driver,
)


def _org(db, name="Co"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _site(db, org_id, **kw):
    kw.setdefault("name", "site")
    kw.setdefault("area_hectares", 0.0)
    kw.setdefault("water_stress", "unknown")
    s = NatureSite(organisation_id=org_id, **kw)
    db.add(s); db.commit(); db.refresh(s)
    return s


def _impact(db, site_id, kind="impact", driver="freshwater_use", materiality="low",
            metric_value=None, metric_unit=None):
    it = NatureImpactDependency(site_id=site_id, kind=kind, driver=driver,
                                materiality=materiality, metric_value=metric_value,
                                metric_unit=metric_unit)
    db.add(it); db.commit(); db.refresh(it)
    return it


# --- vocabularies -------------------------------------------------------------

def test_valid_driver_is_kind_specific():
    assert valid_driver("impact", "freshwater_use")
    assert not valid_driver("impact", "pollination")          # that's a service
    assert valid_driver("dependency", "pollination")
    assert not valid_driver("dependency", "land_use_change")  # that's a driver
    assert not valid_driver("bogus", "freshwater_use")


# --- Locate -------------------------------------------------------------------

def test_empty_org_not_ready(db):
    r = leap_assessment(db, _org(db).id)
    assert r["report_ready"] is False
    assert r["locate"]["site_count"] == 0


def test_sensitivity_is_union_of_three_flags(db):
    org = _org(db)
    prot = _site(db, org.id, name="prot", in_protected_area=True)
    kba = _site(db, org.id, name="kba", in_kba=True)
    stressed = _site(db, org.id, name="stressed", water_stress="extreme")
    plain = _site(db, org.id, name="plain", water_stress="low")
    assert site_is_sensitive(prot) and site_is_sensitive(kba) and site_is_sensitive(stressed)
    assert not site_is_sensitive(plain)
    assert "protected_area" in sensitivity_reasons(prot)
    assert sensitivity_reasons(stressed) == ["water_stress:extreme"]


def test_area_exposure_and_pct(db):
    org = _org(db)
    _site(db, org.id, name="a", area_hectares=100.0, in_protected_area=True)  # sensitive
    _site(db, org.id, name="b", area_hectares=300.0, water_stress="low")      # not
    r = leap_assessment(db, org.id)
    loc = r["locate"]
    assert loc["total_area_hectares"] == pytest.approx(400.0)
    assert loc["area_in_sensitive_locations_hectares"] == pytest.approx(100.0)
    assert loc["pct_area_in_sensitive_locations"] == pytest.approx(25.0)
    assert loc["sensitive_site_count"] == 1


def test_pct_is_none_when_no_area_data(db):
    """A sensitive site with zero area must not read as '0% in sensitive locations'."""
    org = _org(db)
    _site(db, org.id, name="s", area_hectares=0.0, in_kba=True)
    r = leap_assessment(db, org.id)
    assert r["locate"]["pct_area_in_sensitive_locations"] is None
    assert r["locate"]["sensitive_site_count"] == 1


# --- Evaluate -----------------------------------------------------------------

def test_water_withdrawal_counts_only_stressed_basins_with_values(db):
    org = _org(db)
    s_ext = _site(db, org.id, name="ext", water_stress="extreme")
    s_high = _site(db, org.id, name="high", water_stress="high")
    s_unk = _site(db, org.id, name="unk", water_stress="unknown")
    s_low = _site(db, org.id, name="low", water_stress="low")
    _impact(db, s_ext.id, driver="freshwater_use", metric_value=1000.0)   # counted
    _impact(db, s_high.id, driver="freshwater_use", metric_value=None)    # stressed but no value
    _impact(db, s_unk.id, driver="freshwater_use", metric_value=500.0)    # unclassifiable
    _impact(db, s_low.id, driver="freshwater_use", metric_value=999.0)    # not stressed
    _impact(db, s_ext.id, driver="pollution", metric_value=42.0)          # not freshwater
    r = leap_assessment(db, org.id)
    ev = r["evaluate"]
    assert ev["water_withdrawal_in_water_stressed_basins"] == pytest.approx(1000.0)
    joined = " ".join(r["warnings"])
    assert "INCOMPLETE" in joined          # the None-value one
    assert "unknown water" in joined       # the unknown-stress one


def test_mixed_water_units_are_flagged(db):
    org = _org(db)
    s = _site(db, org.id, water_stress="extreme")
    _impact(db, s.id, driver="freshwater_use", metric_value=1000.0, metric_unit="m3")
    _impact(db, s.id, driver="freshwater_use", metric_value=500.0, metric_unit="litre")
    r = leap_assessment(db, org.id)
    assert any("INCONSISTENT metric_units" in w for w in r["warnings"])


def test_impacts_and_dependencies_grouped(db):
    org = _org(db)
    s = _site(db, org.id, water_stress="high")
    _impact(db, s.id, kind="impact", driver="pollution", materiality="high")
    _impact(db, s.id, kind="impact", driver="pollution", materiality="low")
    _impact(db, s.id, kind="dependency", driver="water_provision", materiality="medium")
    r = leap_assessment(db, org.id)
    ev = r["evaluate"]
    assert ev["impacts_by_driver"]["pollution"]["count"] == 2
    assert ev["impacts_by_driver"]["pollution"]["high"] == 1
    assert ev["dependencies_by_service"]["water_provision"]["medium"] == 1
    assert ev["impact_count"] == 2 and ev["dependency_count"] == 1


# --- Assess -------------------------------------------------------------------

def test_priority_needs_sensitive_location_and_high_impact(db):
    org = _org(db)
    # sensitive + high impact -> priority
    s1 = _site(db, org.id, name="p", water_stress="extreme")
    _impact(db, s1.id, driver="freshwater_use", materiality="high")
    # sensitive but only low impact -> not priority
    s2 = _site(db, org.id, name="np1", in_kba=True)
    _impact(db, s2.id, driver="pollution", materiality="low")
    # high impact but NOT sensitive -> not priority
    s3 = _site(db, org.id, name="np2", water_stress="low")
    _impact(db, s3.id, driver="pollution", materiality="high")
    r = leap_assessment(db, org.id)
    assert r["assess"]["priority_site_count"] == 1
    assert r["assess"]["priority_sites"][0]["site_id"] == s1.id


def test_leap_is_org_scoped(db):
    a, b = _org(db, "A"), _org(db, "B")
    sb = _site(db, b.id, name="b-site", water_stress="extreme", area_hectares=50.0)
    _impact(db, sb.id, driver="freshwater_use", metric_value=123.0)
    r = leap_assessment(db, a.id)
    assert r["report_ready"] is False and r["locate"]["site_count"] == 0


# --- SBTN ---------------------------------------------------------------------

def test_sbtn_signed_change_and_validation(db):
    org = _org(db)
    db.add(NatureTarget(organisation_id=org.id, realm="freshwater", name="cut withdrawal",
                        baseline_value=1000.0, baseline_unit="m3", target_value=700.0,
                        target_year=2030, validated=True))
    db.add(NatureTarget(organisation_id=org.id, realm="land", name="restore",
                        baseline_value=0.0, baseline_unit="ha", target_value=50.0,
                        target_year=2035, validated=False))
    db.commit()
    r = sbtn_report(db, org.id)
    assert r["report_ready"] is True and r["target_count"] == 2
    fw = next(t for t in r["targets"] if t["realm"] == "freshwater")
    assert fw["change"] == pytest.approx(-300.0)     # reduction, signed
    assert fw["change_pct"] == pytest.approx(-30.0)
    land = next(t for t in r["targets"] if t["realm"] == "land")
    assert land["change_pct"] is None                # baseline 0 -> no pct
    assert r["validated_count"] == 1
    assert r["targets_by_realm"] == {"freshwater": 1, "land": 1}


# --- API: validation + tenancy ------------------------------------------------

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
    key_a = c.post("/organisations", params={"name": "A"}).json()["api_key"]
    key_b = c.post("/organisations", params={"name": "B"}).json()["api_key"]
    yield c, {"X-API-Key": key_a}, {"X-API-Key": key_b}
    main_mod.app.dependency_overrides.clear()


def test_api_site_validation(client):
    c, hdr, _ = client
    assert c.post("/nature/sites", params={"name": "x", "water_stress": "meh"}, headers=hdr).status_code == 400
    assert c.post("/nature/sites", params={"name": "x", "area_hectares": -1}, headers=hdr).status_code == 400
    assert c.post("/nature/sites", params={"name": "x", "latitude": 200}, headers=hdr).status_code == 400
    ok = c.post("/nature/sites", params={"name": "ok", "water_stress": "high"}, headers=hdr)
    assert ok.status_code == 200


def test_api_impact_validation_and_tenancy(client):
    c, hdr_a, hdr_b = client
    site_a = c.post("/nature/sites", params={"name": "a"}, headers=hdr_a).json()["id"]
    # wrong driver for kind
    assert c.post(f"/nature/sites/{site_a}/impacts",
                  params={"kind": "impact", "driver": "pollination"}, headers=hdr_a).status_code == 400
    # bad materiality
    assert c.post(f"/nature/sites/{site_a}/impacts",
                  params={"kind": "impact", "driver": "pollution", "materiality": "huge"},
                  headers=hdr_a).status_code == 400
    # valid
    assert c.post(f"/nature/sites/{site_a}/impacts",
                  params={"kind": "impact", "driver": "pollution", "materiality": "high"},
                  headers=hdr_a).status_code == 200
    # org B cannot attach to org A's site -> 404 (no cross-tenant write)
    assert c.post(f"/nature/sites/{site_a}/impacts",
                  params={"kind": "impact", "driver": "pollution"}, headers=hdr_b).status_code == 404


def test_api_target_validation(client):
    c, hdr, _ = client
    base = {"name": "t", "baseline_value": 1.0, "baseline_unit": "m3", "target_value": 0.5}
    assert c.post("/nature/targets", params={**base, "realm": "air", "target_year": 2030},
                  headers=hdr).status_code == 400
    assert c.post("/nature/targets", params={**base, "realm": "freshwater", "target_year": 1990},
                  headers=hdr).status_code == 400
    assert c.post("/nature/targets", params={**base, "realm": "freshwater", "target_year": 2030},
                  headers=hdr).status_code == 200


def test_api_reports_carry_guidance(client):
    c, hdr, _ = client
    site = c.post("/nature/sites", params={"name": "s", "water_stress": "extreme",
                                           "area_hectares": 10}, headers=hdr).json()["id"]
    c.post(f"/nature/sites/{site}/impacts",
           params={"kind": "impact", "driver": "freshwater_use", "materiality": "high",
                   "metric_value": 500}, headers=hdr)
    tnfd = c.get("/reports/tnfd", headers=hdr).json()
    assert tnfd["guidance"]["key"] == "tnfd"
    assert tnfd["assess"]["priority_site_count"] == 1
    sbtn = c.get("/reports/sbtn", headers=hdr).json()
    assert sbtn["guidance"]["key"] == "sbtn"
    assert sbtn["report_ready"] is False   # no targets yet
