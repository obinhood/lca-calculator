"""PACT Technical Specifications v3 — ProductFootprint validation and storage.

Three things carry this module.

v3 IS NOT v2 WITH A NEW LABEL. It renamed the declared-unit fields, requires both
biogenic variants, made primaryDataShare and dqi mandatory, and removed
version/updated/statusComment. A v2 document accepted as v3 would be silently
missing what v3 requires, so the spec version is checked rather than assumed.

DECIMALS ARE STRINGS ON THE WIRE, and the published string must round-trip
byte-identical — float(x) is lossy and a supplier's figure is their assertion.

A FOOTPRINT QUOTED AGAINST declaredUnitAmount IS NOT PER UNIT. A PCF of 42 kgCO2e
for a declaredUnitAmount of 10 is 4.2 per unit; using 42 overstates tenfold.
"""
import json

import pytest

from app.models import Organisation, ProductFootprint
from app.services.pact import (
    DECLARED_UNITS, SUPPORTED_SPEC_VERSIONS, kg_co2e_per_declared_unit,
    parse_decimal, parse_document, spec_version_verdict, summarise, validate,
)
from app.services.pact_store import (
    footprint_view, import_footprint, list_footprints,
)


# --- a conforming v3 document ------------------------------------------------

def _doc(**over) -> dict:
    d = {
        "id": "3893bb5d-da16-4dc1-9185-11d97476c254",
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
    for k, v in over.items():
        if k == "pcf":
            d["pcf"].update(v)
        else:
            d[k] = v
    return d


def _org(db, name="PactOrg"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


# --- the reference document validates ----------------------------------------

def test_a_conforming_v3_document_validates():
    v = validate(_doc())
    assert v["valid"] is True, v["errors"]
    assert v["errors"] == []
    assert v["geography_level"] == "geographyCountry"


def test_every_declared_unit_in_the_v3_enum_is_accepted():
    for unit in DECLARED_UNITS:
        assert validate(_doc(pcf={"declaredUnitOfMeasurement": unit}))["valid"], unit


def test_an_unknown_declared_unit_is_rejected():
    v = validate(_doc(pcf={"declaredUnitOfMeasurement": "barrels"}))
    assert v["valid"] is False
    assert any("declaredUnitOfMeasurement" in e["field"] for e in v["errors"])


# --- v3 is not v2 -------------------------------------------------------------

def test_a_v2_document_is_refused_with_the_deprecation_reason():
    """v2.x was deprecated 2026-04-01 and structurally lacks what v3 requires."""
    v = validate(_doc(specVersion="2.3.3"))
    assert v["valid"] is False
    msg = " ".join(e["message"] for e in v["errors"])
    assert "deprecated" in msg and "2026-04-01" in msg


def test_spec_version_verdict_names_the_missing_v3_fields():
    r = spec_version_verdict("2.3.3")
    assert r["ok"] is False
    assert "primaryDataShare" in r["reason"] and "dqi" in r["reason"]


@pytest.mark.parametrize("bad", [None, "", "three", "abc.def", 3])
def test_unusable_spec_version_is_rejected(bad):
    assert spec_version_verdict(bad)["ok"] is False


def test_every_supported_patch_level_is_an_exact_match():
    for sv in SUPPORTED_SPEC_VERSIONS:
        r = spec_version_verdict(sv)
        assert r["ok"] and r["exact_match"], sv


def test_an_unverified_v3_patch_level_warns_but_passes():
    v = validate(_doc(specVersion="3.0.9"))
    assert v["valid"] is True
    assert any("patch level" in w["message"] for w in v["warnings"])


def test_v2_only_fields_are_flagged_as_a_relabel():
    """version/updated/statusComment were REMOVED in v3. Their presence in a
    v3-labelled document means the sender relabelled rather than migrated."""
    v = validate(_doc(version=3, updated="2027-01-01T00:00:00Z",
                      statusComment="revised"))
    assert v["valid"] is True          # not fatal — but it must be said
    msg = " ".join(w["message"] for w in v["warnings"])
    assert "v2-only properties" in msg
    assert "immutable" in msg


def test_v2_only_pcf_field_names_are_flagged_with_their_v3_equivalents():
    v = validate(_doc(pcf={"declaredUnit": "kilogram", "assurance": {}}))
    msg = " ".join(w["message"] for w in v["warnings"])
    assert "declaredUnit -> declaredUnitOfMeasurement" in msg
    assert "assurance -> verification" in msg


# --- v3's newly mandatory fields ---------------------------------------------

@pytest.mark.parametrize("field", [
    "primaryDataShare", "dqi", "pcfIncludingBiogenicUptake",
    "productMassPerDeclaredUnit",
])
def test_fields_that_became_mandatory_in_v3(field):
    """These were optional in v2. A v2-era producer omitting them is exactly the
    case this validator exists to catch."""
    d = _doc()
    d["pcf"].pop(field)
    v = validate(d)
    if field == "dqi":
        # dqi is SHALL-by-2027, so its absence warns rather than blocks today.
        assert v["valid"] is True
        assert any("dqi" in w["field"] for w in v["warnings"])
    else:
        assert v["valid"] is False, field
        assert any(field in e["field"] for e in v["errors"]), field


@pytest.mark.parametrize("field", [
    "id", "specVersion", "created", "status", "companyName", "companyIds",
    "productDescription", "productIds", "productNameCompany", "pcf",
])
def test_every_product_footprint_shall_field_is_required(field):
    d = _doc()
    d.pop(field)
    v = validate(d)
    assert v["valid"] is False, field
    assert any(e["field"] == field for e in v["errors"]), field


# --- decimals are strings -----------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("10", 10.0), ("42.12", 42.12), ("-182.84", -182.84), ("0", 0.0),
    ("1e3", 1000.0), (" 7.5 ", 7.5),
])
def test_decimal_strings_parse(raw, expected):
    assert parse_decimal(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "abc", "1,5", "1.2.3", True, {}, []])
