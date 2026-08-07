"""Which regimes compel a filing — and the doctrine that an unknown is never a "no"."""
import json

import pytest

from app.models import Organisation
from app.services import applicability as A
from app.services.applicability_rules import RULES


def _org(db, **kw):
    lists = {}
    for f in ("jurisdictions", "listed_markets"):
        if f in kw:
            lists[f] = json.dumps(kw.pop(f))
    o = Organisation(name=kw.pop("name", "Co"), **kw, **lists)
    db.add(o); db.commit(); db.refresh(o)
    return o


# --- the governing rule -----------------------------------------------------------------

def test_an_unknown_is_never_a_no(db):
    """'You don't have to file CSRD' is a legally consequential statement. Inferring it
    from an empty form field is the fail-open-on-a-disclosure failure in its purest form."""
    r = A.evaluate(_org(db, name="Blank"), RULES)
    assert r["counts"]["required"] == 0
    leaked = [x["framework"] for x in r["results"] if x["verdict"] == A.NOT_REQUIRED]
    assert not leaked, f"an unknown profile produced a definite NO for {leaked}"
    assert r["profile_complete"] is False
    assert set(r["profile_missing"]) >= {"employees", "annual_turnover", "jurisdictions"}
    # And the caveat has to say so out loud, not leave it to be inferred.
    assert any("could not be assessed" in c for c in r["caveats"])


def test_partial_knowledge_still_decides_what_it_can(db):
    """A definite failure settles a conjunctive test even with other inputs missing —
    otherwise every answer degrades to 'unknown' and the feature is useless."""
    org = _org(db, name="Tiny", employees=12, jurisdictions=["EU"])
    r = A.evaluate_one("csddd", RULES["csddd"], org)
    # 12 employees definitively fails the >5,000 limb of a conjunctive test.
    assert r["verdict"] == A.NOT_REQUIRED
    r2 = A.evaluate_one("esrs_e1", RULES["esrs_e1"], org)
    assert r2["verdict"] == A.NOT_REQUIRED


def test_a_missing_input_that_could_flip_the_answer_yields_unknown(db):
    """The mirror case: where the missing input COULD change the verdict, say so."""
    org = _org(db, name="Maybe", employees=5000, jurisdictions=["EU"],
               financials_currency="EUR")     # turnover unknown
    r = A.evaluate_one("esrs_e1", RULES["esrs_e1"], org)
    assert r["verdict"] == A.CANNOT_DETERMINE
    assert "annual_turnover" in r["missing_inputs"]


# --- nested logic: the shape a flat mode cannot express ---------------------------------

def test_esos_nested_test_is_not_any_2_of_3(db):
    """ESOS is `employees >= 250 OR (turnover > 44m AND balance sheet > 38m)`. Flattening
    it to any-2-of-3 — a different regime's shape — silently changes who is in scope."""
    ev = lambda o: A.evaluate_one("esos", RULES["esos"], o)["verdict"]
    # Headcount alone qualifies, with both financial limbs failing.
    assert ev(_org(db, name="E1", employees=300, annual_turnover=1e6,
                   balance_sheet_total=1e6, financials_currency="GBP",
                   jurisdictions=["UK"])) == A.REQUIRED
    # One financial limb alone does NOT — this is exactly where any-2-of-3 would differ,
    # since turnover + a near-miss headcount would wrongly pass there.
    assert ev(_org(db, name="E2", employees=10, annual_turnover=50_000_000,
                   balance_sheet_total=1_000_000, financials_currency="GBP",
                   jurisdictions=["UK"])) == A.NOT_REQUIRED
    # Both financial limbs together do.
    assert ev(_org(db, name="E3", employees=10, annual_turnover=50_000_000,
                   balance_sheet_total=40_000_000, financials_currency="GBP",
                   jurisdictions=["UK"])) == A.REQUIRED
    # Unknown turnover with a passing balance sheet is undecidable, not a no.
    assert ev(_org(db, name="E4", employees=10, balance_sheet_total=40_000_000,
                   financials_currency="GBP", jurisdictions=["UK"])) == A.CANNOT_DETERMINE


