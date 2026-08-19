"""Turning a supplier's PACT footprint into primary data in our own inventory.

The design claim under test: materialising a PCF as an ordinary EmissionFactor
with method_type='supplier_specific' makes every existing mechanism apply to it
unchanged — the pedigree reliability indicator, the narrowed Monte Carlo interval,
the primary-data share — with no special case in the calculation engine.

The end-to-end test at the foot of this file is the one that matters: it measures
the uncertainty band before and after substitution and asserts it actually
narrowed. Everything else guards a refusal.
"""
import json

import pytest

from app.models import (
    ActivityRecord, EmissionFactor, Organisation, ProductFootprint,
)
from app.services.calc import compute_co2e
from app.services.pact_factor import (
    DECLARED_UNIT_MAP, PACT_FACTOR_SOURCE, bind_activities, materialise,
    materialisation_verdict,
)
from app.services.pact_store import import_footprint
from app.services.uncertainty import propagate


_SEQ = [0]


def _doc(**over) -> dict:
    _SEQ[0] += 1
    d = {
        "id": f"{_SEQ[0]:08d}-da16-4dc1-9185-11d97476c254",
        "specVersion": "3.0.3",
        "created": "2027-02-01T10:00:00Z",
        "status": "Active",
        "companyName": "Acme Chemicals GmbH",
        "companyIds": ["urn:pact:company:customcode:buyer-assigned:acme"],
        "productDescription": "Polypropylene homopolymer",
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
            "crossSectoralStandards": ["ISO14067"],
            "primaryDataShare": "62.5",
            "geographyCountry": "DE",
            "dqi": {"technologicalDQR": "2.0", "geographicalDQR": "1.5",
                    "temporalDQR": "2.5"},
        },
    }
    for k, v in over.items():
        if k == "pcf":
            d["pcf"].update(v)
        else:
            d[k] = v
    return d


def _org(db, name=None):
    _SEQ[0] += 1
    o = Organisation(name=name or f"FactorOrg{_SEQ[0]}")
    db.add(o); db.commit(); db.refresh(o)
    return o


def _held(db, org, **over):
    r = import_footprint(db, org.id, _doc(**over))
    assert r["stored"] is True, r
    return db.get(ProductFootprint, r["id"])


# --- the happy path ----------------------------------------------------------

def test_a_held_footprint_becomes_a_supplier_specific_factor(db):
    org = _org(db)
    row = _held(db, org)
    r = materialise(db, row)
    assert r["materialised"] is True
    f = db.get(EmissionFactor, r["factor_id"])
    assert f.source == PACT_FACTOR_SOURCE
    assert f.version == row.pf_id            # traceable to the exact PCF
    assert f.method_type == "supplier_specific"
    assert f.lca_boundary == "cradle_to_gate"
    assert f.unit == "kg"
    assert f.value == pytest.approx(4.2)     # 42 over declaredUnitAmount 10
    assert f.gwp_set == "AR6"
    assert f.year == 2027                    # reference period END
    assert f.geography == "DE"


def test_the_factor_value_is_per_declared_unit_not_the_quoted_total(db):
    """42 kgCO2e quoted against a declaredUnitAmount of 10 is 4.2 per kilogram.
    Materialising 42 would overstate every bound line tenfold."""
    org = _org(db)
    f = db.get(EmissionFactor, materialise(db, _held(db, org))["factor_id"])
    assert f.value == pytest.approx(4.2)
    assert f.value != pytest.approx(42.0)


def test_materialising_twice_returns_the_same_factor(db):
    org = _org(db)
    row = _held(db, org)
    first = materialise(db, row)
    second = materialise(db, row)
    assert second["materialised"] is False
    assert second["idempotent"] is True
    assert second["factor_id"] == first["factor_id"]
    assert db.query(EmissionFactor).filter(
        EmissionFactor.source == PACT_FACTOR_SOURCE).count() == 1


