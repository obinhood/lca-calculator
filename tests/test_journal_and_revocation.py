"""Three promises that had nothing behind them.

Each of these was asserted in prose — a declared action list, a docstring, an endpoint
description — while the code did none of it. That is the defect class this codebase
treats as worse than a missing feature, because only the prose reaches the auditor.
"""
import json

import pytest

from app.models import (
    Organisation, EmissionFactor, ActivityRecord, MappingAuditEvent, ProductFootprint,
    PactClient, PactToken,
)
from app.services.resolver import auto_map_activity
from app.services.mapping_audit import ACTIONS
from app.services import pact_host, pact_store


# --- the auto-bind path writes the actions it declares -----------------------------------

def _org(db, name):
    o = Organisation(name=name); db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category="electricity", geo="GB"):
    f = EmissionFactor(source="T", version="1", geography=geo, year=2024,
                       category=category, subcategory="", unit="kWh", gwp_set="AR6",
                       value=0.17)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org_id, category="electricity", geo="GB"):
    a = ActivityRecord(organisation_id=org_id, date="2025-06-01", category=category,
                       subcategory="", description="", quantity=100.0, unit="kWh",
                       geo=geo)
    db.add(a); db.commit(); db.refresh(a)
    return a


def _events(db, org_id):
    return db.query(MappingAuditEvent).filter(
        MappingAuditEvent.organisation_id == org_id).all()


def test_an_automatically_bound_activity_is_journalled(db):
    """An inventory bound entirely by auto_map_activity produced a journal with ZERO
    entries — while the evidence pack reported the override-log gap as CLOSED over it."""
    org = _org(db, "JournalOrg")
    _factor(db)
    a = _activity(db, org.id)

    status = auto_map_activity(db, a)
    db.commit()

    assert status == "auto" and a.factor_id is not None
    evs = _events(db, org.id)
    assert len(evs) == 1, (
        "an automatic binding is still a binding decision, and it is the one an assuror "
        "is most likely to want to see")
    assert evs[0].action == "auto_mapped"
    assert evs[0].activity_id == a.id


def test_a_no_op_re_run_does_not_repeat_itself_in_the_journal(db):
    """auto_map_activity is re-run over every unmapped activity on each upload. An
    append-only log that repeats on every no-op stops being readable."""
    org = _org(db, "JournalOrg2")
    _factor(db)
    a = _activity(db, org.id)
    auto_map_activity(db, a); db.commit()
    auto_map_activity(db, a); db.commit()
    auto_map_activity(db, a); db.commit()
    assert len(_events(db, org.id)) == 1


def test_every_declared_action_is_reachable():
    """ACTIONS listed auto_mapped, suggested and unmapped; nothing wrote any of them."""
    import pathlib
    src = "".join(p.read_text() for p in pathlib.Path("app").rglob("*.py"))
    for action in ACTIONS:
        assert src.count(f'"{action}"') >= 2, (
            f"{action!r} is declared in mapping_audit.ACTIONS but nothing records it — "
            f"a declared action nothing writes is a promise of an audit trail that does "
            f"not exist")


# --- PACT: supersession is checked in both directions ------------------------------------