def test_any_2_of_3_decides_early_in_both_directions(db):
    """Two passes is a yes whatever is missing; two definite fails is a no."""
    # listed_markets=[] is "answered: unlisted" — required, since SECR catches quoted
    # companies at any size and silence about listing cannot rule it out.
    ev = lambda o: A.evaluate_one("secr", RULES["secr"], o)["verdict"]
    assert ev(_org(db, name="S1", employees=300, annual_turnover=40_000_000,
                   financials_currency="GBP", jurisdictions=["UK"],
                   listed_markets=[])) == A.REQUIRED
    assert ev(_org(db, name="S2", employees=10, annual_turnover=1_000_000,
                   financials_currency="GBP", jurisdictions=["UK"],
                   listed_markets=[])) == A.NOT_REQUIRED
    assert ev(_org(db, name="S3", employees=300, jurisdictions=["UK"],
                   listed_markets=[])) == A.CANNOT_DETERMINE


# --- territory, listing, currency --------------------------------------------------------

def test_out_of_territory_is_distinct_from_not_required(db):
    """A UK-only entity is not 'below the California threshold' — the regime simply does
    not reach it, and collapsing the two would mislead an entity that later expands."""
    org = _org(db, name="UKonly", annual_turnover=5e9, employees=9000,
               financials_currency="USD", jurisdictions=["UK"])
    assert A.evaluate_one("sb253", RULES["sb253"], org)["verdict"] == A.OUT_OF_TERRITORY
    ca = _org(db, name="CACo", annual_turnover=5e9, employees=9000,
              financials_currency="USD", jurisdictions=["US-CA"])
    assert A.evaluate_one("sb253", RULES["sb253"], ca)["verdict"] == A.REQUIRED


def test_no_stated_jurisdiction_is_unknown_not_exempt(db):
    """An entity that has stated no territory must not be told nothing applies to it."""
    org = _org(db, name="NoJur", employees=9000, annual_turnover=5e9,
               financials_currency="EUR")
    r = A.evaluate_one("esrs_e1", RULES["esrs_e1"], org)
    assert r["verdict"] == A.CANNOT_DETERMINE
    assert r["missing_inputs"] == ["jurisdictions"]


def test_listing_catches_an_entity_below_every_size_limb(db):
    """SECR catches quoted companies at any size — a size-only model would miss them."""
    org = _org(db, name="SmallQuoted", employees=8, annual_turnover=900_000,
               balance_sheet_total=400_000, financials_currency="GBP",
               jurisdictions=["UK"], listed_markets=["UK-OFFICIAL"])
    r = A.evaluate_one("secr", RULES["secr"], org)
    assert r["verdict"] == A.REQUIRED
    assert "listing" in r["reason"]


def test_an_unconvertible_currency_refuses_rather_than_guesses(db):
    """A EUR threshold tested against a GBP turnover without a rate is simply a wrong
    legal answer, so the comparison is refused rather than approximated."""
    org = _org(db, name="GBPCo", employees=2000, annual_turnover=800_000_000,
               financials_currency="GBP", jurisdictions=["EU"])
    assert A.evaluate_one("esrs_e1", RULES["esrs_e1"], org)["verdict"] == A.CANNOT_DETERMINE
    r = A.evaluate_one("esrs_e1", RULES["esrs_e1"], org, rates={("GBP", "EUR"): 1.18})
    assert r["verdict"] == A.REQUIRED
    assert any(t.get("actual") == pytest.approx(944_000_000) for t in r["tests"])


# --- kinds must not be conflated ---------------------------------------------------------

def test_methodology_standards_never_carry_a_threshold(db):
    """You never 'owe' a filing under ISO 14064-1. A size test here is a category error."""
    org = _org(db, name="M", employees=9000, annual_turnover=5e9,
               financials_currency="EUR", jurisdictions=["EU"])
    for key, rule in RULES.items():
        if rule["kind"] != "methodology":
            continue
        assert not rule.get("size_tests") and not rule.get("size_expr"), \
            f"{key} is methodology but carries a threshold"
        assert A.evaluate_one(key, rule, org)["verdict"] == A.NOT_A_FILING


def test_voluntary_schemes_are_never_reported_as_legally_required(db):
    org = _org(db, name="V", employees=9000, annual_turnover=5e9,
               financials_currency="EUR", jurisdictions=["EU", "UK", "US-CA"])
    r = A.evaluate(org, RULES)
    for res in r["results"]:
        if RULES[res["framework"]]["kind"] == "voluntary":
            assert res["verdict"] == A.VOLUNTARY_SCHEME
    # ...and the distinction is explained rather than asserted.
    assert all(RULES[k].get("voluntary_note")
               for k, v in RULES.items() if v["kind"] == "voluntary")


