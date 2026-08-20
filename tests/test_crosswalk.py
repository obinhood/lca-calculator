"""Versioned classification crosswalks and their measured uncertainty.

The load-bearing decision is that crosswalk uncertainty is DATA-DERIVED, not
scored. sigma is ln(GSD) of the candidate set's own factor values, so a
one-to-one hop is exactly zero — which is the right answer and one no fixed 1-5
pedigree score can produce. The pedigree indicator is reported for
interoperability but its published variance caps an order of magnitude below what
spend-factor sets actually disperse by.
"""
import math

import pytest

from app.models import Crosswalk, CrosswalkMapping, EmissionFactor, Organisation
from app.services.crosswalk import (
    KNOWN_HOP_QUALITY, PEDIGREE_TECH_CORRELATION_CAP_VARIANCE, SCHEMES,
    add_mappings, chain_uncertainty, dispersion_sigma, effective_resolution,
    hop_family_verdict, hop_uncertainty, register, resolve,
)


def _factor(db, code, value, source="USEEIO supply chain"):
    f = EmissionFactor(source=source, version="1", geography="US", year=2024,
                       category="spend", subcategory=code, unit="USD",
                       gwp_set="AR6", value=value, method_type="spend_based",
                       base_year=2024)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _table(db, frm="CPA", to="CPC", version="2.1"):
    r = register(db, from_scheme=frm, to_scheme=to, source="Eurostat",
                 table_version=version, licence="open")
    assert r["registered"], r
    return r["id"]


# --- the product / industry divide -------------------------------------------

def test_a_direct_unspsc_to_naics_hop_is_uncitable():
    """UNSPSC classifies the PRODUCT bought; NAICS classifies the ESTABLISHMENT
    that produced it. Every table on offer is commercial or machine-generated."""
    v = hop_family_verdict("UNSPSC", "NAICS")
    assert v["uncitable"] is True
    assert "ESTABLISHMENT" in v["reason"]
    assert "UNSPSC -> CPC -> ISIC" in v["reason"]


def test_the_reverse_direction_is_equally_uncitable():
    assert hop_family_verdict("NACE", "UNSPSC")["uncitable"] is True


def test_a_product_to_product_hop_is_citable():
    v = hop_family_verdict("CPA", "CPC")
    assert v["uncitable"] is False
    assert v["known_quality"]["one_to_one_pct"] == 89.1


def test_an_industry_to_industry_hop_is_citable():
    v = hop_family_verdict("ISIC", "NAICS")
    assert v["uncitable"] is False
    assert v["known_quality"]["one_to_one_pct"] == 24.6


def test_registering_an_uncitable_table_marks_it(db):
    r = register(db, from_scheme="UNSPSC", to_scheme="NAICS",
                 source="internal", table_version="2026-01")
    assert r["registered"] is True
    assert r["uncitable"] is True
    assert "no official UNSPSC correspondence" in r["uncitable_reason"].lower() or \
           "No official UNSPSC correspondence" in r["uncitable_reason"]


# --- registration -------------------------------------------------------------

def test_an_unknown_scheme_is_refused(db):
    r = register(db, from_scheme="MADE_UP", to_scheme="NAICS", source="x",
                 table_version="1")
    assert r["registered"] is False and "scheme must be" in r["reason"]


def test_a_scheme_cannot_map_to_itself(db):
    r = register(db, from_scheme="NAICS", to_scheme="NAICS", source="x",
                 table_version="1")
    assert r["registered"] is False


def test_re_registering_the_same_version_is_idempotent(db):
    _table(db)
    r = register(db, from_scheme="CPA", to_scheme="CPC", source="Eurostat",
                 table_version="2.1")
    assert r["registered"] is False and r["idempotent"] is True
    assert db.query(Crosswalk).count() == 1


def test_two_versions_coexist(db):
    _table(db, version="2.1")
    _table(db, version="2.2")
    assert db.query(Crosswalk).count() == 2


# --- resolution ---------------------------------------------------------------

def test_a_one_to_one_hop_resolves_cleanly(db):
    cid = _table(db)
    add_mappings(db, cid, [{"from_code": "A01", "to_code": "011"}])
    r = resolve(db, from_scheme="CPA", from_code="A01", to_scheme="CPC")
    assert r["resolved"] is True
    assert r["one_to_one"] is True and r["cardinality"] == 1