def test_non_decimals_do_not_parse(raw):
    assert parse_decimal(raw) is None


def test_a_json_number_is_accepted_on_read_but_warned():
    """Real senders emit numbers; the spec says strings. Tolerate inbound, but say
    so, because emitting one would fail a conformance test."""
    v = validate(_doc(pcf={"pcfExcludingBiogenicUptake": 42.0}))
    assert v["valid"] is True
    assert any("decimal STRING" in w["message"] for w in v["warnings"])


def test_a_non_numeric_decimal_is_an_error():
    v = validate(_doc(pcf={"pcfExcludingBiogenicUptake": "about forty"}))
    assert v["valid"] is False


def test_the_published_string_round_trips_byte_identical(db):
    """float() is lossy and a supplier's published figure is their assertion."""
    org = _org(db)
    doc = _doc(pcf={"pcfExcludingBiogenicUptake": "42.10000000000000001"})
    r = import_footprint(db, org.id, doc)
    assert r["stored"] is True
    row = db.get(ProductFootprint, r["id"])
    stored = json.loads(row.document)
    assert stored["pcf"]["pcfExcludingBiogenicUptake"] == "42.10000000000000001"


# --- per-declared-unit arithmetic ---------------------------------------------

def test_footprint_is_divided_by_the_declared_unit_amount():
    """42 kgCO2e for a declaredUnitAmount of 10 is 4.2 per unit. Using 42 would
    overstate by an order of magnitude."""
    s = summarise(_doc())
    assert kg_co2e_per_declared_unit(s) == pytest.approx(4.2)
    assert kg_co2e_per_declared_unit(s, include_biogenic=True) == pytest.approx(4.05)


def test_division_refuses_rather_than_dividing_by_zero():
    s = summarise(_doc())
    s["declared_unit_amount"] = 0
    assert kg_co2e_per_declared_unit(s) is None
    s["declared_unit_amount"] = None
    assert kg_co2e_per_declared_unit(s) is None


