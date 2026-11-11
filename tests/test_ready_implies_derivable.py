"""No renderer may present itself as ready over a figure the engine inferred.

THE PHASE 2 EXIT GATE. Every finding this phase closed was an instance of one shape —
a check reporting a property as held without having been able to observe it:

    SECR         disclosure_ready: true with Scope 1 = Scope 2 = 0.0
    not_material all 15 categories excluded, the threshold never compared
    AR6 default  total_co2e 0.0 reported as status 'complete'
    resolver     a m3 factor bound to a kWh bill at confidence 1.0
    SBTi         the 5% test set by emissions provably not in Scope 3

Each was fixed where it was found, and each fix was wired renderer by renderer — eight
renderers calling `scope_declaration_completeness`, added across two pull requests, by
hand. That is the arrangement this file exists to replace. Eight renderers gated by hand
is eight things someone remembered; the ninth is a renderer nobody thought of.

The ninth was `scope3_inventory`, and it is worth saying why it was missed: nobody reads
a SCOPE 3 report as publishing a Scope 1/2 split. It does — as the two denominator terms
every category's materiality is judged against — and it published them at 0.0 by guess,
`disclosure_ready: true`, no blockers. This file found it on its first run.

So the property is asserted over `export.BUILDERS` — the registry the API itself
dispatches on — rather than over a list maintained here. A framework added to that
registry is covered by this file the moment it is added, and if it cannot be covered the
coverage test below fails and says so by name.

THE LIMIT OF THAT: coverage follows the registry, so a renderer served OUTSIDE it is
outside this gate and `test_every_builder_is_covered_or_named` cannot name it either —
it iterates BUILDERS, so a renderer absent from BUILDERS is absent from its view.
`sbti_v2` is the live instance: served at `/reports/sbti_v2` (app/main.py), not in
BUILDERS. It does gate itself — it calls `scope_declaration_completeness` twice — but by
hand, which is the arrangement above and not something this file verifies. Putting it in
BUILDERS would fix that and is a product change (the registry is also what `/export`
enumerates), so it is named here rather than quietly assumed covered.

APPLICABILITY IS MEASURED, NOT NAMED. A renderer is in scope for this gate if some number
it publishes MOVES when the scope split changes AND NOTHING ELSE DOES. Keeping a list of
which renderers publish a scope figure would be the same staleness this file is built to
prevent, one level down; matching their KEYS is no better, because every framework has
its own word for Scope 1 and two versions of this rule missed a renderer over exactly
that.

That makes the control the whole argument, and the first control was confounded. Varying
the category NAME changes the scope split, but the name is also read by mechanisms that
never look at scope, so `esos` was selected over nine energy-carrier figures while
publishing no scope figure at all. The control here instead holds every category string
fixed and declares the scope on the activity; a third run cancels ids. See
`_scope_driven_paths`.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  (registers tables on Base.metadata)
from app.reports.export import BUILDERS, build_report
from tests.scope3_util import (
    ASSUMED_CATEGORIES, assumed_scope_run, declared_scope_run, explicit_scope_run,
)


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


# A superset of what any builder reads. Supplying every parameter means a renderer that
# refuses does so because of the scope split, not because this file forgot an argument —
# a refusal for the wrong reason would make the gate look like it was working.
_PARAMS = {
    "intensity_denominator": 2.0,
    "intensity_denominator_unit": "revenue_musd",
    "intensity_denominator_period_days": 365,
    "net_revenue_millions": 1.0,
    "revenue_currency": "EUR",
    "assurance_level": "limited",
    "current_year": 2025,
    "as_of": "2025-12-31",
    "credits_as_of": "2025-12-31",
}

# Builders that cannot be reached from a run alone: they report on an object this gate
# does not construct, and raise before producing a payload. Each is listed with the
# argument it wants, and `test_every_builder_is_covered_or_named` fails if a builder
# starts or stops raising — so this set cannot quietly absorb a renderer that broke.
_NEEDS_ANOTHER_OBJECT = {
    "lca": "assessment_id",     # a product assessment, not an organisational run
    "sbti": "target_id",        # a specific emissions target
}

# WHAT THIS GATE DOES NOT ASSERT, stated here so nobody reads more into a pass.
#
# It asserts the inference is DISCLOSED — a blocker naming it exists in the payload. It
# does NOT assert that the blocker sits in the particular list the renderer's readiness
# flag is computed from, so a disclosure filed into a nested, non-gating `*_blockers`
# list would satisfy it.
#
# The obvious fix is to read the readiness flag too, and it was tried and dropped: a
# flag can only be read by NAME, and a hand-kept list of names is the staleness this
# file exists to refuse. Measured — the names a reader would reach for
# ("disclosure_ready", "filing_ready", "ready", ...) miss the verdict key of 5 of the 11
# renderers this gate selects: `submission_ready` (cdp), `assessment_ready` (ecovadis),
# `report_ready` (ets_mrv), `climate_transition_plan_carbon_inputs_ready` (csddd),
# `metrics_and_targets_b_quantitative_ready` (tcfd).
#
# Finding the flag by BEHAVIOUR instead — the boolean that flips between the guessed run
# and the control — covers only 7 of the 11: cdp, esrs_e1, ets_mrv and secr refuse BOTH
# runs for unrelated reasons, so no boolean of theirs moves and there is nothing to
# observe. An assertion that silently covers 7 of 11 while reading as though it covers
# all of them is this project's own dominant defect, so it is absent rather than
# half-present.


def _numbers(payload) -> dict:
    """Every NUMBER the payload publishes, by path. No name matching whatsoever.

    A number, not a mention: a key called `scope2_residual_mix_blockers` holding a list
    is commentary about Scope 2, not a figure.
    """
    out, stack = {}, [("", payload, 0)]
    while stack:
        path, obj, depth = stack.pop()
        if depth > 8:
            continue
        if isinstance(obj, dict):
            items = obj.items()
        elif isinstance(obj, (list, tuple)):
            items = ((str(i), v) for i, v in enumerate(obj))
        else:
            continue
        for k, v in items:
            p = f"{path}.{k}" if path else str(k)
            if isinstance(v, (dict, list, tuple)):
                stack.append((p, v, depth + 1))
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                out[p] = v
    return out


def _scope_driven_paths(assumed, assumed_twin, control) -> list:
    """The paths whose number is produced by the run's own scope split.

    NOT MATCHED ON NAMES, and two earlier versions of this rule failed because they
    were. Selecting keys containing "scope1" skipped GRI, which publishes at
    `gri_305_1_scope1.gross_tco2e` — the scope named by the parent, the leaf just
    "gross". Widening to the whole path then fixed GRI and still skipped ETS MRV, which
    calls the same figure `direct_emissions_tco2e`, because that is what the MRR calls
    it. Every framework has its own word for Scope 1, so no list of words can be
    complete; both misses were found by unwiring the gate renderer by renderer and
    watching this file stay green.

    So the figures are identified by what MOVES. That makes the CONTROL the whole
    argument, and the obvious control is wrong.

    `declared_scope_run` differs from `assumed_scope_run` in the category STRING, and the
    string is read by at least two independent mechanisms: `SCOPE_RULES` in
    services/calc.py, and the `_ENERGY_CARRIERS` allowlist in `reports/secr._energy_kwh`,
    which never reads scope at all. Across that pair `esos` moved 9 numbers —
    `total_energy_kwh`, `by_carrier_kwh.*`, `share_of_reported_carriers_pct.*` — and
    `esos` publishes NO scope figure anywhere in its payload. It was selected by the
    renaming, not by the split, and the earlier docstring here asserted the opposite
    ("a number that differs between them is therefore produced by the scope split").

    `explicit_scope_run` is the control that actually isolates it: the same three
    category strings, byte-identical, with the correct scope DECLARED on each activity so
    the lines freeze `scope_source == "explicit"` instead of `"assumed_scope3"`. No
    category-keyed mechanism anywhere can move across that pair — only the split.
    Measured: `esos` drops from 9 moving numbers to 3 (run.id, run.organisation_id,
    run.reporting_period_id — identity), while `scope3_inventory` (21) and `ets_mrv` (4)
    move exactly as many as before. 11 of 25 reachable builders applicable, 14 not.

    `assumed_twin` is a SECOND assumed run, built identically to the first, and it is
    what keeps the remaining inference honest. Ids and row counts differ between any two
    runs, and without the twin they read as scope-driven: `removals` publishes exactly
    one such number and would be flagged over it. Whatever moves between the two twins is
    identity, not scope, so it is subtracted.

    Among the 14 not selected are `pcaf` and `sfdr_pai`, whose scope figures are FINANCED
    emissions — an investee's Scope 1 and 2, attributed from FinancedPosition rows. The
    reporting organisation's own split has nothing to do with them, and the first,
    name-matched version of this rule flagged both.
    """
    a, t, c = _numbers(assumed), _numbers(assumed_twin), _numbers(control)
    identity = {p for p in set(a) | set(t) if a.get(p) != t.get(p)}
    return sorted({p for p in set(a) | set(c) if a.get(p) != c.get(p)} - identity)


def _all_blockers(payload) -> list:
    """Every blocker the payload carries, at any depth and under any name.

    NOT `payload["blockers"]`. Reading only the top level produced two false positives
    the first time this gate ran: `csddd` nests its inventory's blockers at
    `article_22_carbon_inputs.ghg_inventory.blockers`, and `tcfd` renames them to
    `metrics_and_targets_b_quantitative_blockers`. Both inherit the scope blocker from
    ISSB S2 and were refusing correctly; the gate simply was not looking where they put
    it, and would have had two already-correct renderers wired a second time.
    """
    out = []
    stack = [(payload, 0)]
    while stack:
        obj, depth = stack.pop()
        if depth > 8:
            continue
        if isinstance(obj, dict):
            for k, v in obj.items():
                if "blocker" in str(k).lower() and isinstance(v, (list, tuple)):
                    out.extend(v)
                elif isinstance(v, (dict, list, tuple)):
                    stack.append((v, depth + 1))
        elif isinstance(obj, (list, tuple)):
            for v in obj:
                if isinstance(v, (dict, list, tuple)):
                    stack.append((v, depth + 1))
    return out


def _names_the_inference(blockers) -> bool:
    """Does a blocker name the activities whose scope was guessed?

    ASSERTS THE BLOCKER IS PRESENT, NOT THAT THE RENDERER IS UNREADY, and the difference
    is what makes this gate work. The first version of this file checked
    `ready is True and not blockers` — and it was masked. `secr` and `esrs_e1` also
    refuse the fixture run over a missing `intensity_denominator_period_days`, so
    removing their scope wiring entirely left them unready anyway and the gate stayed
    green. `ets_mrv` is the extreme case: `verified` defaults to False, so its blocker
    list is NEVER empty and that implication was vacuous there for all time.

    Measured by unwiring each disclosure one at a time and re-running this file: the
    `ready`-gated version caught 2 of 5; this version catches 9 of 9 — every
    `scope_declaration_completeness` call site of the shape
    `blockers.extend(...)` (git grep it).

    Matched on the fixture's own category names rather than on the blocker's wording, so
    rephrasing the sentence does not silently disarm the test.
    """
    return any(cat in str(b) for b in blockers for cat in ASSUMED_CATEGORIES)


def _reachable_builders():
    return sorted(set(BUILDERS) - set(_NEEDS_ANOTHER_OBJECT))


class _Runs:
    """The four runs this gate is built on, and the renderers they select."""

    def __init__(self, db, assumed, twin, explicit, declared):
        self._db, self._a, self._t = db, assumed, twin
        self._e, self._d = explicit, declared

    def _build(self, pair, key):
        org, run = pair
        return build_report(self._db, org.id, key,
                            {**_PARAMS, "run_id": run.id, "current_run_id": run.id})

    def assumed(self, key):
        return self._build(self._a, key)

    def applicable(self):
        """(key, guessed payload, explicit-scope payload, renamed payload) per renderer
        whose figures the scope split drives.

        Applicability is decided against the EXPLICIT-SCOPE control only — it is the one
        that holds the category string fixed. The renamed run comes along because the
        refusal control below must hold on both kinds of clean run, not because it
        selects anything.
        """
        for key in _reachable_builders():
            pa, pt, pe = (self._build(self._a, key), self._build(self._t, key),
                          self._build(self._e, key))
            if _scope_driven_paths(pa, pt, pe):
                yield key, pa, pe, self._build(self._d, key)

    def identity_paths(self):
        """What the twin cancels, per renderer — the subtraction's own audit trail."""
        return {key: sorted({p for p in set(a) | set(t) if a.get(p) != t.get(p)})
                for key in _reachable_builders()
                for a, t in [(_numbers(self._build(self._a, key)),
                              _numbers(self._build(self._t, key)))]}


