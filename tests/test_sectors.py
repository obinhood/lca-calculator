"""The reporting entity's own sector routes challenges; it never changes a number."""
import pytest

from app.services import sectors


def test_taxonomy_and_relevance_map_are_consistent():
    # Guards the module-level asserts stay meaningful under refactoring.
    assert set(sectors.SCOPE3_RELEVANCE) == set(sectors.SECTORS)
    for sector, mapping in sectors.SCOPE3_RELEVANCE.items():
        for cat, rel in mapping.items():
            assert 1 <= cat <= 15, f"{sector} references category {cat}"
            assert rel in (sectors.DOMINANT, sectors.TYPICAL, sectors.MINOR)
    # Every sector except the escape hatch must actually commit to something; a sector
    # with no dominant category would silently never challenge anything.
    for sector in sectors.SECTORS:
        if sector != "other":
            assert sectors.dominant_categories(sector), f"{sector} has no dominant category"


def test_unknown_sector_is_minor_never_an_exemption():
    assert sectors.relevance(None, 1) == sectors.MINOR
    assert sectors.relevance("not-a-sector", 1) == sectors.MINOR
    assert sectors.relevance("other", 15) == sectors.MINOR
    assert sectors.is_valid("not-a-sector") is False
    assert sectors.is_valid("financial_services") is True


def test_financial_services_must_defend_excluding_category_15():
    assert sectors.relevance("financial_services", 15) == sectors.DOMINANT
    assert 15 in sectors.dominant_categories("financial_services")
    # ...and a professional-services firm is not challenged on financed emissions.
    assert sectors.relevance("professional_services", 15) == sectors.MINOR


def test_catalogue_is_orderable_and_puts_other_last():
    rows = sectors.catalogue()
    assert len(rows) == len(sectors.SECTORS)
    assert rows[-1]["key"] == "other"
    assert all({"key", "label", "note", "dominant_scope3"} <= set(r) for r in rows)


# --- the load-bearing claim: no sector code multiplies, scales or estimates a figure ---

def test_no_scaling_arithmetic_anywhere_in_the_sector_module():
    """The module's whole premise is that it routes and never computes. If a future edit
    adds a factor, uplift or estimate here, that premise is silently gone.

    Scoped to the SCALING operators (* / **) in both plain and augmented form — `x *= f`
    is the most natural way to write the exact thing being forbidden, and an earlier
    version of this test missed it. Add/Sub are deliberately excluded: string and list
    concatenation are legitimate here, and flagging them creates pressure to route around
    the check. This catches the syntactic shape, not every conceivable smuggling route
    (`operator.mul`, `math.prod`, a bare int table scaled in another module) — the
    end-to-end guarantee is `test_sector_changes_no_figure_end_to_end` below.
    """
    import ast
    import inspect
    SCALING = (ast.Mult, ast.Div, ast.FloorDiv, ast.Pow, ast.MatMult)
    tree = ast.parse(inspect.getsource(sectors))
    bad = [n for n in ast.walk(tree)
           if (isinstance(n, ast.BinOp) and isinstance(n.op, SCALING))
           or (isinstance(n, ast.AugAssign) and isinstance(n.op, SCALING))]
    assert not bad, f"sectors.py performs scaling arithmetic ({len(bad)} op(s)) — it must not"


# --- integration: the prior must actually FIRE on a real run ---------------------------

from app.models import ActivityRecord, EmissionFactor, Organisation   # noqa: E402
from app.services.calc import compute_co2e                            # noqa: E402
from app.services.ghgp import scope3_completeness                     # noqa: E402
from tests.scope3_util import ready_run                                # noqa: E402
from app.services.calc import compute_co2e as _recompute               # noqa: E402