def test_declared_unit_amount_must_be_positive():
    for bad in ("0", "-5"):
        v = validate(_doc(pcf={"declaredUnitAmount": bad}))
        assert v["valid"] is False, bad


def test_import_stores_the_pre_divided_per_unit_figure(db):
    org = _org(db)
    r = import_footprint(db, org.id, _doc())
    row = db.get(ProductFootprint, r["id"])
    assert row.kg_co2e_per_unit_excl_biogenic == pytest.approx(4.2)
    assert row.kg_co2e_per_unit_incl_biogenic == pytest.approx(4.05)
    assert row.declared_unit_amount == pytest.approx(10.0)


# --- geography mutual exclusivity ---------------------------------------------

def test_exactly_one_geography_level_is_permitted():
    v = validate(_doc(pcf={"geographyCountry": "DE",
                           "geographyRegionOrSubregion": "Western Europe"}))
    assert v["valid"] is False
    assert any("mutually exclusive" in e["message"] for e in v["errors"])


def test_no_geography_means_global():
    d = _doc()
    d["pcf"].pop("geographyCountry")
    v = validate(d)
    assert v["valid"] is True
    assert v["geography_level"] == "global"
    assert summarise(d)["geography_level"] == "global"


# --- bounded fields -----------------------------------------------------------

@pytest.mark.parametrize("value,ok", [
    ("0", True), ("5", True), ("2.5", True), ("5.01", False), ("-1", False),
])
def test_exempted_emissions_percent_is_capped_at_five(value, ok):
    """The methodology caps what a conforming PCF may exclude."""
    assert validate(_doc(pcf={"exemptedEmissionsPercent": value}))["valid"] is ok


@pytest.mark.parametrize("value,ok", [
    ("0", True), ("100", True), ("62.5", True), ("100.1", False), ("-1", False),
])
def test_primary_data_share_is_a_percentage(value, ok):
    assert validate(_doc(pcf={"primaryDataShare": value}))["valid"] is ok


@pytest.mark.parametrize("value,ok", [
    ("1", True), ("5", True), ("3.5", True), ("0.9", False), ("5.1", False),
])
def test_dqi_ratings_are_bounded_one_to_five(value, ok):
    d = _doc()
    d["pcf"]["dqi"]["technologicalDQR"] = value
    assert validate(d)["valid"] is ok


def test_reference_period_must_run_forwards():
    v = validate(_doc(pcf={"referencePeriodStart": "2027-01-01T00:00:00Z",
                           "referencePeriodEnd": "2026-01-01T00:00:00Z"}))
    assert v["valid"] is False
    assert any("after" in e["message"] for e in v["errors"])


def test_negative_mass_and_content_are_rejected():
    for f in ("fossilCarbonContent", "fossilGhgEmissions",
              "productMassPerDeclaredUnit"):
        assert validate(_doc(pcf={f: "-1"}))["valid"] is False, f


def test_unrecognised_cross_sectoral_standard_warns_but_passes():
    v = validate(_doc(pcf={"crossSectoralStandards": ["MyOwnStandard"]}))
    assert v["valid"] is True
    assert any("unrecognised standard" in w["message"] for w in v["warnings"])


def test_id_must_be_a_uuid():
    assert validate(_doc(id="not-a-uuid"))["valid"] is False


def test_status_is_constrained():
    assert validate(_doc(status="Draft"))["valid"] is False
    assert validate(_doc(status="Deprecated"))["valid"] is True


# --- parsing -----------------------------------------------------------------

def test_parse_document_accepts_text_bytes_and_objects():
    d = _doc()
    for raw in (d, json.dumps(d), json.dumps(d).encode()):
        parsed, err = parse_document(raw)
        assert err is None and parsed["id"] == d["id"]


