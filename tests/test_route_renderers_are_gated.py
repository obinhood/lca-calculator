"""The Phase 2 exit gate, extended to renderers served OUTSIDE `export.BUILDERS`.

WHAT THIS FILE CLOSES. `tests/test_ready_implies_derivable.py` asserts its property over
`BUILDERS` — the registry `/export` and `/compliance` dispatch on — so a framework added
to that registry is covered the moment it is added. Its companion
`test_every_builder_is_covered_or_named` is meant to stop a renderer falling silently
outside the gate, but it iterates `BUILDERS` too: a renderer absent from the registry is
absent from its view, and it cannot name what it cannot see.

`sbti_v2` was the live instance — served at `/reports/sbti_v2`, implemented in
`app/reports/sbti_v2.py`, never in `BUILDERS`.

TWO ENUMERATIONS, NEITHER OF WHICH KEEPS A LIST OF FRAMEWORK NAMES. A list of names is
the staleness the gate file exists to refuse, so coverage is derived twice, from two
independent facts about the source tree, and a renderer must be accounted for in both:

    ROUTE  — every GET route under `/reports/`, read off the FastAPI app. Measured: 34
             routes, and the first path segment of 27 of them is a `BUILDERS` key (every
             registry key has a route). The seven outside are `sbti_v2` plus six analyses
             that are not framework disclosures at all.
    MODULE — every module under `app/reports/` whose source calls
             `scope_declaration_completeness`, against the modules reachable from a
             builder. Measured: 11 modules gate the split, 19 are reachable, and the
             difference is exactly `{sbti_v2}`.

The axes fail differently. A renderer added as a route with no registry entry fails the
route axis; one reached by some other path — a sub-app, a router, a service call — still
fails the module axis the moment it gates the split by hand. Both named `sbti_v2` before
this file existed.

WHY `sbti_v2` WAS NOT SIMPLY ADDED TO `BUILDERS`, which would have brought it under the
existing gate with a one-line registry entry. The registry is also what `/export`
enumerates, and `sbti_v2` publishes NO whole-document readiness key — none of the seven
`to_pdf` reads (`disclosure_ready`, `filing_ready`, `report_ready`, `declaration_ready`,
`assessment_ready`, `submission_ready`, `ok`) and no top-level `blockers`. Executed
against the guessed-scope payload, `to_pdf` selects the neutral grey INFORMATION REPORT
banner, renders no outstanding-items section, no assumed-scope caveat and no figure
table — over a report whose significance test was REFUSED because the split was guessed.
The refusal survives in CSV, which flattens everything, and vanishes from the PDF, which
is the artefact a human files. That is the defect class this phase exists to close, and
`to_pdf`'s own comment records it having already happened to four frameworks. A registry
entry would have created a fifth while appearing to close a coverage gap.

So the renderer is gated HERE instead, under the same measurement rather than a second
partial copy of it: `_all_blockers`, `_names_the_inference` and `_scope_driven_paths` are
imported from the gate file, not reimplemented. Being named in `_GATED_HERE` is therefore
not an excuse — it is a requirement to pass the property below, and the adapter is what
pays for it.

WHAT THIS FILE DOES NOT ASSERT, stated so nobody reads more into a pass. Both axes find
renderers that KNOW their figures rest on the scope split — one by being served as a
framework report, the other by calling the gate function. Neither can find a renderer
that SHOULD gate the split and never has: it is in no registry, it calls nothing, and
there is no artefact to enumerate. `/reports/hourly_scope2` publishes hourly
market-based Scope 2 figures and does not call `scope_declaration_completeness`; whether
it should is a question about that renderer, not one this file answers.
"""
import inspect
import pathlib
import re

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
import app.models  # noqa: F401  (registers tables on Base.metadata)
from app.reports.export import BUILDERS
from tests.scope3_util import (
    assumed_scope_run, declared_scope_run, declared_scope_run_with_scope3,
    explicit_scope_run,
)
# The SAME measurement as the registry gate, imported rather than copied. A second
# implementation of "which figures move with the split" would drift from the first, and
# this project has already paid for one rule re-derived partially in a second caller.
from tests.test_ready_implies_derivable import (
    _all_blockers, _names_the_inference, _scope_driven_paths,
)

_REPORTS_PREFIX = "/reports/"
_REPORTS_DIR = pathlib.Path(__file__).resolve().parent.parent / "app" / "reports"


# --- what is accounted for, and how -------------------------------------------------------

