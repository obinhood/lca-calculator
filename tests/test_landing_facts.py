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

# Every page a stranger can read without signing in. The no-fabrication rule belongs to
# all of them, not just the one that happened to exist when the rule was written — a
# second page is exactly where a guard with one target stops guarding.
MARKETING_PAGES = [
    LANDING,
    pathlib.Path("frontend/src/components/Homepage.tsx"),
    pathlib.Path("frontend/src/components/SiteChrome.tsx"),
]

# `{ v: "122", l: "API endpoints", ... }` — the figure and the thing it counts.
_FACT = re.compile(r'\{\s*v:\s*"([^"]+)",\s*l:\s*"([^"]+)"')


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)      # block + JSX comments
    return re.sub(r"^\s*//.*$", " ", src, flags=re.M)      # line comments


def _source_without_comments() -> str:
    """The platform page's source with comments stripped.

    Scanning the raw file for banned phrases matched the comment that explains why the
    page has no testimonials — a rule tripping over its own rationale. Only what a reader
    can actually see should be judged.
    """
    return _strip_comments(LANDING.read_text())


def _stated() -> dict:
    """What the landing page currently claims, keyed by the label under the number."""
    return {label: value for value, label in _FACT.findall(_source_without_comments())}


def _as_int(s: str) -> int:
    """Digits only — the page writes figures for humans ("1,470+"), not for parsers."""
    digits = re.sub(r"[^0-9]", "", s)
    if not digits:
        raise AssertionError(f"{s!r} is not a figure this guard can check")
    return int(digits)


def _actual_operations() -> int:
    """Operations the running app actually serves.

    Counted from the registered routes rather than by grepping `@app.<verb>(` in
    app/main.py. The decorator grep was a proxy for the real surface, and it silently
    stopped being one the moment routes began to be registered through
    `app.add_api_route` (the 19 paginated list endpoints are a loop over a table, not 19
    decorators). It undercounted by exactly those 19 — which is still a figure the
    codebase disagrees with, just in the flattering direction, and this file exists to
    stop that.

    FastAPI's own docs routes are excluded: they are the framework's, not this API's.
    HEAD and OPTIONS are excluded because they are generated, not authored.
    """
    from fastapi.routing import APIRoute

    from app.main import app
    builtin = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
    return len([
        (r.path, m)
        for r in app.routes if isinstance(r, APIRoute)
        for m in r.methods
        if m not in ("HEAD", "OPTIONS") and r.path not in builtin
    ])


def test_the_landing_page_states_the_real_endpoint_count():
    stated = _stated()
    assert "API endpoints" in stated, (
        "the rigour section no longer carries an endpoint figure — if it was removed on "
        "purpose, delete this test; if it was renamed, update the label here")
    actual = _actual_operations()
    assert _as_int(stated["API endpoints"]) == actual, (
        f"the landing page says {stated['API endpoints']} API endpoints; the app serves "
        f"{actual}. Update frontend/src/components/Landing.tsx.")


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
    banned = [
        "trusted by", "customers worldwide", "join thousands", "our clients",
        "testimonial", "g2 ", "capterra", "award-winning", "industry-leading",
        "rated #1", "best-in-class", "certified by",
    ]
    offences = {}
    for page in MARKETING_PAGES:
        assert page.exists(), f"{page} is listed as a marketing page but does not exist"
        src = _strip_comments(page.read_text()).lower()
        hits = [b for b in banned if b in src]
        if hits:
            offences[page.name] = hits
    assert not offences, (
        f"a signed-out page has acquired unverifiable social proof: {offences}. Every "
        f"claim on those pages has to be checkable against this codebase.")


def test_no_signed_out_page_invents_a_services_business():
    """There is no consulting arm, implementation team or support SLA to sell.

    The homepage's "ways to use it" section sits exactly where a services page would, and
    is the most natural place for someone to later add an offering that does not exist.
    The page says outright what is not sold; this stops the opposite creeping in beside it.
    """
    invented = [
        "our consultants", "consulting services", "implementation team",
        "dedicated account manager", "24/7 support", "managed service",
        "professional services", "we will audit", "we certify", "our experts",
    ]
    offences = {}
    for page in MARKETING_PAGES:
        src = _strip_comments(page.read_text()).lower()
        hits = [p for p in invented if p in src]
        if hits:
            offences[page.name] = hits
    assert not offences, (
        f"a signed-out page now advertises a service this product does not provide: "
        f"{offences}. The platform is software you run yourself; it prepares the evidence "
        f"an assuror works from and does not sign an opinion.")