@pytest.mark.parametrize("raw,needle", [
    ("{not json", "not valid JSON"),
    ("[1,2]", "must be a JSON object"),
    (7, "JSON text or an object"),
    (b"\xff\xfe", "not valid UTF-8"),
])
def test_parse_document_refuses_with_a_reason(raw, needle):
    parsed, err = parse_document(raw)
    assert parsed is None and needle in err


# --- storage ------------------------------------------------------------------

def test_import_stores_and_summarises(db):
    org = _org(db)
    r = import_footprint(db, org.id, _doc())
    assert r["stored"] is True
    row = db.get(ProductFootprint, r["id"])
    assert row.pf_id == _doc()["id"]
    assert row.direction == "received"
    assert row.company_name == "Acme Chemicals GmbH"
    assert row.primary_data_share == pytest.approx(62.5)
    assert row.geography_level == "geographyCountry" and row.geography_value == "DE"
    assert row.dqi_technological == pytest.approx(2.0)


def test_an_invalid_document_is_never_stored(db):
    """No 'store it and flag it' path: a stored footprint is what later becomes a
    primary-data factor, and a flag is not a barrier once the row exists."""
    org = _org(db)
    r = import_footprint(db, org.id, _doc(specVersion="2.3.3"))
    assert r["stored"] is False
    assert r["errors"]
    assert db.query(ProductFootprint).count() == 0


def test_reimporting_identical_content_is_idempotent(db):
    org = _org(db)
    assert import_footprint(db, org.id, _doc())["stored"] is True
    again = import_footprint(db, org.id, _doc())
    assert again["stored"] is False
    assert again["idempotent"] is True
    assert db.query(ProductFootprint).count() == 1


def test_same_id_different_content_is_refused_as_a_protocol_violation(db):
    """v3 removed in-place updates entirely: a correction must arrive as a NEW id."""
    org = _org(db)
    import_footprint(db, org.id, _doc())
    r = import_footprint(db, org.id, _doc(pcf={"pcfExcludingBiogenicUptake": "99.0"}))
    assert r["stored"] is False
    assert r["held_content_hash"] != r["incoming_content_hash"]
    assert "precedingPfIds" in " ".join(e["message"] for e in r["errors"])
    # The held row is untouched.
    row = db.query(ProductFootprint).one()
    assert row.kg_co2e_per_unit_excl_biogenic == pytest.approx(4.2)


def test_a_superseding_footprint_deprecates_the_old_one_without_deleting_it(db):
    """A filed run that used the old figure must still be able to show what it used."""
    org = _org(db)
    first = import_footprint(db, org.id, _doc())
    second = import_footprint(db, org.id, _doc(
        id="9f8e7d6c-5b4a-4392-8180-7f6e5d4c3b2a",
        precedingPfIds=[_doc()["id"]],
        pcf={"pcfExcludingBiogenicUptake": "38.0"}))
    assert second["stored"] is True
    assert second["deprecated_superseded"] == [_doc()["id"]]

    old = db.get(ProductFootprint, first["id"])
    assert old.status == "Deprecated"
    assert old.kg_co2e_per_unit_excl_biogenic == pytest.approx(4.2)   # still readable
    new = db.get(ProductFootprint, second["id"])
    assert new.status == "Active"
    assert new.kg_co2e_per_unit_excl_biogenic == pytest.approx(3.8)


def test_preceding_ids_must_be_uuids():
    assert validate(_doc(precedingPfIds=["nope"]))["valid"] is False


def test_two_organisations_may_hold_the_same_footprint(db):
    """The same supplier PCF legitimately reaches many buyers."""
    a, b = _org(db, "A"), _org(db, "B")
    assert import_footprint(db, a.id, _doc())["stored"] is True
    assert import_footprint(db, b.id, _doc())["stored"] is True
    assert db.query(ProductFootprint).count() == 2


def test_direction_is_constrained(db):
    org = _org(db)
    r = import_footprint(db, org.id, _doc(), direction="sideways")
    assert r["stored"] is False and "direction" in r["reason"]