def test_category_and_subcategory_can_be_supplied(db):
    org = _org(db)
    r = materialise(db, _held(db, org), category="polymer", subcategory="PP")
    f = db.get(EmissionFactor, r["factor_id"])
    assert f.category == "polymer" and f.subcategory == "PP"


def test_category_defaults_to_something_recognisable(db):
    org = _org(db)
    f = db.get(EmissionFactor, materialise(db, _held(db, org))["factor_id"])
    assert f.category == "acmepp 1234"
    assert f.subcategory == "Acme Chemicals GmbH"


# --- the three refusals -------------------------------------------------------

def test_a_deprecated_footprint_is_not_materialised(db):
    """Its author withdrew it; a live factor would put a withdrawn figure into a
    future inventory."""
    org = _org(db)
    row = _held(db, org, status="Deprecated")
    v = materialisation_verdict(db, row)
    assert v["can_materialise"] is False
    assert any("Deprecated" in p for p in v["problems"])
    assert materialise(db, row)["materialised"] is False
    assert db.query(EmissionFactor).count() == 0


def test_an_unmappable_declared_unit_refuses_rather_than_guessing(db):
    """PACT's unit vocabulary is not ours. A guessed mapping is an
    order-of-magnitude error that would be invisible in the result."""
    org = _org(db)
    row = _held(db, org)
    row.declared_unit = "furlong"
    db.commit()
    v = materialisation_verdict(db, row)
    assert v["can_materialise"] is False
    assert any("no mapping" in p for p in v["problems"])


@pytest.mark.parametrize("pact_unit,engine_unit", sorted(DECLARED_UNIT_MAP.items()))
def test_every_mapped_unit_materialises(db, pact_unit, engine_unit):
    org = _org(db)
    row = _held(db, org, pcf={"declaredUnitOfMeasurement": pact_unit})
    r = materialise(db, row)
    assert r["materialised"] is True, pact_unit
    assert db.get(EmissionFactor, r["factor_id"]).unit == engine_unit


def test_an_unsupported_gwp_set_refuses(db):
    """The run's gwp_mismatch guard compares vintages; an unrecognised one would
    slip past it and mix AR5 with AR6 inside a single total."""
    org = _org(db)
    row = _held(db, org, pcf={"ipccCharacterizationFactors": ["AR4"]})
    v = materialisation_verdict(db, row)
    assert v["can_materialise"] is False
    assert any("GWP set" in p for p in v["problems"])


def test_ar5_is_supported_as_well_as_ar6(db):
    org = _org(db)
    row = _held(db, org, pcf={"ipccCharacterizationFactors": ["AR5"]})
    r = materialise(db, row)
    assert r["materialised"] is True
    assert db.get(EmissionFactor, r["factor_id"]).gwp_set == "AR5"


def test_a_negative_per_unit_figure_refuses(db):
    """A negative factor would net a removal into the gross total."""
    org = _org(db)
    row = _held(db, org)
    row.kg_co2e_per_unit_excl_biogenic = -1.0
    db.commit()
    v = materialisation_verdict(db, row)
    assert v["can_materialise"] is False
    assert any("negative" in p for p in v["problems"])


def test_the_preview_writes_nothing(db):
    org = _org(db)
    row = _held(db, org)
    v = materialisation_verdict(db, row)
    assert v["can_materialise"] is True and v["factor_preview"]["value"] == pytest.approx(4.2)
    assert db.query(EmissionFactor).count() == 0


def test_a_dimensionless_unit_is_disclosed(db):
    org = _org(db)
    r = materialise(db, _held(db, org, pcf={"declaredUnitOfMeasurement": "piece"}))
    assert r["materialised"] is True
    assert "count, not a physical dimension" in r["dimensionless_unit_note"]


def test_a_dimensional_unit_carries_no_such_note(db):
    org = _org(db)
    assert materialise(db, _held(db, org))["dimensionless_unit_note"] is None


# --- binding ------------------------------------------------------------------