def _ready(db, sector=None, name="Co"):
    org = Organisation(name=name, sector=sector)
    db.add(org); db.commit(); db.refresh(org)
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category="electricity", subcategory="", unit="kWh",
                       gwp_set="AR6", value=1.0)
    db.add(f); db.commit(); db.refresh(f)
    db.add(ActivityRecord(organisation_id=org.id, date="2025-03-01", category="electricity",
                          subcategory="", description="", quantity=1000.0, unit="kWh",
                          geo="GB", factor_id=f.id))
    db.commit()
    run, period = ready_run(db, org.id)
    return org, run, period


def test_sector_challenge_fires_on_a_dominant_excluded_category(db):
    """A bank whose screening declares Category 15 not_applicable — the exact shape of
    the most consequential Scope 3 omission there is — must be blocked."""
    org, run, _p = _ready(db, sector="financial_services", name="Bank")
    r = scope3_completeness(db, run)
    assert r["sector"] == "financial_services"
    assert r["sector_prior_applied"] is True
    assert 15 in r["sector_dominant_categories"]
    hits = [b for b in r["blockers"] if "category 15" in b and "DOMINATES" in b]
    assert hits, r["blockers"]
    assert "GHGP Scope 3 Ch.6" in hits[0]


def test_same_run_without_a_sector_raises_no_sector_challenge(db):
    """The prior is the ONLY difference — proving it is load-bearing and not incidental."""
    org, run, _p = _ready(db, sector=None, name="Unstated")
    r = scope3_completeness(db, run)
    assert r["sector"] is None
    assert r["sector_prior_applied"] is False
    # The very same screening that a bank is blocked on raises nothing without a sector.
    assert not [b for b in r["blockers"] if "DOMINATES" in b]


def test_a_sector_where_the_category_is_minor_raises_no_challenge(db):
    """Category 15 is dominant for a bank and minor for a consultancy; the same
    declaration must be challenged in one and not the other."""
    org, run, _p = _ready(db, sector="professional_services", name="Consultancy")
    r = scope3_completeness(db, run)
    assert r["sector_prior_applied"] is True
    assert not [b for b in r["blockers"] if "category 15" in b and "DOMINATES" in b]


def test_the_frozen_sector_wins_over_a_later_profile_edit(db):
    """Run immutability: editing the organisation's sector must not retroactively change
    what an already-frozen run's screening was challenged against."""
    cat15 = lambda res: [b for b in res["blockers"]
                         if "category 15" in b and "DOMINATES" in b]
    org, run, _p = _ready(db, sector="professional_services", name="Pivoting Co")
    assert not cat15(scope3_completeness(db, run))    # Cat 15 is minor for this sector
    org.sector = "financial_services"                 # profile edited AFTER the run froze
    db.commit()
    r = scope3_completeness(db, run)
    assert r["sector"] == "professional_services"     # the run kept its own basis
    assert not cat15(r)                               # ...so no financed-emissions challenge


def test_an_unrecognised_sector_is_not_frozen_and_is_reported_as_no_prior(db):
    """A free-text sector must never look like a prior that ran. It is dropped at freeze
    time and the payload says plainly that no sector challenge applied."""
    org, run, _p = _ready(db, sector="crypto-yoga-fusion", name="Freetext Co")
    r = scope3_completeness(db, run)
    assert run.organisation_sector is None
    assert r["sector_prior_applied"] is False


def _set_criterion(db, org_id, cat, note):
    """Answer the `sector_guidance` criterion on one category's live declaration."""
    import json
    from app.models import Scope3CategoryDeclaration
    from app.services.ghgp import SEVEN_CRITERIA
    d = db.query(Scope3CategoryDeclaration).filter(
        Scope3CategoryDeclaration.organisation_id == org_id,
        Scope3CategoryDeclaration.category == cat).first()
    crit = {k: {"met": False, "note": "screened immaterial"} for k in SEVEN_CRITERIA}
    crit["sector_guidance"] = {"met": True, "note": note}
    d.criteria = json.dumps(crit)
    db.commit()