def test_warnings_are_frozen_at_import(db):
    org = _org(db)
    r = import_footprint(db, org.id, _doc(pcf={"crossSectoralStandards": ["Homebrew"]}))
    row = db.get(ProductFootprint, r["id"])
    assert any("unrecognised standard" in w["message"]
               for w in json.loads(row.validation_warnings))


def test_listing_filters(db):
    org = _org(db)
    import_footprint(db, org.id, _doc())
    import_footprint(db, org.id, _doc(id="11111111-2222-4333-8444-555555555555",
                                      productIds=["urn:pact:product:x:OTHER"]),
                     direction="published")
    assert len(list_footprints(db, org.id)) == 2
    assert len(list_footprints(db, org.id, direction="published")) == 1
    assert len(list_footprints(db, org.id, product_id="urn:pact:product:x:OTHER")) == 1
    assert len(list_footprints(db, org.id, status="Active")) == 2


def test_view_omits_the_document_unless_asked(db):
    org = _org(db)
    r = import_footprint(db, org.id, _doc())
    row = db.get(ProductFootprint, r["id"])
    assert "document" not in footprint_view(row)
    assert footprint_view(row, include_document=True)["document"]["id"] == _doc()["id"]


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
    a = client.post("/organisations", params={"name": "A"}).json()["api_key"]
    b = client.post("/organisations", params={"name": "B"}).json()["api_key"]
    yield client, {"X-API-Key": a}, {"X-API-Key": b}
    main_mod.app.dependency_overrides.clear()


def test_endpoint_imports_a_conforming_document(env):
    client, hdr, _ = env
    r = client.post("/pact/footprints/import", headers=hdr, json=_doc())
    assert r.status_code == 200
    body = r.json()
    assert body["stored"] is True
    assert body["kg_co2e_per_unit"] == pytest.approx(4.2)


def test_endpoint_refuses_a_non_conforming_document_with_422(env):
    client, hdr, _ = env
    r = client.post("/pact/footprints/import", headers=hdr, json=_doc(specVersion="2.3.3"))
    assert r.status_code == 422
    assert r.json()["stored"] is False


def test_endpoint_returns_409_for_a_same_id_content_conflict(env):
    client, hdr, _ = env
    client.post("/pact/footprints/import", headers=hdr, json=_doc())
    r = client.post("/pact/footprints/import", headers=hdr,
                    json=_doc(pcf={"pcfExcludingBiogenicUptake": "99.0"}))
    assert r.status_code == 409
    assert r.json()["held_content_hash"] != r.json()["incoming_content_hash"]


def test_validate_endpoint_does_not_store(env):
    client, hdr, _ = env
    r = client.post("/pact/validate", headers=hdr, json=_doc())
    assert r.status_code == 200 and r.json()["valid"] is True
    assert client.get("/pact/footprints", headers=hdr).json()["data"] == []


def test_footprints_are_scoped_to_the_calling_organisation(env):
    client, hdr_a, hdr_b = env
    fid = client.post("/pact/footprints/import", headers=hdr_a, json=_doc()).json()["id"]
    assert client.get(f"/pact/footprints/{fid}", headers=hdr_a).status_code == 200
    assert client.get(f"/pact/footprints/{fid}", headers=hdr_b).status_code == 404
    assert client.get("/pact/footprints", headers=hdr_b).json()["data"] == []


def test_document_is_returned_verbatim_when_requested(env):
    client, hdr, _ = env
    fid = client.post("/pact/footprints/import", headers=hdr, json=_doc()).json()["id"]
    body = client.get(f"/pact/footprints/{fid}", headers=hdr,
                      params={"include_document": True}).json()
    assert body["document"]["pcf"]["pcfExcludingBiogenicUptake"] == "42.0"


def test_malformed_body_is_refused_with_the_parse_error(env):
    client, hdr, _ = env
    r = client.post("/pact/footprints/import", headers=hdr, content=b"{not json")
    assert r.status_code == 422
    assert "not valid JSON" in " ".join(e["message"] for e in r.json()["errors"])