def _activity(db, org, quantity=100.0, unit="kg", category="polymer"):
    """An activity on an industry-AVERAGE factor — the state substitution improves on.

    Deliberately not a spend-based factor: those are keyed by currency and the
    engine (correctly) refuses one without a base_year and a price index, so a
    spend factor quoted in kg is not a thing that can exist. average_data scores
    pedigree reliability 3 against supplier_specific's 1, which is the contrast
    under test.
    """
    f = EmissionFactor(source="INDUSTRY", version="1", geography="GB", year=2024,
                       category=category, subcategory="", unit=unit, gwp_set="AR6",
                       value=1.5, method_type="average_data")
    db.add(f); db.commit(); db.refresh(f)
    a = ActivityRecord(organisation_id=org.id, date="2027-03-01", category=category,
                       subcategory="", description="purchased polymer",
                       quantity=quantity, unit=unit, geo="GB", factor_id=f.id,
                       mapping_basis="category_only", mapping_status="auto")
    db.add(a); db.commit(); db.refresh(a)
    return a


def test_binding_moves_an_activity_onto_the_supplier_factor(db):
    org = _org(db)
    a = _activity(db, org)
    fid = materialise(db, _held(db, org))["factor_id"]
    out = bind_activities(db, org.id, fid, [a.id])
    assert out["bound"] == 1
    db.refresh(a)
    assert a.factor_id == fid
    assert a.mapping_status == "overridden"
    assert a.mapping_confidence == 1.0


def test_binding_refuses_an_activity_whose_unit_cannot_convert(db):
    """Caught at BIND time rather than inside a run, where it would become an
    excluded row in a coverage counter."""
    org = _org(db)
    a = _activity(db, org, unit="kWh")           # energy, against a kg footprint
    fid = materialise(db, _held(db, org))["factor_id"]
    out = bind_activities(db, org.id, fid, [a.id])
    assert out["bound"] == 0
    assert "cannot convert" in out["skipped"][0]["reason"]
    db.refresh(a)
    assert a.factor_id != fid                    # left untouched


def test_binding_converts_across_compatible_units(db):
    org = _org(db)
    a = _activity(db, org, quantity=2.0, unit="tonne")
    fid = materialise(db, _held(db, org))["factor_id"]
    assert bind_activities(db, org.id, fid, [a.id])["bound"] == 1
    run = compute_co2e(db, org.id)
    # 2 tonne = 2000 kg at 4.2 kgCO2e/kg
    assert run.total_co2e == pytest.approx(8400.0)


def test_binding_is_scoped_to_the_organisation(db):
    a_org, b_org = _org(db), _org(db)
    a = _activity(db, b_org)
    fid = materialise(db, _held(db, a_org))["factor_id"]
    out = bind_activities(db, a_org.id, fid, [a.id])
    assert out["bound"] == 0
    assert "not found for this organisation" in out["skipped"][0]["reason"]


def test_binding_rejects_a_non_pact_factor(db):
    org = _org(db)
    a = _activity(db, org)
    out = bind_activities(db, org.id, a.factor_id, [a.id])
    assert out["bound"] == 0
    assert "not a materialised PACT factor" in out["reason"]


# --- the payoff ---------------------------------------------------------------

def test_substitution_narrows_the_uncertainty_band(db):
    """THE POINT OF THE WHOLE EPIC, measured rather than asserted.

    A line on an industry-average factor scores pedigree reliability 3. Rebinding
    it to a supplier-specific footprint scores 1 (the best), and because dq.py
    already maps method_type to that indicator, the lognormal sigma narrows and the
    Monte Carlo interval tightens — with no change to the calculation engine. The
    gap is wider still from spend_based, which scores 5."""
    org = _org(db)
    activities = [_activity(db, org, quantity=100.0 + i) for i in range(6)]

    before_run = compute_co2e(db, org.id)
    before = propagate(db, before_run.id, iterations=20000)
    before_width = before["interval"]["relative_half_width_pct"]

    fid = materialise(db, _held(db, org))["factor_id"]
    assert bind_activities(db, org.id, fid, [a.id for a in activities])["bound"] == 6

    after_run = compute_co2e(db, org.id)
    after = propagate(db, after_run.id, iterations=20000)
    after_width = after["interval"]["relative_half_width_pct"]

    assert after_width < before_width, (before_width, after_width)


