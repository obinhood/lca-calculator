"""The landing page may not state a figure the codebase disagrees with.

The marketing page carries two numbers about the engine — how many endpoints it exposes
and how many tests guard it. Both were typed by hand, and hand-typed inventory figures in
this repo have a track record: the README's endpoint and test counts have now gone stale
three separate times, and each time it was caught by someone happening to look rather than
by anything failing.

A landing page is a worse place for that than a README. It is the first thing a stranger
reads, its whole argument is that this engine does not assert what it cannot support, and
a visibly wrong number on it refutes the argument at no cost to the reader.

The framework count needs no test: the page derives it from the registry it renders, which
is the better fix wherever it is available. These two cannot be derived in the browser, so
they are pinned here instead.
"""
import pathlib
import re

import pytest

LANDING = pathlib.Path("frontend/src/components/Landing.tsx")
MAIN = pathlib.Path("app/main.py")

# `{ v: "122", l: "API endpoints", ... }` — the figure and the thing it counts.
_FACT = re.compile(r'\{\s*v:\s*"([^"]+)",\s*l:\s*"([^"]+)"')


def _source_without_comments() -> str:
    """The page's source with comments stripped.

    Scanning the raw file for banned phrases matched the comment that explains why the
    page has no testimonials — a rule tripping over its own rationale. Only what a reader
    can actually see should be judged.
    """
    src = LANDING.read_text()
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)      # block + JSX comments
    src = re.sub(r"^\s*//.*$", " ", src, flags=re.M)        # line comments
    return src


def _stated() -> dict:
    """What the landing page currently claims, keyed by the label under the number."""
    return {label: value for value, label in _FACT.findall(_source_without_comments())}


def _as_int(s: str) -> int:
    """Digits only — the page writes figures for humans ("1,470+"), not for parsers."""
    digits = re.sub(r"[^0-9]", "", s)
    if not digits:
        raise AssertionError(f"{s!r} is not a figure this guard can check")
    return int(digits)


def test_the_landing_page_states_the_real_endpoint_count():
    stated = _stated()
    assert "API endpoints" in stated, (
        "the rigour section no longer carries an endpoint figure — if it was removed on "
        "purpose, delete this test; if it was renamed, update the label here")
    actual = len(re.findall(r"^@app\.(?:get|post|put|delete|patch)\(", MAIN.read_text(),
                            re.MULTILINE))
    assert _as_int(stated["API endpoints"]) == actual, (
        f"the landing page says {stated['API endpoints']} API endpoints; app/main.py "
        f"defines {actual}. Update frontend/src/components/Landing.tsx.")


def test_the_landing_page_states_the_real_test_count(request):
    """Counted from this very session's collection, so it can never be a stale snapshot."""
    stated = _stated()
    assert "automated tests" in stated, (
        "the rigour section no longer carries a test-count figure — if it was removed on "
        "purpose, delete this test; if it was renamed, update the label here")

    collected = len(request.session.items)
    # Guard against a partial run (`pytest tests/test_landing_facts.py`) failing the whole
    # build over a number it cannot see. Only a full collection can judge this.
    if collected < 100:
        pytest.skip(f"only {collected} tests collected — run the full suite to check this")

    # Deliberately a FLOOR, not an equality. An exact figure would mean every test added
    # anywhere in the repo broke the build until someone edited a marketing page — a rule
    # annoying enough that it would eventually be deleted rather than obeyed. A floor
    # cannot overclaim, which is the property that actually matters, and it survives the
    # suite growing. The page writes it with a "+" so the claim matches the guarantee.
    claimed = _as_int(stated["automated tests"])
    assert claimed <= collected, (
        f"the landing page claims {stated['automated tests']} tests but only {collected} "
        f"exist. The page must never state more than the codebase has.")
    # ...and it must not drift so far below that it stops meaning anything.
    assert claimed >= collected * 0.85, (
        f"the landing page's floor of {claimed} is now well below the real {collected}. "
        f"Round it up in frontend/src/components/Landing.tsx.")


def test_the_landing_page_makes_no_fabricated_social_proof_claim():
    """The page's own rule, enforced.

    Its argument is that the engine refuses to state what it cannot support. A customer
    count, a testimonial or an award would refute that on the first screen — and would be
    the easiest thing in the world to add later without thinking about it.
    """
    src = _source_without_comments().lower()
    banned = [
        "trusted by", "customers worldwide", "join thousands", "our clients",
        "testimonial", "g2 ", "capterra", "award-winning", "industry-leading",
        "rated #1", "best-in-class", "certified by",
    ]
    hits = [b for b in banned if b in src]
    assert not hits, (
        f"the landing page has acquired unverifiable social proof: {hits}. Every claim on "
        f"that page has to be checkable against this codebase.")