@pytest.fixture
def runs(db):
    """The guessed run, a twin of it, and the two kinds of clean control.

    `explicit_scope_run` keeps the category strings and declares the scope; it is what
    decides applicability. `declared_scope_run` renames the categories so SCOPE_RULES
    recognises them; it is the realistic production path, and a correct renderer must be
    silent on it too.
    """
    return _Runs(db,
                 assumed_scope_run(db, name="Assumed"),
                 assumed_scope_run(db, name="AssumedTwin"),
                 explicit_scope_run(db, name="ExplicitScopes"),
                 declared_scope_run(db, name="Declared"))


# --- the gate ----------------------------------------------------------------------------

def test_no_renderer_certifies_a_scope_figure_the_engine_inferred(runs):
    """The property, over every renderer the registry dispatches on.

    `assumed_scope_run` builds three real sources — purchased steam, a refrigerant
    top-up, generator fuel — whose category names miss SCOPE_RULES. Every line freezes
    `scope_source == 'assumed_scope3'`, so Scope 1 and Scope 2 are both 0.0 BY GUESS
    while the whole inventory sits in Scope 3.

    A renderer whose figures come off that split is publishing a guess. It must disclose
    that — a blocker naming the activities whose scope was guessed. See the note above
    `_numbers` for what this does NOT assert.

    WHAT IS ASSERTED IS THE DISCLOSURE, NOT THE CALL: a renderer that names the
    inference in its own framework's vocabulary passes without calling
    `scope_declaration_completeness`. In practice all 11 selected renderers do call it,
    so that latitude currently costs nothing — it is here so the gate reports a defect
    rather than a missing function call.
    """
    silent = [key for key, pa, _pe, _pd in runs.applicable()
              if not _names_the_inference(_all_blockers(pa))]

    assert not silent, (
        "these renderers published a figure derived from an INFERRED scope split and "
        f"said nothing about it: {silent}. Call "
        "app/services/calc.scope_declaration_completeness and extend the blockers with "
        "it, as the renderers that pass this already do — or stop publishing the figure.")


