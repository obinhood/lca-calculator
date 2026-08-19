"""ISAE 3410 sunset and ISSA 5000 migration.

The defect this closes: `compliance_requirements.py` cited ISAE 3410 paragraph
numbers as live obligations with no end date, so from 15 December 2026 the platform
would state a WITHDRAWN standard as applicable to every engagement.

The property that matters most is the one that is easy to get wrong: applicability
follows the PERIOD BEING ASSURED, never today's date. Keying it off the clock would
silently restate the applicable standard of every historical engagement the instant
the cutoff passed.
"""
import json

import pytest

from app.models import (
    ActivityRecord, CalculationRun, EmissionFactor, Organisation, ReportingPeriod,
)
from app.reports.compliance import evaluate
from app.reports.compliance_requirements import REQUIREMENTS, requirements_for
from app.services.assurance import readiness_assessment
from app.services.assurance_standards import (
    ISSA_5000_EFFECTIVE_FROM, VALID_STANDARDS, applicable_standards,
    standard_permitted, run_period_start,
)
from app.services.calc import compute_co2e


# --- the date rule ----------------------------------------------------------

def test_period_before_the_cutoff_is_governed_by_isae_3410():
    v = applicable_standards("2025-01-01")
    assert v["determinable"] is True
    assert "ISAE_3410" in v["applicable"]
    assert v["withdrawn"] == []


def test_period_on_or_after_the_cutoff_drops_isae_3410():
    for start in (ISSA_5000_EFFECTIVE_FROM, "2027-01-01", "2030-06-30"):
        v = applicable_standards(start)
        assert v["determinable"] is True, start
        assert "ISSA_5000" in v["applicable"], start
        assert v["withdrawn"] == ["ISAE_3410"], start
        assert "ISAE_3410" not in v["applicable"], start


def test_the_boundary_date_itself_is_on_the_issa_side():
    """'periods beginning ON OR AFTER 15 December 2026' — the boundary day is
    inclusive, and an off-by-one here silently applies a withdrawn standard."""
    assert applicable_standards("2026-12-14")["withdrawn"] == []
    assert applicable_standards("2026-12-15")["withdrawn"] == ["ISAE_3410"]


def test_iso_14064_3_is_never_withdrawn():
    """A different standard-setter entirely — the IAASB withdrawal does not touch it."""
    for start in ("2020-01-01", "2026-12-15", "2099-01-01", None):
        assert "ISO_14064_3" in applicable_standards(start)["applicable"], start


def test_early_application_of_issa_5000_is_always_available():
    assert "ISSA_5000" in applicable_standards("2020-01-01")["applicable"]
    assert standard_permitted("ISSA_5000", "2020-01-01")["permitted"] is True


# --- unknown period is cannot_determine, not a guess ------------------------

@pytest.mark.parametrize("bad", [None, "", "not-a-date", "2026", "15/12/2026", "abcd-ef-gh"])
def test_unusable_period_is_undeterminable_not_defaulted(bad):
    v = applicable_standards(bad)
    assert v["determinable"] is False, bad
    # Neither IAASB standard is asserted as applicable, and BOTH conditions are
    # published so the reader can settle it once the period is known.
    assert v["applicable"] == ["ISO_14064_3"], bad
    assert set(v["conditional"]) == {"ISAE_3410", "ISSA_5000"}, bad


def test_undeterminable_permits_isae_3410_but_warns(db):
    """Refusing every non-period-scoped run would be a much larger error than the
    one being prevented, so an unknown period warns instead of blocking."""
    v = standard_permitted("ISAE_3410", None)
    assert v["permitted"] is True
    assert "withdrawn" in v["warning"]
    assert ISSA_5000_EFFECTIVE_FROM in v["warning"]


def test_isae_3410_is_refused_for_a_period_it_cannot_govern():
    v = standard_permitted("ISAE_3410", "2027-04-01")
    assert v["permitted"] is False
    assert "ISSA_5000" in v["reason"]
    assert v["warning"] is None


def test_unknown_standard_is_refused():
    v = standard_permitted("ISAE_9999", "2025-01-01")
    assert v["permitted"] is False
    assert "unknown standard" in v["reason"]


# --- requirement rows resolve by period -------------------------------------

def _refs(rows):
    return " | ".join(r["ref"] for r in rows)


def _eval_refs(out):
    """evaluate() names the resolved rows 'requirements'."""
    return _refs(out["requirements"])


def test_base_list_no_longer_hard_codes_a_withdrawn_standard():
    """REGRESSION: the static registry cited 'ISAE 3410 ¶17' and '¶69' with no end
    date, which is the defect. The base list must now be period-independent."""
    assert "ISAE 3410" not in _refs(REQUIREMENTS["assurance_readiness"])