def test_activity_based_regimes_do_not_pretend_size_decides_them(db):
    """CBAM catches a two-person importer and misses a 50,000-employee services group."""
    tiny = _org(db, name="TinyImporter", employees=2, annual_turnover=100_000,
                financials_currency="EUR", jurisdictions=["EU"])
    huge = _org(db, name="HugeServices", employees=50_000, annual_turnover=9e9,
                financials_currency="EUR", jurisdictions=["EU"])
    for org in (tiny, huge):
        r = A.evaluate_one("cbam", RULES["cbam"], org)
        assert r["verdict"] == A.ACTIVITY_BASED
        assert "import" in r["reason"]


# --- enforceability ----------------------------------------------------------------------

def test_an_enjoined_provision_is_flagged_not_silently_reported_as_due(db):
    """A statutory date read alone overstates the duty when a court has suspended it."""
    org = _org(db, name="CABig", employees=9000, annual_turnover=5e9,
               financials_currency="USD", jurisdictions=["US-CA"])
    r = A.evaluate_one("sb261", RULES["sb261"], org)
    assert r["verdict"] == A.REQUIRED          # scope stands: an injunction can be lifted
    assert r["enforceable"] is False
    assert r["status"] == A.ENJOINED
    assert "enjoined" in r["reason"]
    full = A.evaluate(org, RULES)
    assert "sb261" in full["required_but_not_enforceable"]
    assert any("not presently enforceable" in c for c in full["caveats"])
    # SB 253 was NOT enjoined — the two must not be lumped together.
    assert A.evaluate_one("sb253", RULES["sb253"], org)["enforceable"] is True


# --- the ruleset itself ------------------------------------------------------------------

def test_every_mandatory_rule_carries_a_citation_and_a_test(db):
    """A mandatory verdict with no citation is an assertion the reader cannot check."""
    for key, rule in RULES.items():
        if rule["kind"] not in ("mandatory", "conditional"):
            continue
        assert rule.get("citation"), f"{key} has no citation"
        assert rule.get("summary"), f"{key} has no summary"
        if rule["kind"] == "mandatory":
            assert rule.get("size_tests") or rule.get("size_expr"), \
                f"{key} is mandatory but has no size test"


def test_thresholds_are_well_formed(db):
    """A typo'd operator or an unknown metric would silently never fire."""
    def walk(node, key):
        if "metric" in node:
            assert node["metric"] in A.METRICS, f"{key}: unknown metric {node['metric']}"
            assert node["operator"] in A._OPS, f"{key}: bad operator {node['operator']}"
            assert isinstance(node["value"], (int, float)) and node["value"] >= 0
            assert node.get("unit"), f"{key}: threshold has no unit"
            return
        assert node["op"] in (A.ALL, A.ANY, A.ANY_2_OF_3), f"{key}: bad op {node['op']}"
        assert node["of"], f"{key}: empty expression"
        for c in node["of"]:
            walk(c, key)

    for key, rule in RULES.items():
        if rule.get("size_expr"):
            walk(rule["size_expr"], key)
        for t in rule.get("size_tests") or []:
            walk(t, key)
        if rule.get("size_logic"):
            assert rule["size_logic"] in (A.ALL, A.ANY, A.ANY_2_OF_3)


def test_rules_that_cannot_test_the_whole_legal_test_say_so(db):
    """The limits are the point. A rule that ignores a real limb of the statute without
    saying so returns a confident wrong answer."""
    # Every rule whose statute has a two-consecutive-year or non-profile gate must
    # disclose it — these are the ones the research explicitly flagged.
    for key in ("csddd", "esos", "eu_ets", "uk_ets", "issb_s2", "sfdr"):
        assert RULES[key].get("model_limitations"), f"{key} hides a known limitation"
    # ...and the limitation must survive into the payload the caller sees.
    org = _org(db, name="LimCo", employees=9000, annual_turnover=5e9,
               financials_currency="EUR", jurisdictions=["EU"])
    r = A.evaluate_one("csddd", RULES["csddd"], org)
    assert "TWO CONSECUTIVE" in (r["model_limitations"] or "")