def test_the_gate_is_not_refusing_everything(runs):
    """The negative control, and it is the half that makes the test above mean anything.

    If renderers raised the scope blocker on a CLEAN run too, the gate above would pass
    while proving nothing — the exact failure this phase spent itself finding.

    Checked against BOTH kinds of clean run, because they discharge the inference by
    different means and a correct renderer must be silent on each: `explicit_scope_run`
    declares the scope on the activity (`scope_source == "explicit"`), and
    `declared_scope_run` renames the category so SCOPE_RULES derives it
    (`scope_source == "category_rule"`).
    """
    refused_guess, accepted_explicit, accepted_renamed = set(), set(), set()
    for key, pa, pe, pd in runs.applicable():
        if _names_the_inference(_all_blockers(pa)):
            refused_guess.add(key)
        if not _names_the_inference(_all_blockers(pe)):
            accepted_explicit.add(key)
        if not _names_the_inference(_all_blockers(pd)):
            accepted_renamed.add(key)

    assert refused_guess, (
        "no renderer refused the inferred-scope run — the gate above would pass over a "
        "product where nothing checks the scope split at all")
    assert refused_guess & accepted_explicit, (
        f"every renderer that refuses a guessed split ({sorted(refused_guess)}) also "
        f"refuses the EXPLICIT-SCOPE control, where the preparer declared the scope "
        f"outright. A gate that refuses both is refusing everything.")
    assert refused_guess & accepted_renamed, (
        f"every renderer that refuses a guessed split ({sorted(refused_guess)}) also "
        f"refuses the RENAMED control, where SCOPE_RULES derives the scope — the "
        f"realistic production path. The blocker is firing on something other than the "
        f"inference.")