def _b_sbti_v2(db, run):
    """`sbti_v2` reached the way its route reaches it: from the run alone.

    Worth stating because it is what made a registry entry look free. `_b_sbti` needs a
    `target_id` and the gate file carries it in `_NEEDS_ANOTHER_OBJECT` for that reason;
    `sbti_v2_report` takes the run and nothing else, so it IS reachable from a run and
    the gate's property applies to it in full. The reason it is gated here rather than
    there is the export surface, not reachability — see the module docstring.
    """
    from app.reports.sbti_v2 import sbti_v2_report
    return sbti_v2_report(db, run)


# Renderers served outside the registry that this file gates itself. A key here is a
# PROMISE that the property below holds over it, not a hole in the gate: adding a name
# without an adapter that passes is not possible.
_GATED_HERE = {
    "sbti_v2": _b_sbti_v2,
}

# Routes under `/reports/` that are not framework disclosure payloads. Each reason is a
# fact about what the route returns, checkable by reading its handler — not a note that
# it was inconvenient.
#
# The module axis does NOT independently vouch for these six: it scans `app/reports/`,
# and every one of them is served from `app/services/` (screening, series_screen,
# hourly_scope2, uncertainty) or from the summary renderer. What it does guarantee is
# narrower and still worth having — a module under `app/reports/` that gates the split by
# hand can never be excused here, because it fails the module axis whatever this dict
# says. So the dict can hold a wrong reason; it cannot hide a hand-gated renderer.
_NOT_A_FRAMEWORK_DISCLOSURE = {
    "summary.txt": "a plain-text run summary served as text/plain, not a disclosure "
                   "payload in any framework's shape",
    "screening": "the pre-calculation screening state frozen onto a run — an assurance "
                 "exception register, not a framework's required disclosures",
    "series_screen": "year-on-year variance screening of one period against a baseline "
                     "(GHG Protocol ch.7), an analysis over two periods rather than a "
                     "report on one run",
    "series_screen_history": "the same screening across an ordered series of periods, "
                             "to separate a persistent level shift from a spike",
    "hourly_scope2": "hourly CFE temporal matching for one reporting period — a parallel "
                     "method whose own docstring records that the run's annual location "
                     "and market figures are untouched by it",
    "uncertainty": "Monte Carlo uncertainty propagation over a run's figures, reporting "
                   "a confidence interval rather than a framework's disclosures",
}


def _report_route_keys() -> set:
    """The first path segment of every GET route under `/reports/`.

    The segment, not the whole path, because four product reports are served with the
    object in the path (`/reports/lca/{assessment_id}` and its three siblings) and those
    are registry entries like any other.
    """
    from app.main import app
    keys = set()
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path.startswith(_REPORTS_PREFIX):
            continue
        if "GET" not in (getattr(route, "methods", None) or set()):
            continue
        keys.add(path[len(_REPORTS_PREFIX):].split("/")[0])
    return keys


def _modules_that_gate_the_split() -> set:
    """Modules under `app/reports/` whose source calls `scope_declaration_completeness`.

    Read off the source tree rather than imported, so a module that gates the split is
    found whether or not anything currently reaches it — which is the whole failure being
    closed.
    """
    return {p.stem for p in _REPORTS_DIR.glob("*.py")
            if "scope_declaration_completeness" in p.read_text(encoding="utf-8")}


def _modules_reachable_from_the_registry() -> set:
    """Modules a builder imports, read out of the builders' own source.

    Every builder defers its import into the function body, so the import is in the
    source and not in the module namespace. Reading it is not elegant, but the
    alternative — a hand-kept map from framework key to module — is the staleness this
    whole arrangement refuses.
    """
    out = set()
    for fn in BUILDERS.values():
        out |= set(re.findall(r"from \.(\w+) import", inspect.getsource(fn)))
    return out


# --- fixtures ------------------------------------------------------------------------------

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


@pytest.fixture
def runs(db):
    """The guessed run, its twin, and THREE clean controls.

    The third one is not redundant and this file does not work without it. See
    `test_the_outside_gate_is_not_refusing_everything`.
    """
    return {
        "assumed": assumed_scope_run(db, name="OutsideAssumed")[1],
        "twin": assumed_scope_run(db, name="OutsideTwin")[1],
        "explicit": explicit_scope_run(db, name="OutsideExplicit")[1],
        "declared": declared_scope_run(db, name="OutsideDeclared")[1],
        "with_scope3": declared_scope_run_with_scope3(db, name="OutsideWithScope3")[1],
    }


# --- the coverage property, on both axes ---------------------------------------------------