def test_evaluation_is_indicative_and_says_so(db):
    r = A.evaluate(_org(db, name="CaveatCo", jurisdictions=["EU"]), RULES)
    assert any("not legal advice" in c for c in r["caveats"])
    assert any("never a 'no'" in c for c in r["caveats"])


def test_unknown_listing_status_cannot_rule_out_a_listing_caught_regime(db):
    """SECR catches quoted companies at ANY size, so silence about listing must not
    produce a definite 'below the threshold'. Storing NULL (never answered) and []
    (answered: unlisted) identically is what made a tiny quoted company read as exempt."""
    tiny = dict(employees=8, annual_turnover=900_000, balance_sheet_total=400_000,
                financials_currency="GBP", jurisdictions=["UK"])
    unknown = A.evaluate_one("secr", RULES["secr"], _org(db, name="Silent", **tiny))
    assert unknown["verdict"] == A.CANNOT_DETERMINE
    assert "listed_markets" in unknown["missing_inputs"]
    # ...and once answered "unlisted", the size test may settle it.
    stated = A.evaluate_one("secr", RULES["secr"],
                            _org(db, name="Unlisted", listed_markets=[], **tiny))
    assert stated["verdict"] == A.NOT_REQUIRED
    # The summary must count the gap rather than call the profile complete.
    r = A.evaluate(_org(db, name="Silent2", **tiny), RULES)
    assert "listed_markets" in r["profile_missing"]
    assert r["profile_complete"] is False


def test_a_sub_national_regime_is_not_ruled_out_by_naming_only_the_parent(db):
    """'We operate in the US' does not say whether that includes California."""
    us = _org(db, name="USCo", employees=9000, annual_turnover=5e9,
              financials_currency="USD", jurisdictions=["US"], listed_markets=[])
    r = A.evaluate_one("sb253", RULES["sb253"], us)
    assert r["verdict"] == A.CANNOT_DETERMINE
    assert "California" in r["reason"], r["reason"]
    # Naming a wholly unrelated territory IS a definite out-of-territory.
    uk = _org(db, name="UKCo2", employees=9000, annual_turnover=5e9,
              financials_currency="USD", jurisdictions=["UK"], listed_markets=[])
    assert A.evaluate_one("sb253", RULES["sb253"], uk)["verdict"] == A.OUT_OF_TERRITORY


def test_money_without_a_currency_is_never_compared(db):
    """600,000,000 is EUR 600m or JPY 600m (~EUR 3.5m); the tool cannot tell."""
    org = _org(db, name="NoCur", employees=2000, annual_turnover=600_000_000,
               jurisdictions=["EU"], listed_markets=[])
    r = A.evaluate_one("esrs_e1", RULES["esrs_e1"], org)
    assert r["verdict"] == A.CANNOT_DETERMINE
    assert any("no currency" in (t.get("why") or "") for t in r["tests"])


def test_the_rules_own_legal_note_survives_into_the_payload(db):
    """The gloss ('exceed' is strictly greater than) was being overwritten by the
    currency-conversion note, so the UI rendered nothing."""
    org = _org(db, name="NoteCo", employees=2000, annual_turnover=600e6,
               financials_currency="EUR", jurisdictions=["EU"], listed_markets=[])
    r = A.evaluate_one("esrs_e1", RULES["esrs_e1"], org)
    notes = [t.get("note") for t in r["tests"]]
    assert all(notes), f"a rule's authored note was lost: {notes}"
    assert any("strictly greater" in n for n in notes)


def test_an_unrecognised_kind_raises_rather_than_inventing_an_obligation(db):
    """A typo'd kind used to fall through to the size evaluator, and a rule with no
    tests then returned REQUIRED — a legal duty conjured from a spelling mistake."""
    org = _org(db, name="KindCo", jurisdictions=["EU"], listed_markets=[])
    with pytest.raises(ValueError, match="unknown kind"):
        A.evaluate_one("typo", {"kind": "voluntry", "label": "X"}, org)