def test_pre_cutoff_period_gets_isae_3410_rows():
    rows = requirements_for("assurance_readiness", period_start="2025-01-01")
    refs = _refs(rows)
    assert "ISAE 3410" in refs
    assert "ISSA 5000" not in refs


def test_post_cutoff_period_gets_issa_5000_rows():
    rows = requirements_for("assurance_readiness", period_start="2027-01-01")
    refs = _refs(rows)
    assert "ISSA 5000" in refs
    assert "ISAE 3410" not in refs


def test_unknown_period_lists_both_with_their_conditions():
    rows = requirements_for("assurance_readiness", period_start=None)
    refs = _refs(rows)
    assert "ISAE 3410" in refs and "ISSA 5000" in refs
    # Every conditional row states WHICH periods select it, so neither reads as
    # unconditionally applicable.
    for r in rows:
        if "ISAE 3410" in r["ref"] or "ISSA 5000" in r["ref"]:
            assert "beginning" in r["ref"], r["ref"]


def test_every_resolution_still_carries_the_platform_row():
    for start in (None, "2025-01-01", "2027-01-01"):
        rows = requirements_for("assurance_readiness", period_start=start)
        assert any(r["source"] == "platform" for r in rows), start
        assert any(r["source"] == "assurance" for r in rows), start


def test_other_frameworks_are_returned_unchanged():
    for key in ("secr", "esrs_e1", "cbam", "pcaf"):
        assert requirements_for(key, period_start="2027-01-01") == REQUIREMENTS[key], key
    assert requirements_for("no_such_framework") is None


# --- wired end to end -------------------------------------------------------

def _run_for_period(db, start="2025-01-01", end="2025-12-31"):
    org = Organisation(name=f"Org{start}")
    db.add(org); db.commit(); db.refresh(org)
    p = ReportingPeriod(organisation_id=org.id, label=f"FY{start[:4]}",
                        start_date=start, end_date=end)
    db.add(p); db.commit(); db.refresh(p)
    f = EmissionFactor(source="TEST", version="1", geography="GB", year=2024,
                       category="electricity", subcategory="", unit="kWh",
                       gwp_set="AR6", value=0.2)
    db.add(f); db.commit(); db.refresh(f)
    db.add(ActivityRecord(organisation_id=org.id, date=start, category="electricity",
                          subcategory="", description="", quantity=1000.0, unit="kWh",
                          geo="GB", factor_id=f.id, mapping_basis="exact"))
    db.commit()
    return org, p, compute_co2e(db, org.id, reporting_period_id=p.id)


def test_run_period_start_reads_the_frozen_period_link(db):
    _, p, run = _run_for_period(db, start="2027-03-01", end="2028-02-29")
    assert run_period_start(db, run) == "2027-03-01"


def test_run_without_a_period_reports_unknown_not_a_default(db):
    org = Organisation(name="NoPeriod")
    db.add(org); db.commit(); db.refresh(org)
    run = CalculationRun(organisation_id=org.id, status="complete", total_co2e=0.0)
    db.add(run); db.commit(); db.refresh(run)
    assert run_period_start(db, run) is None


def test_readiness_report_names_the_applicable_standard(db):
    _, _, pre = _run_for_period(db, start="2025-01-01", end="2025-12-31")
    _, _, post = _run_for_period(db, start="2027-01-01", end="2027-12-31")

    a = readiness_assessment(db, pre)["assurance_standard"]
    b = readiness_assessment(db, post)["assurance_standard"]
    assert "ISAE_3410" in a["applicable"] and a["withdrawn"] == []
    assert b["withdrawn"] == ["ISAE_3410"] and "ISSA_5000" in b["applicable"]


def test_compliance_checklist_follows_the_payload_period(db):
    _, _, pre = _run_for_period(db, start="2025-01-01", end="2025-12-31")
    _, _, post = _run_for_period(db, start="2027-01-01", end="2027-12-31")

    pre_refs = _eval_refs(evaluate("assurance_readiness", readiness_assessment(db, pre)))
    post_refs = _eval_refs(evaluate("assurance_readiness", readiness_assessment(db, post)))
    assert "ISAE 3410" in pre_refs and "ISSA 5000" not in pre_refs
    assert "ISSA 5000" in post_refs and "ISAE 3410" not in post_refs


def test_evaluate_on_a_payload_without_a_period_lists_both(db):
    refs = _eval_refs(evaluate("assurance_readiness", {"ready": True}))
    assert "ISAE 3410" in refs and "ISSA 5000" in refs