def test_cardinality_is_preserved(db):
    """A hop with one candidate and a hop with twenty-five must be
    distinguishable — collapsing them is what makes crosswalk error invisible."""
    cid = _table(db)
    add_mappings(db, cid, [{"from_code": "A01", "to_code": f"0{i}"} for i in range(12)])
    r = resolve(db, from_scheme="CPA", from_code="A01", to_scheme="CPC")
    assert r["cardinality"] == 12 and r["one_to_one"] is False


def test_a_partial_row_is_recorded_as_unresolvable_by_lookup(db):
    """93.7% of ISIC Rev.4 to NAICS 2017 rows are flagged partial."""
    cid = _table(db, "ISIC", "NAICS", "2017")
    add_mappings(db, cid, [
        {"from_code": "0111", "to_code": "111140", "partial": True,
         "note": "except kale, mangold wurzel, and pepper farming"}])
    r = resolve(db, from_scheme="ISIC", from_code="0111", to_scheme="NAICS")
    assert r["partial_rows"] == 1
    assert "NOT resolvable by lookup alone" in r["partial_note"]


def test_an_unregistered_hop_does_not_resolve(db):
    r = resolve(db, from_scheme="CPA", from_code="A01", to_scheme="CPC")
    assert r["resolved"] is False and "no registered" in r["reason"]


def test_an_unmapped_code_does_not_resolve(db):
    cid = _table(db)
    add_mappings(db, cid, [{"from_code": "A01", "to_code": "011"}])
    r = resolve(db, from_scheme="CPA", from_code="Z99", to_scheme="CPC")
    assert r["resolved"] is False and "no entry" in r["reason"]


def test_codes_are_matched_case_insensitively(db):
    cid = _table(db)
    add_mappings(db, cid, [{"from_code": "a01", "to_code": "011"}])
    assert resolve(db, from_scheme="cpa", from_code="A01",
                   to_scheme="cpc")["resolved"] is True


def test_a_specific_table_version_can_be_pinned(db):
    _table(db, version="2.1")
    cid2 = _table(db, version="2.2")
    add_mappings(db, cid2, [{"from_code": "A01", "to_code": "NEW"}])
    r = resolve(db, from_scheme="CPA", from_code="A01", to_scheme="CPC",
                table_version="2.1")
    assert r["resolved"] is False        # only 2.2 carries the mapping
    r2 = resolve(db, from_scheme="CPA", from_code="A01", to_scheme="CPC",
                 table_version="2.2")
    assert r2["resolved"] is True and r2["table_version"] == "2.2"


# --- the measured uncertainty -------------------------------------------------

def test_a_single_candidate_carries_exactly_zero_uncertainty():
    """THE reason this is measured rather than scored: an unambiguous mapping adds
    nothing, and no fixed 1-5 score can produce zero."""
    d = dispersion_sigma([3.007])
    assert d["sigma"] == 0.0 and d["gsd"] == 1.0
    assert d["basis"] == "single_candidate"
    assert "cannot express that" in d["note"]


def test_dispersion_is_the_log_standard_deviation():
    d = dispersion_sigma([1.0, 10.0])
    assert d["sigma"] == pytest.approx(statistics_stdev_log([1.0, 10.0]))
    assert d["max_min_ratio"] == pytest.approx(10.0)
    assert d["basis"] == "measured_candidate_dispersion"


def statistics_stdev_log(values):
    import statistics
    return statistics.stdev([math.log(v) for v in values])


def test_a_wider_candidate_set_carries_more_uncertainty():
    tight = dispersion_sigma([3.0, 3.1, 2.9])
    wide = dispersion_sigma([1.0, 5.0, 21.0])
    assert wide["sigma"] > tight["sigma"]


@pytest.mark.parametrize("values", [[], [0.0], [-1.0], [float("nan")]])
def test_unusable_values_yield_no_uncertainty_rather_than_an_error(values):
    d = dispersion_sigma(values)
    assert d["sigma"] == 0.0


