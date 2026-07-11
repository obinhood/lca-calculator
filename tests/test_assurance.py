"""Assurance workspace: readiness checklist, materiality-gated conclusion,
read-only assuror token, tenant isolation — HTTP level."""
import io
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
import app.models  # noqa: F401
from app.models import EmissionFactor
from app import main as main_mod


@pytest.fixture
def env():
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

    seed = Session()
    seed.add(EmissionFactor(source="DEFRA_DEMO", version="2024.1", geography="GB", year=2024,
                            category="electricity", subcategory="", unit="kWh", gwp_set="AR6",
                            value=0.17, kg_co2=0.168337, kg_ch4=0.00001, kg_n2o=0.000005,
                            ch4_origin="fossil", method_type="average_data",
                            lca_boundary="generation"))
    seed.commit(); seed.close()

    key_a = c.post("/organisations", params={"name": "A"}).json()["api_key"]
    key_b = c.post("/organisations", params={"name": "B"}).json()["api_key"]
    hdr_a, hdr_b = {"X-API-Key": key_a}, {"X-API-Key": key_b}
    yield c, hdr_a, hdr_b
    main_mod.app.dependency_overrides.clear()


def _clean_run(c, hdr):
    """Full-coverage run (assurance-ready)."""
    csv = ("date,category,subcategory,description,quantity,unit,geo\n"
           "2025-01-15,electricity,,HQ,1000,kWh,GB\n")
    c.post("/activities/upload_csv", headers=hdr,
           files={"file": ("a.csv", io.BytesIO(csv.encode()), "text/csv")})
    return c.post("/calculate/run", headers=hdr).json()["run"]["id"]


def _partial_run(c, hdr):
    """Run with an unmapped activity -> readiness fails on completeness."""
    csv = ("date,category,subcategory,description,quantity,unit,geo\n"
           "2025-01-15,electricity,,HQ,1000,kWh,GB\n"
           "2025-02-01,widgets,,mystery,5,kg,GB\n")
    c.post("/activities/upload_csv", headers=hdr,
           files={"file": ("b.csv", io.BytesIO(csv.encode()), "text/csv")})
    return c.post("/calculate/run", headers=hdr).json()["run"]["id"]


def test_readiness_ready_vs_partial(env):
    c, hdr_a, _ = env
    clean = _clean_run(c, hdr_a)
    r = c.get("/reports/assurance_readiness", params={"run_id": clean}, headers=hdr_a).json()
    assert r["ready"] is True
    assert all(chk["pass"] for chk in r["checks"])

    partial = _partial_run(c, hdr_a)
    rp = c.get("/reports/assurance_readiness", params={"run_id": partial}, headers=hdr_a).json()
    assert rp["ready"] is False
    fails = {chk["check"] for chk in rp["checks"] if not chk["pass"]}
    assert "completeness" in fails and "no_excluded_activities" in fails


def test_unqualified_gated_on_readiness_and_material_findings(env):
    c, hdr_a, hdr_b = env
    # partial run on org B -> unqualified must be refused (isolated from A's clean run,
    # since a run spans ALL of an org's activities)
    partial = _partial_run(c, hdr_b)
    eng = c.post("/assurance/engagements", headers=hdr_b,
                 params={"run_id": partial, "standard": "ISAE_3410", "level": "limited"}).json()["id"]
    r = c.post(f"/assurance/engagements/{eng}/conclude", headers=hdr_b,
               params={"opinion": "unqualified"})
    assert r.status_code == 409
    # qualified is allowed for a partial run
    assert c.post(f"/assurance/engagements/{eng}/conclude", headers=hdr_b,
                  params={"opinion": "qualified"}).status_code == 200

    # clean run (org A) + an OPEN material finding -> unqualified refused until resolved
    clean = _clean_run(c, hdr_a)
    eng2 = c.post("/assurance/engagements", headers=hdr_a,
                  params={"run_id": clean, "standard": "ISO_14064_3", "level": "reasonable"}).json()["id"]
    fid = c.post(f"/assurance/engagements/{eng2}/findings", headers=hdr_a,
                 params={"severity": "material", "description": "factor vintage query"}).json()["id"]
    assert c.post(f"/assurance/engagements/{eng2}/conclude", headers=hdr_a,
                  params={"opinion": "unqualified"}).status_code == 409
    c.post(f"/assurance/findings/{fid}/resolve", headers=hdr_a,
           params={"resolution_note": "vintage confirmed correct"})
    ok = c.post(f"/assurance/engagements/{eng2}/conclude", headers=hdr_a,
                params={"opinion": "unqualified"})
    assert ok.status_code == 200 and ok.json()["opinion"] == "unqualified"


def test_concluded_engagement_rejects_new_findings(env):
    c, hdr_a, _ = env
    clean = _clean_run(c, hdr_a)
    eng = c.post("/assurance/engagements", headers=hdr_a,
                 params={"run_id": clean, "standard": "ISAE_3410", "level": "limited"}).json()["id"]
    c.post(f"/assurance/engagements/{eng}/conclude", headers=hdr_a, params={"opinion": "unqualified"})
    r = c.post(f"/assurance/engagements/{eng}/findings", headers=hdr_a,
               params={"severity": "minor", "description": "late note"})
    assert r.status_code == 409