# --- guidance ---------------------------------------------------------------

def test_guidance_states_the_withdrawal_and_the_effective_date():
    from app.reports.framework_guidance import FRAMEWORKS
    isae = " ".join(FRAMEWORKS["isae_3410"]["key_points"]) + FRAMEWORKS["isae_3410"]["applies_to"]
    issa = " ".join(FRAMEWORKS["issa_5000"]["key_points"]) + FRAMEWORKS["issa_5000"]["applies_to"]
    assert "WITHDRAWN" in isae and ISSA_5000_EFFECTIVE_FROM in isae
    assert ISSA_5000_EFFECTIVE_FROM in issa
    assert "Early application" in issa or "early application" in issa


# --- endpoint ---------------------------------------------------------------

@pytest.fixture
def env():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from fastapi.testclient import TestClient
    from app.database import Base
    from app import main as main_mod

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine)

    def override_get_db():
        s = TestingSession()
        try:
            yield s
        finally:
            s.close()

    main_mod.app.dependency_overrides[main_mod.get_db] = override_get_db
    client = TestClient(main_mod.app)
    key = client.post("/organisations", params={"name": "A"}).json()["api_key"]
    hdr = {"X-API-Key": key}

    seed = TestingSession()
    org = seed.query(Organisation).filter(Organisation.name == "A").one()
    f = EmissionFactor(source="TEST", version="1", geography="GB", year=2024,
                       category="electricity", subcategory="", unit="kWh",
                       gwp_set="AR6", value=0.2)
    seed.add(f); seed.commit(); seed.refresh(f)

    runs = {}
    for tag, start, end in (("pre", "2025-01-01", "2025-12-31"),
                            ("post", "2027-01-01", "2027-12-31")):
        p = ReportingPeriod(organisation_id=org.id, label=tag,
                            start_date=start, end_date=end)
        seed.add(p); seed.commit(); seed.refresh(p)
        seed.add(ActivityRecord(organisation_id=org.id, date=start, category="electricity",
                                subcategory="", description="", quantity=1000.0, unit="kWh",
                                geo="GB", factor_id=f.id, mapping_basis="exact"))
        seed.commit()
        runs[tag] = compute_co2e(seed, org.id, reporting_period_id=p.id).id
    no_period = CalculationRun(organisation_id=org.id, status="complete", total_co2e=1.0)
    seed.add(no_period); seed.commit(); seed.refresh(no_period)
    runs["none"] = no_period.id
    seed.close()

    yield client, hdr, runs
    main_mod.app.dependency_overrides.clear()


def test_endpoint_refuses_isae_3410_over_a_post_cutoff_period(env):
    client, hdr, runs = env
    r = client.post("/assurance/engagements", headers=hdr, params={
        "run_id": runs["post"], "standard": "ISAE_3410", "level": "limited"})
    assert r.status_code == 400
    assert "withdrawn" in r.json()["detail"]


def test_endpoint_allows_isae_3410_over_a_pre_cutoff_period(env):
    client, hdr, runs = env
    r = client.post("/assurance/engagements", headers=hdr, params={
        "run_id": runs["pre"], "standard": "ISAE_3410", "level": "limited"})
    assert r.status_code == 200
    assert "warning" not in r.json()


def test_endpoint_allows_issa_5000_over_either_period(env):
    client, hdr, runs = env
    for tag in ("pre", "post"):
        r = client.post("/assurance/engagements", headers=hdr, params={
            "run_id": runs[tag], "standard": "ISSA_5000", "level": "limited"})
        assert r.status_code == 200, tag


def test_endpoint_warns_rather_than_blocks_when_the_period_is_unknown(env):
    client, hdr, runs = env
    r = client.post("/assurance/engagements", headers=hdr, params={
        "run_id": runs["none"], "standard": "ISAE_3410", "level": "limited"})
    assert r.status_code == 200
    assert "withdrawn" in r.json()["warning"]


def test_iso_14064_3_is_accepted_for_any_period(env):
    client, hdr, runs = env
    for tag in ("pre", "post", "none"):
        r = client.post("/assurance/engagements", headers=hdr, params={
            "run_id": runs[tag], "standard": "ISO_14064_3", "level": "reasonable"})
        assert r.status_code == 200, tag
        assert "warning" not in r.json(), tag


def test_readiness_endpoint_publishes_the_standard_verdict(env):
    client, hdr, runs = env
    body = client.get("/reports/assurance_readiness",
                      params={"run_id": runs["post"]}, headers=hdr).json()
    assert body["assurance_standard"]["withdrawn"] == ["ISAE_3410"]
    assert body["assurance_standard"]["period_start"] == "2027-01-01"
