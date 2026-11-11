"""A factor whose unit the activity cannot convert into is not a match.

`resolver.py` opens by promising that "only an EXACT match (category +
subcategory-as-given + geography) may bind a factor automatically" — and that
list is the whole defect. The unit is not in it, and one
(category, subcategory, geography, vintage) identity routinely holds several
rows that differ ONLY in unit: `loaders/base.py` keeps unit in the supersession
key so unit-of-measure siblings all stay live, and `loaders/defra.py` emits one
row per unit. The pick among them was `EmissionFactor.id.desc()` — publication
order in the source file.

Reproduced on the real DESNZ shape before the fix: a kWh gas bill bound the
cubic-metre factor at basis 'exact', confidence 1.0, status 'auto', and the run
then reported `total_co2e 0.0` with `status 'complete'` and the line counted in
`unit_errors`. Both halves matter — the wrong binding, and the zero presented as
a finished answer.

`units.are_compatible()` existed the whole time with ZERO production callers;
these tests are also what stops it going back to being dead code.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  (registers tables on Base.metadata)
from app.models import ActivityRecord, EmissionFactor, Organisation
from app.services.calc import compute_co2e
from app.services.resolver import auto_map_activity, propose_mapping


@pytest.fixture
def db():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    try:
        yield s
    finally:
        s.close()


def _org(db, name="Acme"):
    o = Organisation(name=name, api_key_hash="h")
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, *, unit, value, category="natural_gas", subcategory="", geo="GB",
            method="average_data", gwp="AR6"):
    f = EmissionFactor(source="DEFRA", version="2024", geography=geo, year=2024,
                       category=category, subcategory=subcategory, unit=unit,
                       gwp_set=gwp, value=value, method_type=method)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _activity(db, org, *, unit, qty=100000.0, category="natural_gas", geo="GB"):
    a = ActivityRecord(organisation_id=org.id, date="2025-06-01", category=category,
                       subcategory="", description="site gas", quantity=qty,
                       unit=unit, geo=geo)
    db.add(a); db.commit(); db.refresh(a)
    return a


# --- the defect itself -------------------------------------------------------------------

def test_a_kwh_activity_does_not_bind_the_cubic_metre_factor(db):
    """The DESNZ shape: natural gas published per kWh AND per cubic metre."""
    org = _org(db)
    kwh = _factor(db, unit="kWh", value=0.18290)
    m3 = _factor(db, unit="m3", value=2.02266)      # higher id — wins on order alone
    act = _activity(db, org, unit="kWh")

    status = auto_map_activity(db, act)
    assert act.factor_id == kwh.id, "bound a factor whose unit the activity cannot convert into"
    assert act.factor_id != m3.id
    assert status == "auto" and act.mapping_confidence == 1.0


def test_the_wrongly_bound_line_used_to_vanish_from_the_inventory(db):
    """The consequence, end to end: the figure must be computed, not silently zero."""
    org = _org(db)
    _factor(db, unit="kWh", value=0.18290)
    _factor(db, unit="m3", value=2.02266)
    act = _activity(db, org, unit="kWh", qty=100000.0)
    auto_map_activity(db, act)

    run = compute_co2e(db, org.id)
    assert run.unit_errors == 0
    assert run.mapped == 1
    assert run.total_co2e == pytest.approx(100000.0 * 0.18290)


def test_order_alone_does_not_decide_when_the_sibling_is_published_first(db):
    """The mirror case: the usable factor has the HIGHER id, so a fix that merely
    reversed the ordering would pass the test above and fail this one."""
    org = _org(db)
    _factor(db, unit="m3", value=2.02266)
    kwh = _factor(db, unit="kWh", value=0.18290)
    act = _activity(db, org, unit="kWh")
    auto_map_activity(db, act)
    assert act.factor_id == kwh.id


# --- compatible is not the same as identical ---------------------------------------------

def test_a_convertible_unit_still_binds(db):
    """MWh -> kWh is a conversion the engine performs; requiring an identical
    string would reject a perfectly usable factor."""
    org = _org(db)
    mwh = _factor(db, unit="MWh", value=182.90, category="electricity")
    act = _activity(db, org, unit="kWh", qty=1000.0, category="electricity")
    auto_map_activity(db, act)
    assert act.factor_id == mwh.id


def test_a_spend_factor_in_the_same_currency_still_binds(db):
    """Currency never reaches pint; `are_compatible` short-circuits on identity,
    so spend-based resolution is unaffected."""
    org = _org(db)
    f = _factor(db, unit="GBP", value=0.31, category="consultancy",
                method="spend_based")
    act = _activity(db, org, unit="GBP", qty=50000.0, category="consultancy")
    auto_map_activity(db, act)
    assert act.factor_id == f.id


def test_a_spend_factor_in_another_currency_does_not_bind(db):
    """Cross-currency needs an audited FX rate; binding it would invent one."""
    org = _org(db)
    _factor(db, unit="EUR", value=0.31, category="consultancy", method="spend_based")
    act = _activity(db, org, unit="GBP", qty=50000.0, category="consultancy")
    status = auto_map_activity(db, act)
    assert act.factor_id is None and status == "unmapped"


def test_a_currency_activity_does_not_bind_a_physical_factor(db):
    org = _org(db)
    _factor(db, unit="kWh", value=0.18, category="consultancy")
    act = _activity(db, org, unit="GBP", qty=50000.0, category="consultancy")
    assert auto_map_activity(db, act) == "unmapped"
    assert act.factor_id is None


# --- an unknown unit is not a licence to guess -------------------------------------------

def test_an_activity_with_no_unit_does_not_auto_bind(db):
    """Nothing is compatible with an unknown unit, so it routes to a human rather
    than binding on publication order."""
    org = _org(db)
    _factor(db, unit="kWh", value=0.18290)
    act = _activity(db, org, unit="")
    status = auto_map_activity(db, act)
    assert act.factor_id is None
    assert status in ("unmapped", "needs_review")


# --- the parameter is opt-in, so existing callers keep their behaviour --------------------

def test_propose_mapping_without_a_unit_is_unchanged(db):
    """`unit=None` means the caller stated no constraint — the behaviour every
    caller had before this change."""
    _org(db)
    _factor(db, unit="m3", value=2.02266)
    hit = propose_mapping(db, "natural_gas", "", None, "GB")
    assert hit is not None and hit[1] == "exact"


def test_propose_mapping_with_a_unit_filters(db):
    _org(db)
    _factor(db, unit="m3", value=2.02266)
    assert propose_mapping(db, "natural_gas", "", None, "GB", unit="kWh") is None


# --- the dead-code guard -----------------------------------------------------------------

def test_are_compatible_is_actually_called_in_production(db):
    """It was defined, correct, and called by nothing but its own unit test, while
    the defect it would have caught shipped. If the resolver stops consulting it,
    this fails.

    COUNTED BY AST, NOT BY GREP, and the difference is the whole point. A text
    search matches the `from .units import are_compatible` line, so it stays green
    when the import is left behind and the CALL is gone — which is precisely the
    state this is meant to detect. Verified: with the call removed and the import
    kept, the grep version passed and this one fails.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    callers = set()
    for path in root.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:                       # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if (getattr(fn, "id", None) or getattr(fn, "attr", None)) == "are_compatible":
                    callers.add(path.name)

    assert callers, "units.are_compatible() is defined but called nowhere in app/"
    assert "resolver.py" in callers, \
        f"the resolver no longer checks unit compatibility (callers: {sorted(callers)})"
