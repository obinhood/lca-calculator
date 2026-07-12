import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
import app.models  # noqa: F401
from app.models import EmissionFactor, ActivityRecord, Organisation
from app import main as main_mod
from app.services.calc import compute_co2e
from app.reports.framework_guidance import FRAMEWORKS, guidance_ref, list_frameworks


def test_every_framework_entry_is_well_formed():
    required = {"name", "category", "jurisdiction", "authority",
                "platform_support", "endpoint", "key_points"}
    for key, g in FRAMEWORKS.items():
        assert required <= set(g), f"{key} missing {required - set(g)}"
        assert g["platform_support"] in ("built", "partial", "reference"), key
        assert isinstance(g["key_points"], list) and g["key_points"], key


def test_user_taxonomy_is_covered():
    """Every standard the user listed must have a guidance entry."""
    names = " ".join(g["name"] for g in FRAMEWORKS.values())
    for token in ["GHG Protocol", "ISO 14064", "ISO 14067", "ISO 14040", "PEF",
                  "ESRS", "IFRS S2", "CDP", "GRI", "SBTi", "PCAF", "SFDR",
                  "EU Taxonomy", "CBAM", "EU ETS", "UK ETS", "SECR", "ESOS",
                  "ISO 14083", "GLEC", "EN 15978", "EN 15804", "RICS",
                  "ICVCM", "VCMI", "Verra", "Gold Standard", "TNFD", "SBTN",
                  "ISAE 3410", "ISO 14068", "ISO 14025", "TCFD", "ISSA 5000",
                  "SB 253", "CSDDD"]:
        assert token in names, f"missing guidance for {token}"


def test_guidance_ref_maps_report_framework_names():
    assert guidance_ref("CSRD ESRS E1")["key"] == "esrs_e1"
    assert guidance_ref("ISSB IFRS S2")["key"] == "issb_s2"
    assert guidance_ref("EU CBAM (definitive period)")["key"] == "cbam"
    assert guidance_ref("SBTi target")["key"] == "sbti"
    assert guidance_ref("ISO 14068-1 carbon neutrality")["key"] == "iso_14068"
    assert guidance_ref("Something Unknown") is None


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()
    main_mod.app.dependency_overrides[main_mod.get_db] = override
    c = TestClient(main_mod.app)
    # seed a factor + a run so a report can be generated
    seed = Session()
    seed.add(EmissionFactor(source="DEFRA_DEMO", version="2024.1", geography="GB", year=2024,
                            category="electricity", subcategory="", unit="kWh", gwp_set="AR6",
                            value=0.17))
    org = Organisation(name="G", api_key_hash=main_mod._hash_key("k"))
    seed.add(org); seed.commit(); seed.refresh(org)
    seed.add(ActivityRecord(organisation_id=org.id, date="2025-01-01", category="electricity",
                            subcategory="", description="", quantity=1000, unit="kWh", geo="GB",
                            factor_id=1))
    seed.commit()
    compute_co2e(seed, org.id)
    seed.close()
    yield c, {"X-API-Key": "k"}
    main_mod.app.dependency_overrides.clear()


def test_frameworks_endpoints_public(client):
    c, _ = client
    all_fw = c.get("/frameworks")
    assert all_fw.status_code == 200
    assert len(all_fw.json()) == len(list_frameworks()) >= 30
    # filter by category
    fin = c.get("/frameworks", params={"category": "Finance"}).json()
    assert all(f["category"] == "Finance" for f in fin) and len(fin) >= 3
    # detail
    g = c.get("/frameworks/cbam")
    assert g.status_code == 200
    assert g.json()["key"] == "cbam" and "CBAM factor" in " ".join(g.json()["key_points"])
    assert c.get("/frameworks/nope").status_code == 404


def test_report_payloads_carry_inline_guidance(client):
    c, hdr = client
    r = c.get("/reports/secr", params={"intensity_denominator": 1.0}, headers=hdr).json()
    assert r["guidance"]["key"] == "secr"
    assert r["guidance"]["full_guidance"] == "/frameworks/secr"
    assert r["guidance"]["key_points"]
    e = c.get("/reports/esrs_e1", params={"net_revenue_millions": 1.0}, headers=hdr).json()
    assert e["guidance"]["key"] == "esrs_e1"