def test_candidate_factors_are_matched_by_code_not_by_scheme_name(db):
    """REGRESSION. A NAICS-keyed factor's source is its publisher — 'USEEIO',
    'EPA' — never 'NAICS'. Filtering the source by the scheme name found nothing
    and reported a real 21x ambiguity as ZERO uncertainty."""
    cid = _table(db, "ISIC", "NAICS", "2017")
    add_mappings(db, cid, [{"from_code": "X", "to_code": c} for c in ("a", "b")])
    _factor(db, "a", 1.0, source="USEEIO supply chain")
    _factor(db, "b", 21.0, source="USEEIO supply chain")
    h = hop_uncertainty(db, from_scheme="ISIC", from_code="X", to_scheme="NAICS")
    assert h["sigma"] > 0
    assert h["dispersion"]["n"] == 2


def test_the_hop_uncertainty_uses_the_candidate_factors(db):
    cid = _table(db, "ISIC", "NAICS", "2017")
    add_mappings(db, cid, [{"from_code": "2394", "to_code": c}
                           for c in ("327310", "327320", "327390")])
    _factor(db, "327310", 1.0)
    _factor(db, "327320", 7.0)
    _factor(db, "327390", 21.0)     # NAICS 327 spans 21x in the real data
    h = hop_uncertainty(db, from_scheme="ISIC", from_code="2394", to_scheme="NAICS")
    assert h["cardinality"] == 3
    assert h["dispersion"]["max_min_ratio"] == pytest.approx(21.0)
    assert h["sigma"] > 0
    assert h["variance_contribution"] == pytest.approx(h["sigma"] ** 2)


def test_a_high_dispersion_hop_saturates_the_pedigree_indicator(db):
    """ecoinvent Table 10.5 caps 'further technological correlation' at variance
    0.12 — roughly an order of magnitude below what spend-factor sets disperse by,
    so relying on the published factor would silently truncate it."""
    cid = _table(db, "ISIC", "NAICS", "2017")
    add_mappings(db, cid, [{"from_code": "X", "to_code": c} for c in ("a", "b")])
    _factor(db, "a", 1.0)
    _factor(db, "b", 21.0)
    h = hop_uncertainty(db, from_scheme="ISIC", from_code="X", to_scheme="NAICS")
    p = h["pedigree_interoperability"]
    assert p["saturated"] is True
    assert p["official_cap_variance"] == PEDIGREE_TECH_CORRELATION_CAP_VARIANCE == 0.12
    assert p["measured_variance"] > p["official_cap_variance"]
    assert "silently truncate" in p["note"]


def test_an_unresolvable_hop_has_unknown_not_zero_uncertainty(db):
    h = hop_uncertainty(db, from_scheme="CPA", from_code="A01", to_scheme="CPC")
    assert h["sigma"] is None
    assert "UNKNOWN, not zero" in h["sigma_note"]


def test_an_uncitable_hop_is_flagged_in_its_uncertainty(db):
    cid = register(db, from_scheme="UNSPSC", to_scheme="NAICS", source="internal",
                   table_version="v1")["id"]
    add_mappings(db, cid, [{"from_code": "43211500", "to_code": "334111"}])
    h = hop_uncertainty(db, from_scheme="UNSPSC", from_code="43211500",
                        to_scheme="NAICS")
    assert h["uncitable"] is True
    assert "proprietary internal mapping" in h["uncitable_note"]


# --- chains -------------------------------------------------------------------

def test_variances_add_across_a_chain(db):
    a = _table(db, "CHART_OF_ACCOUNTS", "CPC", "v1")
    add_mappings(db, a, [{"from_code": "6100", "to_code": c} for c in ("x1", "x2")])
    b = _table(db, "CPC", "NAICS", "v1")
    add_mappings(db, b, [{"from_code": "x1", "to_code": c} for c in ("n1", "n2")])
    for code, v in (("x1", 1.0), ("x2", 4.0), ("n1", 1.0), ("n2", 9.0)):
        _factor(db, code, v)

    out = chain_uncertainty(db, [
        {"from_scheme": "CHART_OF_ACCOUNTS", "from_code": "6100", "to_scheme": "CPC"},
        {"from_scheme": "CPC", "from_code": "x1", "to_scheme": "NAICS"},
    ])
    assert out["quantifiable"] is True
    assert out["hop_count"] == 2
    hop_vars = [h["variance_contribution"] for h in out["hops"]]
    assert out["total_variance"] == pytest.approx(sum(hop_vars))
    assert out["combined_gsd"] > 1.0


