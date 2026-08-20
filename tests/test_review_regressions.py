"""Regressions for the defects an adversarial review found in the shipped batch.

Every test here pins a seam, not a unit. That is deliberate: the suite was 1,462 green
while all of these were live, because each component was correct in isolation and the
defect lived where two of them met — a renderer that re-derived a gate instead of calling
it, a reader that looked for a key the writer nests one level down, a detector that
compared the one field nobody edits.

The recurring shape is a SECOND IMPLEMENTATION of a rule that already exists. So several
of these assert not merely that a renderer refuses, but that it refuses *for the same
reason the reference implementation does* — which is the only version of the assertion a
future divergence cannot slip past.
"""
import json

import pytest

from app.models import (
    Organisation, EmissionFactor, ActivityRecord, ReportingEntity, ReportingPeriod,
    EmissionLineItem, MarketInstrument,
)
from app.services.calc import compute_co2e
from app.services import applicability as A
from app.services.applicability_rules import RULES
from app.services.boundary import boundary_comparable
from app.reports.iso_14064_2 import iso_14064_2_report
from app.reports.ecovadis import ecovadis_readiness
from app.reports.summary import summary, run_factor_sources, coverage

JUSTIFY = ("Baseline = continuation of the pre-retrofit gas boiler at metered pre-project "
           "load; no policy or price driver would have changed it absent the project.")


# --- fixtures ---------------------------------------------------------------------------

def _org(db, name="Group", approach="operational_control"):
    o = Organisation(name=name, consolidation_approach=approach,
                     consolidation_approach_reason="Operational control basis for the group.")
    db.add(o); db.commit(); db.refresh(o)
    return o


def _entity(db, org_id, name="Sub", **kw):
    kw.setdefault("accounting_category", "subsidiary")
    kw.setdefault("in_consolidated_accounting_group", True)
    e = ReportingEntity(organisation_id=org_id, name=name, **kw)
    db.add(e); db.commit(); db.refresh(e)
    return e


def _factor(db, value=0.5, **kw):
    kw.setdefault("source", "T"); kw.setdefault("version", "1")
    kw.setdefault("geography", "GB"); kw.setdefault("year", 2024)
    kw.setdefault("category", "gas"); kw.setdefault("gwp_set", "AR6")
    f = EmissionFactor(subcategory="", unit="kWh", value=value, **kw)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _act(db, org_id, factor_id, kwh=1000.0, entity_id=None, date="2025-06-01"):
    a = ActivityRecord(organisation_id=org_id, date=date, category="gas", subcategory="",
                       description="", quantity=kwh, unit="kWh", geo="GB",
                       factor_id=factor_id, entity_id=entity_id)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _period(db, org_id, label, start, end):
    p = ReportingPeriod(organisation_id=org_id, label=label, start_date=start,
                        end_date=end, frozen=False)
    db.add(p); db.commit(); db.refresh(p)
    return p


def _divested_pair(db, org):
    """Two equal-length runs whose ONLY difference is that an entity left the boundary.

    The parent's own consumption is identical in both years, so every kilogram of the
    apparent 'reduction' is the divestment. The consolidation approach never changes —
    which is precisely why a gate that tests only the approach waves this through.
    """
    f = _factor(db)
    sub = _entity(db, org.id, name="DisposalCo", equity_share_pct=100.0,
                  financial_control=True, operational_control=True)
    parent_act = _act(db, org.id, f.id, kwh=1000.0, entity_id=None, date="2024-06-15")
    _act(db, org.id, f.id, kwh=1000.0, entity_id=sub.id, date="2024-06-15")
    bp = _period(db, org.id, "Baseline FY24", "2024-01-01", "2024-12-31")
    base = compute_co2e(db, org.id, gwp_set="AR6", reporting_period_id=bp.id)

    # Divest: the subsidiary leaves the boundary at the year end and its consumption
    # falls outside the project period. The parent's own load is carried over unchanged,
    # so the whole apparent reduction is the disposal.
    sub.effective_to = "2024-12-31"
    parent_act.date = "2025-06-15"
    db.commit()
    pp = _period(db, org.id, "Project FY25", "2025-01-01", "2025-12-31")
    proj = compute_co2e(db, org.id, gwp_set="AR6", reporting_period_id=pp.id)
    return base, proj


# --- the headline: a divestment must never be published as abatement --------------------