def test_substitution_raises_the_primary_data_share(db):
    from app.reports.summary import summary
    org = _org(db)
    activities = [_activity(db, org) for _ in range(3)]

    compute_co2e(db, org.id)
    before = summary(db, organisation_id=org.id)["method_split"]["primary_data_share_pct"]

    fid = materialise(db, _held(db, org))["factor_id"]
    bind_activities(db, org.id, fid, [a.id for a in activities])
    compute_co2e(db, org.id)
    after = summary(db, organisation_id=org.id)["method_split"]["primary_data_share_pct"]

    assert before == pytest.approx(0.0)
    assert after == pytest.approx(100.0)


def test_the_frozen_line_records_the_supplier_method(db):
    """The lineage an assuror follows: the line itself says it rests on primary
    data, and which footprint it came from."""
    org = _org(db)
    a = _activity(db, org)
    row = _held(db, org)
    fid = materialise(db, row)["factor_id"]
    bind_activities(db, org.id, fid, [a.id])
    run = compute_co2e(db, org.id)

    from app.models import EmissionLineItem
    line = db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id).first()
    detail = json.loads(line.details)
    assert detail["method_type"] == "supplier_specific"
    assert detail["lca_boundary"] == "cradle_to_gate"
    assert detail["data_quality"]["indicators"]["reliability"] == 1

    factor = db.get(EmissionFactor, detail["factor_id"])
    assert factor.version == row.pf_id     # traceable back to the supplier's PCF


def test_the_engine_was_not_special_cased(db):
    """A guard on the design claim: no PACT-specific branch exists in calc.py."""
    src = open("app/services/calc.py").read()
    assert "pact" not in src.lower()


# --- endpoints ----------------------------------------------------------------

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
    fid = client.post("/pact/footprints/import", headers=hdr, json=_doc()).json()["id"]
    yield client, hdr, fid
    main_mod.app.dependency_overrides.clear()


def test_materialise_endpoint(env):
    client, hdr, fid = env
    r = client.post(f"/pact/footprints/{fid}/materialise", headers=hdr)
    assert r.status_code == 200
    body = r.json()
    assert body["materialised"] is True
    assert body["factor"]["method_type"] == "supplier_specific"
    assert body["effect"]["pedigree_reliability"] == 1


def test_preview_endpoint_writes_nothing(env):
    client, hdr, fid = env
    r = client.get(f"/pact/footprints/{fid}/materialisation", headers=hdr)
    assert r.status_code == 200 and r.json()["can_materialise"] is True
    # A second materialise still creates it, proving the preview did not.
    assert client.post(f"/pact/footprints/{fid}/materialise",
                       headers=hdr).json()["materialised"] is True


def test_materialise_endpoint_returns_422_when_it_cannot(env):
    client, hdr, fid = env
    other = client.post("/pact/footprints/import", headers=hdr,
                        json=_doc(status="Deprecated")).json()["id"]
    r = client.post(f"/pact/footprints/{other}/materialise", headers=hdr)
    assert r.status_code == 422
    assert any("Deprecated" in p for p in r.json()["problems"])


def test_bind_endpoint_validates_its_input(env):
    client, hdr, fid = env
    factor_id = client.post(f"/pact/footprints/{fid}/materialise",
                            headers=hdr).json()["factor_id"]
    assert client.post(f"/pact/factors/{factor_id}/bind", headers=hdr,
                       params={"activity_ids": "not,ints"}).status_code == 400
    assert client.post(f"/pact/factors/{factor_id}/bind", headers=hdr,
                       params={"activity_ids": ""}).status_code == 400


def test_endpoints_are_scoped_to_the_organisation(env):
    client, hdr, fid = env
    other = client.post("/organisations", params={"name": "B"}).json()["api_key"]
    assert client.post(f"/pact/footprints/{fid}/materialise",
                       headers={"X-API-Key": other}).status_code == 404
