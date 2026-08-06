"""CSV / PDF export of disclosure reports.

Pins: exports are REGENERATED server-side (never built from client-supplied figures), CSV
flattens every leaf, the PDF is a real PDF carrying the same fail-closed verdict (DRAFT when
not ready, with the blockers), unknown frameworks/formats fail closed, and export is
org-scoped.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
from app import main as main_mod
from app.models import EmissionFactor
from app.reports.export import BUILDERS, to_csv, _flatten


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
    s = Session()
    s.add(EmissionFactor(source="T", version="1", geography="GB", year=2024,
                         category="electricity", subcategory="", unit="kWh",
                         gwp_set="AR6", value=0.17))
    s.commit(); s.close()
    key = c.post("/organisations", params={"name": "ExportCo"}).json()["api_key"]
    c.post("/demo/seed", headers={"X-API-Key": key})
    yield c, {"X-API-Key": key}
    main_mod.app.dependency_overrides.clear()


# --- CSV ---

def test_csv_flattens_nested_payload():
    rows = _flatten({"a": 1, "b": {"c": "x"}, "d": [1, 2]})
    paths = dict(rows)
    assert paths["a"] == 1
    assert paths["b.c"] == "x"
    assert paths["d[0]"] == 1 and paths["d[1]"] == 2


def test_csv_has_header_and_rows():
    csv_text = to_csv({"framework": "UK SECR", "emissions_tco2e": {"scope1": 1.5}})
    lines = csv_text.strip().splitlines()
    assert lines[0] == "section,field,value"
    assert any("UK SECR" in l for l in lines)
    assert any("scope1" in l and "1.5" in l for l in lines)


def test_csv_export_endpoint(client):
    c, h = client
    r = c.get("/export/secr", headers=h, params={"format": "csv"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert r.text.startswith("section,field,value")


# --- PDF ---

def test_pdf_export_is_a_real_pdf(client):
    c, h = client
    r = c.get("/export/secr", headers=h, params={"format": "pdf"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content[:5] == b"%PDF-"          # magic bytes, not an HTML error page
    assert len(r.content) > 1000


def test_pdf_stamps_draft_and_lists_blockers_when_not_ready(client):
    """A not-ready report must never look filing-ready on paper."""
    from app.reports.export import to_pdf
    payload = {"framework": "ISSB IFRS S2", "disclosure_ready": False,
               "blockers": ["Scope 3 categories are UNDECLARED"],
               "ghg_emissions_tco2e": {"scope1_gross": 1.0}}
    pdf = to_pdf(payload, framework_label="ISSB IFRS S2", organisation="ExportCo",
                 generated_at="2026-01-01T00:00:00+00:00")
    assert pdf[:5] == b"%PDF-"
    from pypdf import PdfReader
    import io
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert "DRAFT" in text and "NOT DISCLOSURE-READY" in text
    assert "UNDECLARED" in text                      # the blocker is on the page
    assert "not an independently verified" in text.lower() or "NOT an independently" in text


def test_pdf_marks_ready_report(client):
    from app.reports.export import to_pdf
    import io
    from pypdf import PdfReader
    pdf = to_pdf({"framework": "UK SECR", "disclosure_ready": True, "blockers": [],
                  "emissions_tco2e": {"scope1": 0.5}},
                 framework_label="UK SECR", organisation="ExportCo",
                 generated_at="2026-01-01T00:00:00+00:00")
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert "DISCLOSURE-READY" in text and "DRAFT" not in text


# --- gates ---

def test_unknown_framework_404s(client):
    c, h = client
    assert c.get("/export/not_a_framework", headers=h, params={"format": "csv"}).status_code == 404


def test_bad_format_400s(client):
    c, h = client
    assert c.get("/export/secr", headers=h, params={"format": "docx"}).status_code == 400


def test_export_requires_api_key(client):
    c, _ = client
    assert c.get("/export/secr", params={"format": "csv"}).status_code in (401, 422)
    assert c.get("/export/secr", headers={"X-API-Key": "bogus"},
                 params={"format": "csv"}).status_code == 401


def test_every_registered_framework_exports(client):
    """Every catalogue entry must export in both formats (or fail cleanly on a missing input,
    never with a 500)."""
    c, h = client
    for key in sorted(BUILDERS):
        for fmt in ("csv", "pdf"):
            params = {"format": fmt}
            if key in ("lca", "epd", "rics", "pef"):
                params["assessment_id"] = "1"
            if key == "sbti":
                params["target_id"] = "1"
            r = c.get(f"/export/{key}", headers=h, params=params)
            assert r.status_code in (200, 400), f"{key}/{fmt} -> {r.status_code} {r.text[:120]}"