def test_one_unresolvable_hop_makes_the_whole_chain_unquantifiable(db):
    """A chain is only as citable as its weakest link — an unresolvable hop must
    not contribute zero."""
    a = _table(db, "CHART_OF_ACCOUNTS", "CPC", "v1")
    add_mappings(db, a, [{"from_code": "6100", "to_code": "x1"}])
    _factor(db, "x1", 1.0)
    out = chain_uncertainty(db, [
        {"from_scheme": "CHART_OF_ACCOUNTS", "from_code": "6100", "to_scheme": "CPC"},
        {"from_scheme": "CPC", "from_code": "x1", "to_scheme": "NAICS"},
    ])
    assert out["quantifiable"] is False
    assert out["total_variance"] is None and out["total_sigma"] is None
    assert out["unresolved_hops"] == ["CPC->NAICS"]
    assert "weakest link" in out["note"]


def test_an_uncitable_hop_is_named_in_the_chain(db):
    cid = register(db, from_scheme="UNSPSC", to_scheme="NAICS", source="internal",
                   table_version="v1")["id"]
    add_mappings(db, cid, [{"from_code": "43211500", "to_code": "334111"}])
    _factor(db, "334111", 2.0)
    out = chain_uncertainty(db, [
        {"from_scheme": "UNSPSC", "from_code": "43211500", "to_scheme": "NAICS"}])
    assert out["uncitable_hops"] == ["UNSPSC->NAICS"]


def test_the_chain_states_the_version_pinning_rule(db):
    a = _table(db, "CPA", "CPC", "2.1")
    add_mappings(db, a, [{"from_code": "A", "to_code": "B"}])
    _factor(db, "B", 1.0)
    out = chain_uncertainty(db, [
        {"from_scheme": "CPA", "from_code": "A", "to_scheme": "CPC"}])
    assert "freezes FX rates" in out["version_pinning_note"]


# --- effective resolution -----------------------------------------------------

def test_nominal_precision_is_distinguished_from_effective_precision(db):
    """1,016 NAICS-6 codes resolve to 281 distinct EPA factor values — wheat, corn,
    rice, dry pea and oilseed farming all return the same number."""
    for code in ("111140", "111150", "111160", "111130"):
        _factor(db, code, 3.007)          # identical value across four codes
    _factor(db, "327310", 9.9)
    r = effective_resolution(db, "USEEIO")
    assert r["codes"] == 5
    assert r["distinct_values"] == 2
    assert r["collapse_ratio"] == pytest.approx(2.5)
    assert "not effective precision" in r["note"]


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
    yield client, {"X-API-Key": key}
    main_mod.app.dependency_overrides.clear()


def test_register_and_resolve_end_to_end(env):
    client, hdr = env
    r = client.post("/crosswalks", headers=hdr, params={
        "from_scheme": "CPA", "to_scheme": "CPC", "source": "Eurostat",
        "table_version": "2.1", "licence": "open"})
    assert r.status_code == 200
    cid = r.json()["id"]
    client.post(f"/crosswalks/{cid}/mappings", headers=hdr,
                json=[{"from_code": "A01", "to_code": "011"}])
    body = client.get("/crosswalks/resolve", headers=hdr, params={
        "from_scheme": "CPA", "from_code": "A01", "to_scheme": "CPC"}).json()
    assert body["resolved"] is True and body["one_to_one"] is True


def test_an_unknown_scheme_is_a_400(env):
    client, hdr = env
    r = client.post("/crosswalks", headers=hdr, params={
        "from_scheme": "NONSENSE", "to_scheme": "CPC", "source": "x",
        "table_version": "1"})
    assert r.status_code == 400


def test_the_chain_endpoint_requires_a_non_empty_array(env):
    client, hdr = env
    assert client.post("/crosswalks/chain", headers=hdr, json=[]).status_code == 400


def test_the_uncitable_flag_reaches_the_api(env):
    client, hdr = env
    r = client.post("/crosswalks", headers=hdr, params={
        "from_scheme": "UNSPSC", "to_scheme": "NAICS", "source": "internal",
        "table_version": "v1"})
    assert r.json()["uncitable"] is True
    assert r.json()["uncitable_reason"]