def test_any_2_of_3_stays_sound_with_more_than_three_limbs():
    """Generalised so a future four-limb rule cannot silently produce a wrong negative."""
    ev = lambda vals: A._eval_expr(
        {"op": A.ANY_2_OF_3, "of": [{"metric": "employees", "operator": ">",
                                     "value": 0, "unit": "headcount"}] * 0 or []},
        {}, None, [], [])
    # Direct check of the combinator via a stub profile.
    def combine(vals):
        node = {"op": A.ANY_2_OF_3, "of": [{"metric": f"m{i}"} for i in range(len(vals))]}
        prof, seq = {}, list(vals)
        import app.services.applicability as mod
        orig = mod._test_one
        mod._test_one = lambda t, p, r: (seq.pop(0), {})
        try:
            return mod._eval_expr(node, prof, None, [], [])
        finally:
            mod._test_one = orig
    assert combine([False, False, True, None]) is None   # the unknown could make two
    assert combine([True, True, False, False]) is True
    assert combine([False, False, False, False]) is False


# --- HTTP layer: the guards that only exist at the boundary -----------------------------

@pytest.fixture
def http():
    """A real client over its own in-memory DB, matching tests/test_api.py's pattern."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from fastapi.testclient import TestClient
    from app.database import Base
    import app.models  # noqa: F401
    from app import main as main_mod

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    main_mod.app.dependency_overrides[main_mod.get_db] = lambda: Session()
    client = TestClient(main_mod.app)
    key = client.post("/organisations", params={"name": "HttpCo"}).json()["api_key"]
    yield client, {"X-API-Key": key}
    main_mod.app.dependency_overrides.clear()


def test_applicability_endpoint_returns_a_full_assessment(http):
    c, h = http
    r = c.get("/applicability", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["counts"]["required"] == 0          # a blank profile decides nothing
    assert body["profile_complete"] is False
    assert any("not legal advice" in x for x in body["caveats"])
    v = c.get("/applicability/vocabulary").json()
    assert "EU" in v["jurisdictions"] and "UK-OFFICIAL" in v["listing_markets"]


def test_money_without_currency_is_rejected_at_the_boundary(http):
    """The only control stopping an untestable figure being stored."""
    c, h = http
    bad = c.post("/organisations/profile", params={"annual_turnover": 600_000_000},
                 headers=h)
    assert bad.status_code == 400
    assert "financials_currency" in bad.json()["detail"]
    ok = c.post("/organisations/profile",
                params={"annual_turnover": 600_000_000, "financials_currency": "EUR"},
                headers=h)
    assert ok.status_code == 200 and ok.json()["annual_turnover"] == 600_000_000


def test_profile_rejects_unknown_vocabulary_and_bad_numbers(http):
    c, h = http
    for params in ({"jurisdictions": "MARS"}, {"listed_markets": "MOON"},
                   {"employees": -5}, {"financials_currency": "XYZ"},
                   {"financials_as_of": "not-a-date"}, {"sector": "crypto-yoga"}):
        r = c.post("/organisations/profile", params=params, headers=h)
        assert r.status_code == 400, f"{params} was accepted"


def test_empty_listed_markets_records_unlisted_rather_than_unknown(http):
    """'' is how a caller says "answered: none". Without it, regimes that catch quoted
    companies at any size stay permanently undecidable."""
    c, h = http
    c.post("/organisations/profile",
           params={"employees": 10, "annual_turnover": 900_000,
                   "balance_sheet_total": 400_000, "financials_currency": "GBP",
                   "jurisdictions": "UK"}, headers=h)
    secr = next(x for x in c.get("/applicability", headers=h).json()["results"]
                if x["framework"] == "secr")
    assert secr["verdict"] == A.CANNOT_DETERMINE
    r = c.post("/organisations/profile", params={"listed_markets": ""}, headers=h)
    assert r.status_code == 200 and r.json()["listed_markets"] == []
    secr = next(x for x in c.get("/applicability", headers=h).json()["results"]
                if x["framework"] == "secr")
    assert secr["verdict"] == A.NOT_REQUIRED


def test_partial_profile_updates_do_not_clear_untouched_fields(http):
    c, h = http
    c.post("/organisations/profile",
           params={"employees": 1200, "annual_turnover": 600_000_000,
                   "financials_currency": "EUR"}, headers=h)
    r = c.post("/organisations/profile", params={"jurisdictions": "EU"}, headers=h).json()
    assert r["employees"] == 1200 and r["annual_turnover"] == 600_000_000
    assert r["jurisdictions"] == ["EU"]