def test_every_builder_is_covered_or_named(runs):
    """No renderer may be silently outside this gate.

    The two sets below must account for every key in BUILDERS. A framework added to the
    registry lands in neither and fails here by name, which is the whole reason this gate
    reads the registry instead of a list of its own.
    """
    built, raised = set(), {}
    for key in sorted(BUILDERS):
        try:
            runs.assumed(key)
            built.add(key)
        except Exception as exc:                      # noqa: BLE001 - the point is to see it
            raised[key] = f"{type(exc).__name__}: {exc}"

    unaccounted = sorted(set(BUILDERS) - built - set(_NEEDS_ANOTHER_OBJECT))
    assert not unaccounted, (
        f"these builders are neither exercised by this gate nor declared as needing "
        f"another object: {unaccounted}")

    # A builder listed as needing another object must actually raise for that reason. If
    # one stops raising it is now reachable and belongs in the gate; if it raises for a
    # DIFFERENT reason it is broken, and absorbing that into an exclusion list is how a
    # renderer disappears from its own test.
    for key, want in _NEEDS_ANOTHER_OBJECT.items():
        assert key in raised, (
            f"{key} no longer raises — it is reachable from a run now, so remove it from "
            f"_NEEDS_ANOTHER_OBJECT and let the gate cover it")
        assert want in raised[key], (
            f"{key} raises for an unexpected reason: {raised[key]!r} (expected it to ask "
            f"for {want!r})")

    # And nothing else may raise. A renderer that throws is not covered by the gate above,
    # because a payload that never arrives cannot fail an assertion about its contents.
    unexpected = {k: v for k, v in raised.items() if k not in _NEEDS_ANOTHER_OBJECT}
    assert not unexpected, f"builders raised unexpectedly: {unexpected}"


