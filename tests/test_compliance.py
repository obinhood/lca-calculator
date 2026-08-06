"""Required-data checklist: field coverage + numeric gap analysis per framework.

The load-bearing property is HONESTY: a field may only count as present if it is genuinely in
the payload, requirements the engine cannot produce must be listed (not dropped), and the
verdict must never read as a compliance opinion.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from fastapi.testclient import TestClient

from app.database import Base
from app import main as main_mod
from app.models import EmissionFactor
from app.reports.compliance import evaluate, _resolve, _present
from app.reports.compliance_requirements import REQUIREMENTS
from app.reports.export import BUILDERS


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
    key = c.post("/organisations", params={"name": "ComplyCo"}).json()["api_key"]
    c.post("/demo/seed", headers={"X-API-Key": key})
    yield c, {"X-API-Key": key}
    main_mod.app.dependency_overrides.clear()


# --- resolver ---

def test_resolver_handles_keys_containing_dots():
    """CDP-style keys like C6.1_scope1 would break a naive split('.')."""
    payload = {"answers": {"C6.1_scope1_gross_tco2e": 12.0}}
    assert _resolve(payload, "answers.C6.1_scope1_gross_tco2e") == 12.0


def test_resolver_missing_path_is_none():
    assert _resolve({"a": {"b": 1}}, "a.c") is None
    assert _resolve({"a": 1}, "a.b.c") is None


def test_empty_values_do_not_count_as_present():
    """An empty list/dict/blank string is NOT a satisfied disclosure."""
    assert _present([]) is False
    assert _present({}) is False
    assert _present("  ") is False
    assert _present(None) is False
    assert _present(0) is True          # a real zero IS a reported figure


# --- field coverage ---

def test_missing_field_is_reported_missing():
    got = evaluate("secr", {"emissions_tco2e": {"scope1": 1.0}})
    # key by FIELD (a single citation can cover several fields, e.g. Scope 1 and Scope 2)
    by_field = {r.get("field"): r["status"] for r in got["requirements"] if r.get("field")}
    assert by_field["emissions_tco2e.scope1"] == "present"
    assert by_field["emissions_tco2e.scope2_location_based"] == "missing"
    assert by_field["energy_use_kwh.total_kwh"] == "missing"
    assert got["data_complete"] is False


def test_preparer_and_assurance_items_are_listed_not_dropped():
    got = evaluate("esrs_e1", {})
    kinds = {r["status"] for r in got["requirements"]}
    assert "preparer_must_supply" in kinds
    assert "requires_independent_assurance" in kinds
    assert got["preparer_items_outstanding"] > 0
    assert got["assurance_items_outstanding"] > 0


def test_verdict_never_claims_compliance():
    got = evaluate("secr", {"energy_use_kwh": {"total_kwh": 1}, "emissions_tco2e":
                            {"scope1": 1, "scope2_location_based": 1},
                            "intensity_ratio": {"x": 1}, "methodology_statement": "m",
                            "coverage": {"coverage_pct": 100.0}})
    assert got["data_complete"] is True
    assert "compliant" not in got["verdict"].lower()
    assert "not a compliance opinion" in got["scope_note"]


# --- numeric gaps: "by how much are we off?" ---

def test_threshold_reports_the_shortfall():
    got = evaluate("secr", {"coverage": {"coverage_pct": 87.0}})
    t = next(t for t in got["thresholds"] if t["metric"] == "Activity-data coverage")
    assert t["status"] == "short"
    assert t["actual"] == 87.0 and t["required"] == 100.0
    assert t["gap"] == pytest.approx(13.0)               # 13 points short, quantified
    assert "13" in t["explanation"]


def test_threshold_over_limit_reports_excess():
    """A <= threshold (data quality: lower is better) reports how far OVER you are."""
    got = evaluate("issb_s2", {"data_quality": {"emissions_weighted_score": 4.2}})
    t = next(t for t in got["thresholds"] if t["metric"] == "Data-quality score")
    assert t["status"] == "short" and t["gap"] == pytest.approx(1.2)


def test_categorical_threshold_reports_the_mismatch():
    got = evaluate("issb_s2", {"run": {"gwp_set": "AR5"}})
    t = next(t for t in got["thresholds"] if t["metric"] == "GWP vintage")
    assert t["status"] == "short" and "AR6" in t["explanation"]


def test_absent_metric_is_not_assessable_not_a_pass():
    got = evaluate("secr", {})
    t = next(t for t in got["thresholds"] if t["metric"] == "Activity-data coverage")
    assert t["status"] == "not_assessable"               # never silently "met"


def test_complete_fields_but_short_threshold_is_not_complete():
    got = evaluate("secr", {"energy_use_kwh": {"total_kwh": 1}, "emissions_tco2e":
                            {"scope1": 1, "scope2_location_based": 1},
                            "intensity_ratio": {"x": 1}, "methodology_statement": "m",
                            "coverage": {"coverage_pct": 50.0}})
    assert got["data_fields_missing"] == 0
    assert got["data_complete"] is False                 # the threshold still fails
    assert "All required data fields are present, but" in got["verdict"]


# --- registry integrity ---

def test_every_framework_with_a_report_has_a_checklist():
    missing = sorted(set(BUILDERS) - set(REQUIREMENTS))
    assert not missing, f"no required-data checklist for: {missing}"


def test_registry_entries_are_well_formed():
    for key, reqs in REQUIREMENTS.items():
        assert reqs, f"{key} has no requirements"
        for r in reqs:
            assert r["ref"] and r["what"]
            assert r["source"] in ("platform", "preparer", "assurance")
            if r["source"] == "platform":
                assert r.get("path"), f"{key}/{r['ref']} is platform-sourced but has no path"


# --- endpoint ---

def test_compliance_endpoint(client):
    c, h = client
    r = c.get("/compliance/secr", headers=h, params={"intensity_denominator": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["assessable"] is True
    assert body["required_data_fields"] > 0
    assert "requirements" in body and "thresholds" in body


def test_compliance_endpoint_unknown_framework_404s(client):
    c, h = client
    assert c.get("/compliance/nope", headers=h).status_code == 404


def test_export_carries_the_checklist(client):
    """CSV/PDF must include the completeness evidence, not just the figures."""
    c, h = client
    csv_text = c.get("/export/secr", headers=h, params={"format": "csv"}).text
    assert "required_data_check" in csv_text


# --- fixes from the standards review (each pins a defect that was found and corrected) ---

def test_platform_house_rules_are_labelled_and_do_not_block_completeness():
    """A data-quality target is OUR guideline, not a number the standard sets. It must be
    labelled as such and must never make a report look non-compliant with the framework."""
    got = evaluate("issb_s2", {
        "ghg_emissions_tco2e": {"scope1_gross": 1, "scope2_location_based_gross": 1,
                                "scope3_gross": 1, "scope2_market_based_information": 1,
                                "scope3_categories_included": [1], "gwp_source": "IPCC AR6"},
        "methodology_statement": "m", "scope1_2_disaggregation_29a_iv": {"x": 1},
        "coverage": {"coverage_pct": 100.0}, "run": {"gwp_set": "AR6"},
        "data_quality": {"emissions_weighted_score": 4.5}})          # worse than our 3.0
    dq = next(t for t in got["thresholds"] if t["metric"] == "Data-quality score")
    assert dq["provenance"] == "platform"
    assert dq["status"] == "short"
    assert got["advisory_flags"] == 1
    assert got["data_complete"] is True          # a house rule cannot fail the framework
    assert "platform guideline" in got["verdict"]


def test_standard_thresholds_are_labelled_standard():
    got = evaluate("issb_s2", {"run": {"gwp_set": "AR6"}})
    gwp = next(t for t in got["thresholds"] if t["metric"] == "GWP vintage")
    assert gwp["provenance"] == "standard"


def test_unassessable_threshold_blocks_completeness():
    """Previously an unevaluable check was excluded from 'short' and so silently allowed a
    'every threshold is met' verdict."""
    got = evaluate("secr", {"energy_use_kwh": {"total_kwh": 1}, "emissions_tco2e":
                            {"scope1": 1, "scope2_location_based": 1},
                            "intensity_ratio": {"x": 1}, "methodology_statement": "m"})
    assert got["data_fields_missing"] == 0
    assert got["thresholds_not_assessable"] == 1
    assert got["data_complete"] is False
    assert "could not be checked" in got["verdict"]


def test_sbti_uses_the_criterion_for_the_targets_own_horizon():
    """A flat 4.2%/yr floor false-failed net-zero targets, which are judged on ~90% absolute."""
    netzero = evaluate("sbti", {"ambition_assessment": {
        "target_type": "net_zero", "meets_minimum": True,
        "criterion": ">= 90% absolute reduction (net-zero)"}})
    t = next(t for t in netzero["thresholds"] if t["metric"].startswith("Target ambition"))
    assert t["status"] == "met"

    weak = evaluate("sbti", {"ambition_assessment": {
        "target_type": "near_term", "meets_minimum": False,
        "criterion": ">= 4.2%/yr linear (near-term 1.5C)"}})
    t2 = next(t for t in weak["thresholds"] if t["metric"].startswith("Target ambition"))
    assert t2["status"] == "short" and "4.2%/yr" in t2["explanation"]


def test_epd_threshold_is_not_a_tautology():
    """required == hard-coded actual made this check unfailable, implying EN 15804 asks for
    one indicator."""
    got = evaluate("epd", {})
    t = next(t for t in got["thresholds"] if "Core impact indicators" in t["metric"])
    assert t["required"] == 13 and t["actual"] == 1
    assert t["status"] == "short" and t["gap"] == pytest.approx(12.0)


def test_secr_citations_reference_the_real_instrument():
    """SECR sits in Sch. 7 Pt. 7A of SI 2008/410 — 'reg. 20A(n)' was fabricated."""
    refs = " ".join(r["ref"] for r in REQUIREMENTS["secr"])
    assert "SI 2008/410 Sch. 7 Pt. 7A" in refs
    assert "reg. 20A" not in refs


def test_issb_paragraph_letters_are_correct():
    """29(b)-(e) are risk/opportunity and capital items; internal carbon price is (f),
    remuneration (g); industry metrics are para 32 — not 29(b)."""
    by_ref = {r["ref"]: r["what"] for r in REQUIREMENTS["issb_s2"]}
    assert "Internal carbon price" in by_ref["IFRS S2 ¶29(f)"]
    assert "remuneration" in by_ref["IFRS S2 ¶29(g)"]
    assert "Industry-based metrics" in by_ref["IFRS S2 ¶32"]


def test_resolver_walks_lists_to_reach_a_nested_disclosure():
    payload = {"pillars": [{"recommended_disclosures": [{"ref": "x"},
                                                        {"ghg_emissions_tco2e": {"s1": 1}}]}]}
    assert _resolve(payload, "pillars.recommended_disclosures.ghg_emissions_tco2e") == {"s1": 1}