def _pf_doc(pf_id: str, preceding=None):
    """A conforming v3 document — same shape tests/test_pact.py validates against."""
    d = {
        "id": pf_id,
        "specVersion": "3.0.3",
        "created": "2027-02-01T10:00:00Z",
        "status": "Active",
        "companyName": "Acme Chemicals GmbH",
        "companyIds": ["urn:pact:company:customcode:buyer-assigned:acme"],
        "productDescription": "Polypropylene homopolymer, injection moulding grade",
        "productIds": ["urn:pact:product:customcode:buyer-assigned:PP-1234"],
        "productNameCompany": "AcmePP 1234",
        "pcf": {
            "declaredUnitOfMeasurement": "kilogram",
            "declaredUnitAmount": "10",
            "productMassPerDeclaredUnit": "10",
            "referencePeriodStart": "2026-01-01T00:00:00Z",
            "referencePeriodEnd": "2027-01-01T00:00:00Z",
            "pcfExcludingBiogenicUptake": "42.0",
            "pcfIncludingBiogenicUptake": "40.5",
            "fossilCarbonContent": "8.5",
            "fossilGhgEmissions": "41.2",
            "packagingEmissionsIncluded": False,
            "exemptedEmissionsPercent": "1.5",
            "ipccCharacterizationFactors": ["AR6"],
            "crossSectoralStandards": ["ISO14067", "PACT Methodology v3.0"],
            "primaryDataShare": "62.5",
            "geographyCountry": "DE",
            "dqi": {"technologicalDQR": "2.0", "geographicalDQR": "1.5",
                    "temporalDQR": "2.5"},
        },
    }
    if preceding:
        d["precedingPfIds"] = preceding
    return d


def test_a_footprint_that_arrives_already_superseded_is_stored_deprecated(db):
    """Supersession was walked in ONE direction: the incoming document's precedingPfIds.

    An out-of-order import — the correction arriving before the document it corrects,
    which is exactly what a backfill or a replayed feed does — left the superseded
    footprint Active and materialisable as a best-pedigree supplier_specific factor.
    """
    org = _org(db, "PactOrg")
    new_id = "3893bb5d-da16-4dc1-9185-11d97476c254"
    old_id = "7a1e0c9d-2b34-4f56-8a90-1c2d3e4f5a6b"

    # The CORRECTION arrives first, naming the document it replaces.
    r1 = pact_store.import_footprint(db, org.id, _pf_doc(new_id, preceding=[old_id]))
    assert r1["stored"] is True, r1.get("errors")

    # ...then the document it corrects turns up.
    r2 = pact_store.import_footprint(db, org.id, _pf_doc(old_id))
    assert r2["stored"] is True, r2.get("errors")
    assert r2["deprecated_on_arrival_by"] == [new_id], (
        "a footprint already held declares this one among those it replaces; it arrived "
        "already superseded")

    held = db.query(ProductFootprint).filter(
        ProductFootprint.organisation_id == org.id,
        ProductFootprint.pf_id == old_id).first()
    assert held.status == "Deprecated", (
        "a figure its own author has withdrawn must not be able to price next year's "
        "inventory")


# --- PACT: revocation is a delete, not a hope --------------------------------------------

def test_revoking_a_client_kills_its_live_tokens(db):
    """models.py says 'revocation is a delete rather than a hope' and the endpoint
    promised a partner is revocable. PactClient.revoked was written by nothing, read by
    nothing, and no route existed."""
    org = _org(db, "RevokeOrg")
    created = pact_host.create_client(db, org.id, "PartnerCo")
    cid, secret = created["client_id"], created["client_secret"]

    issued = pact_host.issue_token(db, cid, secret)
    assert issued["ok"] is True, issued
    token = issued["access_token"]
    assert pact_host.resolve_token(db, f"Bearer {token}")["ok"] is True

    out = pact_host.revoke_client(db, org.id, cid)
    assert out["revoked"] is True and out["tokens_invalidated"] >= 1

    after = pact_host.resolve_token(db, f"Bearer {token}")
    assert after["ok"] is False, (
        "a bearer token issued before revocation would otherwise stay valid for the whole "
        "of its expiry window — the exact window revocation exists to close")
    assert pact_host.issue_token(db, cid, secret)["ok"] is False


def test_one_tenant_cannot_revoke_another_tenants_partner(db):
    a, b = _org(db, "TenantA"), _org(db, "TenantB")
    created = pact_host.create_client(db, a.id, "PartnerOfA")
    out = pact_host.revoke_client(db, b.id, created["client_id"])
    assert out["revoked"] is False
    assert pact_host.issue_token(db, created["client_id"],
                                 created["client_secret"])["ok"] is True