def test_iso_14064_2_refuses_a_divestment_as_a_project_reduction(db):
    """The renderer used to compare consolidation_approach ONLY. A divestment leaves the
    approach untouched, so the departed entity's emissions were published as a
    substantiated project reduction with blockers == [] and disclosure_ready True — the
    engine's own statement that it had tested comparability and the quantification held.
    """
    org = _org(db)
    base, proj = _divested_pair(db, org)

    # The shared detector says these two runs are not comparable...
    ref = boundary_comparable(db, base, proj, label_a="baseline", label_b="project",
                              quantity="the project reduction")
    assert ref is not None, "fixture must actually change the entity population"

    r = iso_14064_2_report(db, org.id, baseline_run_id=base.id, project_run_id=proj.id,
                           baseline_justification=JUSTIFY, leakage_tco2e=0.0)

    # ...so the renderer must too, and it must say it in the SAME words. Asserting on the
    # shared detector's own sentence is what stops a future re-derivation drifting apart.
    assert r["disclosure_ready"] is False
    assert ref in r["blockers"], (
        f"ISO 14064-2 must surface the shared boundary detector's refusal verbatim; "
        f"got {r['blockers']}")


def test_ecovadis_trend_refuses_a_divestment(db):
    """Same hole, second renderer. Its own comment claimed "the same comparability gates"
    as GRI 305-5; GRI applies four and this applied three. The missing one is the
    boundary — the most flattering thing that can happen to an Actions-pillar trend.
    """
    org = _org(db)
    base, curr = _divested_pair(db, org)
    ref = boundary_comparable(db, base, curr, label_a="baseline", label_b="current",
                              quantity="the trend")
    assert ref is not None

    r = ecovadis_readiness(db, org.id, run_id=curr.id, baseline_run_id=base.id)
    assert any(ref in b for b in r["blockers"]), (
        f"EcoVadis trend must carry the boundary gate; got {r['blockers']}")


def test_the_boundary_gate_is_the_shared_detector_not_a_local_copy():
    """The structural assertion. `boundary_difference`'s docstring says "never two
    detectors that can drift apart" — so no renderer may reach for the raw field.

    A renderer comparing `consolidation_approach` itself is re-deriving a rule that
    already exists, and it will be a partial re-derivation: that is exactly how the two
    defects above were born.
    """
    import pathlib
    offenders = []
    for p in sorted(pathlib.Path("app/reports").glob("*.py")):
        src = p.read_text()
        if "consolidation_approach !=" in src or "consolidation_approach or None) !=" in src:
            offenders.append(p.name)
    assert not offenders, (
        f"{offenders} compare consolidation_approach directly instead of calling "
        f"services.boundary.boundary_comparable — a divestment passes such a check")


# --- unknown is never a no: a determined answer must not be thrown away -----------------

def test_declaring_the_parent_territory_too_does_not_destroy_a_determined_answer(db):
    """The sub-national gate fired before the direct hit was tested, so naming BOTH "US"
    and "US-CA" turned a fully-determined California obligation into cannot_determine —
    with a reason ("you have not said whether that includes California") flatly
    contradicted by the profile the caller had just submitted.
    """
    o = Organisation(name="CalCo", annual_turnover=2e9, employees=5000,
                     financials_currency="USD",
                     jurisdictions=json.dumps(["US", "US-CA"]))
    db.add(o); db.commit(); db.refresh(o)

    r = A.evaluate_one("sb253", RULES["sb253"], o)
    assert r["verdict"] != A.CANNOT_DETERMINE, (
        f"US-CA is stated outright; the answer is determined. Got: {r.get('reason')}")

    # And the parent-only case must still be a question, not an exemption.
    o2 = Organisation(name="NatCo", annual_turnover=2e9, employees=5000,
                      financials_currency="USD", jurisdictions=json.dumps(["US"]))
    db.add(o2); db.commit(); db.refresh(o2)
    r2 = A.evaluate_one("sb253", RULES["sb253"], o2)
    assert r2["verdict"] == A.CANNOT_DETERMINE


# --- the assurance file must not publish null for what the run determined ---------------

def test_evidence_pack_publishes_the_method_shares_it_computed(db):
    """_s8 read primary/spend share from the top level of the summary payload; summary
    nests both under "method_split". The pack therefore published null for two figures
    the run had determined — in the file an assuror reads to judge how much of the
    inventory rests on primary data.
    """
    from app.services.evidence_pack import build_evidence_pack
    org = _org(db, name="PackOrg")
    _act(db, org.id, _factor(db).id, kwh=1000.0)
    run = compute_co2e(db, org.id, gwp_set="AR6")

    s = summary(db, run_id=run.id)
    assert s["method_split"]["primary_data_share_pct"] is not None

    pack = build_evidence_pack(db, run)
    dq = pack["sections"]["8_data_quality_and_uncertainty"]
    assert dq["primary_data_share_pct"] == s["method_split"]["primary_data_share_pct"]
    assert dq["spend_based_share_pct"] == s["method_split"]["spend_based_share_pct"]


# --- factor identity is frozen, and drift in it is visible -------------------------------