def test_every_report_route_is_in_the_registry_or_accounted_for():
    """A renderer served as a route may not be outside the gate unaccounted for.

    This is the assertion `test_every_builder_is_covered_or_named` structurally cannot
    make: it iterates `BUILDERS`, and the failure mode is a renderer that is not in
    `BUILDERS`. Here the enumeration is the route table, so a renderer reachable over
    HTTP is in view whether or not the registry knows about it.

    Fails on the commit before this file with `['sbti_v2']`.
    """
    unaccounted = sorted(_report_route_keys() - set(BUILDERS)
                         - set(_GATED_HERE) - set(_NOT_A_FRAMEWORK_DISCLOSURE))
    assert not unaccounted, (
        f"these report routes are served outside export.BUILDERS and outside this "
        f"file's two buckets, so nothing asserts the Phase 2 property over them: "
        f"{unaccounted}. Either add the renderer to BUILDERS (which also puts it on "
        f"/export and /compliance — check it carries a whole-document readiness key "
        f"first, or the PDF files a neutral banner over a refusal), or add it to "
        f"_GATED_HERE with an adapter, or to _NOT_A_FRAMEWORK_DISCLOSURE with a reason "
        f"that is true.")


def test_no_module_that_gates_the_scope_split_is_outside_both_registries():
    """The second axis, which does not depend on how a renderer is served.

    A module that calls `scope_declaration_completeness` has decided its figures rest on
    the scope split. If nothing reaches it from the registry, the registry gate does not
    cover it, and the hand-wiring the gate file exists to replace is what is holding it
    up. Independent of the route axis: this one fails even for a renderer served from a
    sub-app, a router, or another service.

    Fails on the commit before this file with `['sbti_v2']`.
    """
    stranded = sorted(_modules_that_gate_the_split()
                      - _modules_reachable_from_the_registry() - set(_GATED_HERE))
    assert not stranded, (
        f"these app/reports modules gate the scope split by hand but no builder reaches "
        f"them, so the registry gate cannot see them: {stranded}. A renderer that knows "
        f"its figures depend on the split and is verified only by tests written for it "
        f"by name is the arrangement tests/test_ready_implies_derivable.py replaced.")


def test_an_account_may_not_outlive_the_thing_it_accounts_for():
    """Covered-or-named rots in BOTH directions, so the names are checked too.

    A reason for a route that no longer exists reads as diligence and asserts nothing,
    and an adapter for a deleted renderer would make the property below vacuous while
    still passing. `_NEEDS_ANOTHER_OBJECT` in the gate file is guarded the same way — a
    builder listed as needing another object must actually still raise.
    """
    live = _report_route_keys()
    stale_reasons = sorted(set(_NOT_A_FRAMEWORK_DISCLOSURE) - live)
    assert not stale_reasons, (
        f"_NOT_A_FRAMEWORK_DISCLOSURE excuses routes that no longer exist: "
        f"{stale_reasons}. Delete the entries.")

    stale_adapters = sorted(set(_GATED_HERE) - live - _modules_that_gate_the_split())
    assert not stale_adapters, (
        f"_GATED_HERE carries adapters for renderers that are neither served nor gating "
        f"the split any more: {stale_adapters}. Delete them, or the property test below "
        f"passes over nothing.")

    overlap = sorted(set(_GATED_HERE) & set(_NOT_A_FRAMEWORK_DISCLOSURE))
    assert not overlap, (
        f"{overlap} is both gated and excused — one of the two is a false statement")

    assert all(_NOT_A_FRAMEWORK_DISCLOSURE[k].strip() for k in _NOT_A_FRAMEWORK_DISCLOSURE), (
        "every excused route needs a stated reason")


# --- the Phase 2 property itself, over the renderers gated here ----------------------------