def test_assuror_token_is_read_only_and_scoped(env):
    c, hdr_a, hdr_b = env
    clean = _clean_run(c, hdr_a)
    eng = c.post("/assurance/engagements", headers=hdr_a,
                 params={"run_id": clean, "standard": "ISSA_5000", "level": "reasonable",
                         "assuror_name": "Example Assurance LLP"}).json()["id"]
    token = c.post(f"/assurance/engagements/{eng}/grant_access", headers=hdr_a).json()["assurance_token"]
    tok_hdr = {"X-Assurance-Token": token}

    # Assuror can read the engagement + lineage with NO org key.
    v = c.get(f"/assurance/engagements/{eng}", headers=tok_hdr)
    assert v.status_code == 200
    assert v.json()["readiness"]["ready"] is True
    lin = c.get(f"/assurance/engagements/{eng}/lineage", headers=tok_hdr)
    assert lin.status_code == 200 and len(lin.json()["line_items"]) >= 1
    # Assuror view omits the owner-only assuror_name field.
    assert "assuror_name" not in v.json()["engagement"]

    # The token is read-only: no write endpoint accepts it (writes need org key).
    assert c.post(f"/assurance/engagements/{eng}/findings",
                  params={"severity": "minor", "description": "x"},
                  headers=tok_hdr).status_code == 422  # missing X-API-Key
    # A wrong token is rejected.
    assert c.get(f"/assurance/engagements/{eng}",
                 headers={"X-Assurance-Token": "wrong"}).status_code == 401


def test_engagement_tenant_isolation(env):
    c, hdr_a, hdr_b = env
    clean = _clean_run(c, hdr_a)
    eng = c.post("/assurance/engagements", headers=hdr_a,
                 params={"run_id": clean, "standard": "ISAE_3410", "level": "limited"}).json()["id"]
    # B cannot read, add findings to, or conclude A's engagement.
    assert c.get(f"/assurance/engagements/{eng}", headers=hdr_b).status_code == 401
    assert c.post(f"/assurance/engagements/{eng}/findings", headers=hdr_b,
                  params={"severity": "minor", "description": "x"}).status_code == 404
    assert c.post(f"/assurance/engagements/{eng}/conclude", headers=hdr_b,
                  params={"opinion": "adverse"}).status_code == 404
    # B cannot create an engagement over A's run.
    assert c.post("/assurance/engagements", headers=hdr_b,
                  params={"run_id": clean, "standard": "ISAE_3410", "level": "limited"}).status_code == 404


# --- Phase 12 verification-panel hardening ---

def test_no_existence_oracle_on_reader_endpoints(env):
    """Nonexistent and unauthorized engagements return the SAME 401 (no id enumeration)."""
    c, hdr_a, _ = env
    clean = _clean_run(c, hdr_a)
    eng = c.post("/assurance/engagements", headers=hdr_a,
                 params={"run_id": clean, "standard": "ISAE_3410", "level": "limited"}).json()["id"]
    # No credentials at all: existing and nonexistent both 401 (not 404).
    assert c.get(f"/assurance/engagements/{eng}").status_code == 401
    assert c.get("/assurance/engagements/999999").status_code == 401
    assert c.get(f"/assurance/engagements/{eng}/lineage").status_code == 401
    assert c.get("/assurance/engagements/999999/lineage").status_code == 401


def test_findings_frozen_after_conclusion(env):
    c, hdr_a, _ = env
    clean = _clean_run(c, hdr_a)
    eng = c.post("/assurance/engagements", headers=hdr_a,
                 params={"run_id": clean, "standard": "ISAE_3410", "level": "reasonable"}).json()["id"]
    fid = c.post(f"/assurance/engagements/{eng}/findings", headers=hdr_a,
                 params={"severity": "material", "description": "open issue"}).json()["id"]
    # Conclude qualified (blocked from unqualified by the open material finding).
    c.post(f"/assurance/engagements/{eng}/conclude", headers=hdr_a, params={"opinion": "qualified"})
    # Resolving the finding AFTER conclusion is refused — the ledger is frozen.
    r = c.post(f"/assurance/findings/{fid}/resolve", headers=hdr_a,
               params={"resolution_note": "sneaky post-hoc edit"})
    assert r.status_code == 409


def test_readiness_snapshot_frozen_at_conclusion(env):
    c, hdr_a, _ = env
    clean = _clean_run(c, hdr_a)
    eng = c.post("/assurance/engagements", headers=hdr_a,
                 params={"run_id": clean, "standard": "ISO_14064_3", "level": "reasonable"}).json()["id"]
    c.post(f"/assurance/engagements/{eng}/conclude", headers=hdr_a, params={"opinion": "unqualified"})
    # Make the run stale (add an activity without recomputing this run).
    csv = ("date,category,subcategory,description,quantity,unit,geo\n"
           "2025-03-01,electricity,,extra,50,kWh,GB\n")
    c.post("/activities/upload_csv", headers=hdr_a,
           files={"file": ("extra.csv", io.BytesIO(csv.encode()), "text/csv")})
    v = c.get(f"/assurance/engagements/{eng}", headers=hdr_a).json()
    # Snapshot readiness (as concluded) still shows ready; drift is flagged.
    assert v["readiness_is_snapshot_at_conclusion"] is True
    assert v["readiness"]["ready"] is True
    assert v["run_changed_since_conclusion"] is True
    assert v["engagement"]["opinion"] == "unqualified"        # frozen opinion intact