def test_factor_provenance_is_frozen_so_a_filed_methodology_cannot_be_rewritten(db):
    """Only `factor_value` was frozen. The methodology statement every renderer publishes
    ("EF sources: DEFRA v2024") joined the LIVE source/version, so re-labelling a factor
    in place silently rewrote that sentence on runs already filed — and the drift
    detector, which compared only `value`, could not see it.
    """
    org = _org(db, name="ProvOrg")
    f = _factor(db, source="DEFRA", version="2024")
    _act(db, org.id, f.id, kwh=1000.0)
    run = compute_co2e(db, org.id, gwp_set="AR6")

    assert run_factor_sources(db, run) == ["DEFRA v2024"]

    # Re-label in place. The VALUE is untouched, so the totals still reproduce.
    f.source, f.version = "DEFRA-RENAMED", "2025"
    db.commit()

    assert run_factor_sources(db, run) == ["DEFRA v2024"], (
        "a filed run must keep the methodology statement it was filed with")

    warn = " ".join(coverage(db, run).get("factor_drift") or [])
    assert "source" in warn and "version" in warn, (
        f"identity drift must be reported even though the value is unchanged; got {warn!r}")


def test_a_legacy_run_without_frozen_provenance_is_marked_not_blocked(db):
    """Anti-cliff: a run computed before provenance was frozen is never back-filled and
    never blocked. But it must say that its sources were read from the current catalog,
    because a reader cannot otherwise tell a guaranteed provenance from one that merely
    has not been edited yet.
    """
    org = _org(db, name="LegacyOrg")
    f = _factor(db, source="DEFRA", version="2024")
    _act(db, org.id, f.id, kwh=1000.0)
    run = compute_co2e(db, org.id, gwp_set="AR6")

    # Strip the frozen block, as a run computed before this existed would be.
    for li in db.query(EmissionLineItem).filter(EmissionLineItem.run_id == run.id).all():
        d = json.loads(li.details)
        d.pop("factor_provenance", None)
        li.details = json.dumps(d)
    db.commit()

    out = run_factor_sources(db, run)
    assert len(out) == 1 and out[0].startswith("DEFRA v2024")
    assert "current catalog" in out[0], (
        f"a live-derived source must disclose that it is live-derived; got {out!r}")


# --- an exclusion nobody could see -------------------------------------------------------

def test_summary_surfaces_instruments_dropped_on_a_gwp_label_mismatch(db):
    """calc drops a contractual instrument WHOLE (its covered MWh, not merely its rate)
    when the instrument's gwp_set label differs from the run's, and froze that exclusion
    onto every market line. No renderer read it — so a fully-REC'd org could publish a
    market-based total identical to its location-based one with nothing saying why.
    """
    org = _org(db, name="RecOrg")
    f = _factor(db, category="electricity", value=0.2)
    a = ActivityRecord(organisation_id=org.id, date="2025-06-01", category="electricity",
                       subcategory="", description="", quantity=1000.0, unit="kWh",
                       geo="GB", factor_id=f.id)
    db.add(a); db.commit()
    inst = MarketInstrument(organisation_id=org.id, instrument_type="rec",
                            coverage_kwh=1000.0, gwp_set="AR5", kg_co2e_per_kwh=0.0,
                            market="GB")
    db.add(inst); db.commit()

    run = compute_co2e(db, org.id, gwp_set="AR6")
    s2 = summary(db, run_id=run.id)["scope2"]
    assert "instruments_excluded_by_gwp_vintage" in s2
    assert s2["instruments_excluded_by_gwp_vintage"], (
        "an instrument dropped for a GWP-label mismatch must be visible in the payload, "
        "or the org cannot tell why its RECs did nothing")


# --- the fail-closed verdict must survive the trip to the artefact a human files --------

def test_every_whole_document_readiness_key_drives_the_pdf_banner():
    """to_pdf probed three keys. Four frameworks minted their own, so a NOT-ready ESOS,
    ETS MRV, CBAM or EcoVadis document printed the neutral grey "INFORMATION REPORT"
    instead of the red DRAFT stamp: the gate held in the JSON and was lost on the way to
    the artefact anyone actually reads.
    """
    import inspect
    from app.reports import export
    src = inspect.getsource(export.to_pdf)
    for key in ("report_ready", "declaration_ready", "assessment_ready", "submission_ready"):
        assert key in src, f"to_pdf cannot stamp DRAFT for a payload keyed {key!r}"
    # Sub-verdicts must NOT drive a whole-document banner.
    assert ".endswith(" not in src.split("blockers =")[0], (
        "a suffix rule would promote tcfd/pef SUB-verdicts to whole-document verdicts")


def test_every_registry_category_is_renderable():
    """Two entries carried a category string absent from CATEGORIES, so their cards could
    never render in the Reports catalogue — built, shipped, unreachable.
    """
    import pathlib, re
    src = pathlib.Path("frontend/src/frameworks.ts").read_text()
    cats = set(re.findall(r'"([^"]+)",', src.split("export const CATEGORIES = [")[1]
                          .split("] as const;")[0]))
    used = set(re.findall(r'category: "([^"]+)"', src))
    assert used <= cats, f"unreachable categories: {sorted(used - cats)}"