def test_the_applicability_rule_selects_a_real_set(runs):
    """A guard on the guard: `_scope_driven_paths` must select some renderers and
    not all of them.

    If it matched nothing, the gate would iterate over an empty set and pass — the
    empty-collection failure this phase found twice. If it matched everything, it would
    be asserting about renderers that have no scope figure to get wrong.
    """
    keys = _reachable_builders()
    applicable = [key for key, *_ in runs.applicable()]

    assert applicable, "the applicability rule matched no renderer at all"
    assert len(applicable) < len(keys), (
        f"the applicability rule matched every one of the {len(keys)} renderers, "
        "including ones that publish no scope figure — it is not discriminating and the "
        "gate's scope is wrong")


def test_the_control_is_not_confounded_by_the_category_rename(db):
    """The guard that stops the confounded control coming back.

    Two clean controls are available and they are NOT interchangeable.
    `declared_scope_run` renames the categories, and the category string is read by
    mechanisms that never look at scope — `_ENERGY_CARRIERS` in
    `reports/secr._energy_kwh` being the one that bit. `explicit_scope_run` keeps the
    strings and declares the scope, so only the split moves.

    So the renamed control selects renderers the split does not drive, and the explicit
    one must select a STRICT SUBSET. `esos` is the measured instance: 9 numbers move
    across the rename, 0 across the split, and its payload contains no scope figure at
    all. If this test starts failing because the two sets agree, do not relax it —
    re-derive which mechanism moved, because the applicability rule's whole claim is
    that a movement is attributable to the scope split.
    """
    a = assumed_scope_run(db, name="ConfA")
    twin = assumed_scope_run(db, name="ConfTwin")
    expl = explicit_scope_run(db, name="ConfExpl")
    renamed = declared_scope_run(db, name="ConfRenamed")

    def build(pair, key):
        org, run = pair
        return build_report(db, org.id, key,
                            {**_PARAMS, "run_id": run.id, "current_run_id": run.id})

    by_split, by_rename = set(), set()
    for key in _reachable_builders():
        pa, pt = build(a, key), build(twin, key)
        if _scope_driven_paths(pa, pt, build(expl, key)):
            by_split.add(key)
        if _scope_driven_paths(pa, pt, build(renamed, key)):
            by_rename.add(key)

    assert by_split, "the explicit-scope control selected no renderer at all"
    assert by_split <= by_rename, (
        f"the explicit-scope control selected renderers the rename does not: "
        f"{sorted(by_split - by_rename)}. The rename changes the split as well, so it "
        f"should select a superset — this means the two controls disagree about the "
        f"split itself and one of the fixtures is wrong.")
    assert by_rename - by_split, (
        "the renamed control no longer over-selects anything, so this file can no "
        "longer demonstrate WHY applicability is decided against the explicit-scope "
        "control. Re-derive it before simplifying the fixtures together.")


def test_the_identity_control_actually_cancels_something(runs):
    """A guard on the subtraction, because a vacuous one would read exactly like a
    working one.

    `_scope_driven_paths` subtracts whatever moves between two identically-built assumed
    runs. If nothing ever moved there, the subtraction would be a no-op that still looked
    principled, and the first id or timestamp a renderer started publishing would be read
    as a scope-driven figure. Something must actually be cancelled — `removals` publishes
    exactly one such number, and without the twin it is flagged over it.
    """
    cancelled = {k: v for k, v in runs.identity_paths().items() if v}
    assert cancelled, (
        "no renderer published a number that differs between two identically-built "
        "runs, so the identity subtraction cancels nothing and is untested here")