def test_the_sector_challenge_can_be_discharged_with_entity_specific_evidence(db):
    """The challenge must have an exit. An insurance intermediary genuinely holds no
    portfolio; a wall would block its truthful not_applicable while leaving `included`
    with no data passing at 100% coverage — the doctrine exactly inverted."""
    org, run, period = _ready(db, sector="financial_services", name="Broker Ltd")
    cat15 = lambda r: [b for b in r["blockers"] if "category 15" in b and "DOMINATES" in b]
    assert cat15(scope3_completeness(db, run))            # undefended -> blocked

    _set_criterion(db, org.id, 15,
                   "We are an insurance intermediary: all PCAF asset classes A-G are nil "
                   "and we hold no balance-sheet investment exposure (statutory accounts "
                   "note 12). The sector pattern does not apply to this entity.")
    run2 = _recompute(db, org.id, reporting_period_id=period.id)
    assert not cat15(scope3_completeness(db, run2))       # defended -> discharged


def test_boilerplate_does_not_discharge_the_sector_challenge(db):
    """The exit must be evidence, not a keystroke."""
    org, run, period = _ready(db, sector="financial_services", name="Lazy Bank")
    for note in ("n/a", "not applicable", "", "no data"):
        _set_criterion(db, org.id, 15, note)
        run2 = _recompute(db, org.id, reporting_period_id=period.id)
        assert [b for b in scope3_completeness(db, run2)["blockers"]
                if "category 15" in b and "DOMINATES" in b], f"{note!r} discharged it"


def test_a_run_with_no_sector_prior_says_so_in_its_completeness_statement(db):
    """Fail closed on the DISCLOSURE: a screening judged with one of the seven relevance
    criteria switched off is a weaker assertion, and a reader must be told."""
    org, run, _p = _ready(db, sector=None, name="No Sector Co")
    r = scope3_completeness(db, run)
    assert r["sector_prior_applied"] is False
    assert [w for w in r["warnings"] if "no sector prior" in w]
    # ...and a run WITH a prior carries no such caveat.
    org2, run2, _p2 = _ready(db, sector="manufacturing", name="With Sector Co")
    assert not [w for w in scope3_completeness(db, run2)["warnings"]
                if "no sector prior" in w]


def test_the_completeness_payload_actually_reaches_the_report(db):
    """The disclosure fields were previously dropped by every consumer, making the
    caveat unreachable from the product."""
    from app.reports.scope3 import scope3_by_ghgp_category
    org, run, _p = _ready(db, sector="manufacturing", name="Reported Co")
    c = scope3_by_ghgp_category(db, run)["completeness"]
    assert c["sector"] == "manufacturing"
    assert c["sector_label"] == "Manufacturing & industrial goods"
    assert c["sector_prior_applied"] is True
    assert 1 in c["sector_dominant_categories"]


def test_sector_changes_no_figure_end_to_end(db):
    """The load-bearing claim, verified where it matters rather than by reading source:
    two identical inventories differing ONLY in sector must agree on every number."""
    org, _run, period = _ready(db, sector=None, name="Fig Co")

    def figures(sector):
        org.sector = sector
        db.commit()
        run = _recompute(db, org.id, reporting_period_id=period.id)
        from app.reports.summary import summary
        from app.reports.scope3 import scope3_by_ghgp_category
        out = []
        def walk(o):
            if isinstance(o, dict):
                for k in sorted(o):
                    if "sector" not in k:                  # the label itself may differ
                        walk(o[k])
            elif isinstance(o, list):
                for v in o:
                    walk(v)
            elif isinstance(o, float):     # ids and counts are ints; figures are floats
                out.append(o)
        walk(summary(db, org.id, run_id=run.id))
        walk(scope3_by_ghgp_category(db, run)["categories"])
        return out

    none_ = figures(None)
    fin = figures("financial_services")
    mfg = figures("manufacturing")
    assert len(none_) > 50, "fixture produced too few figures to be a real check"
    assert none_ == fin == mfg