def test_renderers_outside_the_registry_disclose_an_inferred_scope_split(db, runs):
    """The gate file's property, applied to the renderers the registry does not reach.

    Same fixture, same measurement, same assertion: a renderer whose published figures
    move when the scope split changes must disclose that the split was inferred, and a
    blocker naming the guessed activities is what counts as disclosing it.

    Measured for `sbti_v2`: 8 numbers move across the explicit-scope control, among them
    `significance.denominator_tco2e` — the denominator of the 5% significance test, which
    on the guessed run is None rather than a figure derived from emissions that are
    provably not in Scope 3.

    FOUND BY ITS OWN NEGATIVE CONTROL: emptying `_GATED_HERE` made this test pass over
    nothing, and the first mutation run read the vacuous pass as evidence the gate was
    weak. The coverage axes above do go red on an empty bucket — an unaccounted renderer
    is exactly what they are for — but a test whose subject can vanish while it still
    reports success is this product's dominant defect pointed at its own suite, so the
    non-vacuity is asserted here rather than inferred from another test failing.
    """
    assert _GATED_HERE, (
        "_GATED_HERE is empty, so this test asserted nothing. If the last renderer "
        "outside the registry was brought into BUILDERS, delete this file; do not leave "
        "an empty gate reporting a pass.")

    silent = []
    for key, build in sorted(_GATED_HERE.items()):
        payload = build(db, runs["assumed"])
        if not _names_the_inference(_all_blockers(payload)):
            silent.append(key)

    assert not silent, (
        f"these renderers published a figure derived from an INFERRED scope split and "
        f"said nothing about it: {silent}. Call "
        f"app/services/calc.scope_declaration_completeness and disclose it in the "
        f"payload, as the renderers the registry gate covers already do — or stop "
        f"publishing the figure.")


def test_the_outside_gate_is_not_refusing_everything(db, runs):
    """The negative control, and the control this renderer needs is NOT the obvious one.

    `explicit_scope_run` and `declared_scope_run` both discharge the inference, and a
    correct renderer must be silent on each — but neither of them carries any Scope 3 at
    all, so `significance()` answers `determinable: False` on both for its own perfectly
    legitimate reason ("categories 1-14 total zero"). A significance gate tested only
    against those would read as working while refusing every run it is ever shown, which
    is the shape this phase spent itself finding. `tests/scope3_util` records the same
    warning above `declared_scope_run_with_scope3`.

    So the third control carries real Scope 3 emissions beside a rule-derived Scope 1/2
    split. Measured: `denominator_tco2e` is 10.0 and `determinable` is True there, so the
    renderer is shown to still ANSWER rather than merely to stop blocking.
    """
    refused_guess, accepted = set(), {c: set() for c in
                                     ("explicit", "declared", "with_scope3")}
    for key, build in sorted(_GATED_HERE.items()):
        if _names_the_inference(_all_blockers(build(db, runs["assumed"]))):
            refused_guess.add(key)
        for control in accepted:
            if not _names_the_inference(_all_blockers(build(db, runs[control]))):
                accepted[control].add(key)

    assert refused_guess, (
        "no renderer gated here refused the inferred-scope run, so the property test "
        "above would pass over a renderer that checks nothing")
    for control, ok in sorted(accepted.items()):
        assert refused_guess & ok, (
            f"every renderer that refuses a guessed split ({sorted(refused_guess)}) also "
            f"refuses the {control!r} control, where the split is known. A gate that "
            f"refuses both is refusing everything and proves nothing.")

    # Silence is not the same fact as an answer. On the control that carries real Scope 3
    # the renderer must still PRODUCE the figure the guessed run withheld — otherwise a
    # renderer that had quietly stopped computing significance altogether would satisfy
    # every assertion above.
    answered = [key for key, build in sorted(_GATED_HERE.items())
                if _scope_driven_paths(build(db, runs["assumed"]),
                                       build(db, runs["twin"]),
                                       build(db, runs["with_scope3"]))]
    assert answered == sorted(_GATED_HERE), (
        f"on the control carrying real Scope 3 emissions these renderers published no "
        f"figure that differs from the guessed run: "
        f"{sorted(set(_GATED_HERE) - set(answered))}. They stopped refusing without "
        f"starting to answer.")


def test_the_applicability_rule_selects_every_renderer_gated_here(db, runs):
    """A guard on the guard: the property above must not be passing over nothing.

    `_names_the_inference` returns False for a payload with no blockers at all, so a
    renderer whose figures the split does NOT drive would sail through the property test
    and the gate would report coverage it never exercised. That is this product's
    dominant defect aimed at its own test suite, so applicability is asserted rather than
    assumed: every adapter here must be selected by the gate file's own measured rule.
    """
    moved = {}
    for key, build in sorted(_GATED_HERE.items()):
        moved[key] = _scope_driven_paths(build(db, runs["assumed"]),
                                         build(db, runs["twin"]),
                                         build(db, runs["explicit"]))

    inapplicable = sorted(k for k, paths in moved.items() if not paths)
    assert not inapplicable, (
        f"the scope split drives no figure these renderers publish: {inapplicable}. The "
        f"property test above therefore asserts nothing about them — either they belong "
        f"in _NOT_A_FRAMEWORK_DISCLOSURE, or the figure they used to publish is gone.")
