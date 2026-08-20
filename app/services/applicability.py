"""Which disclosure regimes COMPEL a filing from this entity, and which merely apply.

The question users actually ask — "which of these do I have to do?" — has three different
answers that get conflated constantly, and conflating them is how a compliance tool
becomes dangerous:

  MANDATORY    A law compels a filing once the entity crosses a size threshold in a
               jurisdiction where it operates. Missing it is a legal breach.
  CONDITIONAL  Compelled only for a specific activity or entity type — importing CBAM
               goods, operating an ETS installation, being an EU financial market
               participant. Size is irrelevant; what you DO is the test.
  VOLUNTARY    Nobody compels it. It may be commercially unavoidable (a customer demands
               CDP, a procurement gate demands EcoVadis) but that is a contract, not law,
               and this module never dresses one up as the other.
  METHODOLOGY  A standard you APPLY when preparing a disclosure. You never "owe" a filing
               under ISO 14064-1 or the GHG Protocol. Reporting a threshold for these
               would be a category error.

THE GOVERNING RULE HERE: an unknown is never a "no".

A company that has not entered its headcount must be told CANNOT_DETERMINE, never
NOT_REQUIRED. "You don't have to file CSRD" is a legally consequential statement, and
inferring it from an empty form field is the fail-open-on-a-disclosure failure in its
purest form. Every evaluation reports exactly which inputs it was missing.

This module is INDICATIVE, not advice. Thresholds are group-level, consolidated and
jurisdiction-specific, with exemptions this model does not carry; the output names the
tests it applied so a reader can check them against the cited instrument themselves.
"""
import json
from typing import Optional

# --- verdicts ---------------------------------------------------------------------------
REQUIRED = "required"                    # every applicable test passed
NOT_REQUIRED = "not_required"            # a test was applied and definitively failed
CANNOT_DETERMINE = "cannot_determine"    # a needed input is missing — NOT a "no"
OUT_OF_TERRITORY = "out_of_territory"    # entity operates in none of the regime's jurisdictions
ACTIVITY_BASED = "activity_based"        # conditional: depends on what you do, not your size
NOT_A_FILING = "not_a_filing"            # methodology standards
VOLUNTARY_SCHEME = "voluntary"           # no legal compulsion

# --- profile metrics the rules may test -------------------------------------------------
METRICS = ("employees", "annual_turnover", "balance_sheet_total")

# Closed vocabulary for where an entity operates. Coarse by design: these are REGIME
# territories, not countries — a rule asks "are you caught by the EU regime", and an
# entity established anywhere in the Union answers yes. Free text here would silently
# put an entity out of territory for every regime, which reads as "nothing applies to
# you" — the most dangerous possible wrong answer.
JURISDICTIONS: dict = {
    "EU": "European Union (any member state)",
    "UK": "United Kingdom",
    "US": "United States (federal)",
    "US-CA": "United States — California",
    "CH": "Switzerland",
    "NO": "Norway",
    "JP": "Japan",
    "AU": "Australia",
    "CA": "Canada",
    "SG": "Singapore",
    "BR": "Brazil",
    "IN": "India",
    "CN": "China",
    "OTHER": "Elsewhere / not listed",
}

# Where an entity's securities trade. Several regimes catch listed entities at a lower
# size threshold, or regardless of size — so these have to match the STATUTORY wording,
# not a plausible paraphrase. Companies Act 2006 s.385(2) defines a quoted company as one
# on the UK official list, "officially listed in an EEA State", or "admitted to dealing on
# either the New York Stock Exchange or the exchange known as Nasdaq". "A US national
# securities exchange" would over-capture (Cboe BZX, IEX and NYSE American are national
# securities exchanges but are not s.385(2)(c)), and "an EU regulated market" would both
# under-capture the EEA states outside the EU and confuse official listing with admission
# to a regulated market.
LISTING_MARKETS: dict = {
    "UK-OFFICIAL": "UK official list (LSE Main Market)",
    "EEA-OFFICIAL": "Officially listed in an EEA state (incl. Norway, Iceland, "
                    "Liechtenstein)",
    "NYSE": "New York Stock Exchange",
    "NASDAQ": "Nasdaq",
    "LSE-AIM": "LSE AIM (a non-quoted market for Companies Act purposes)",
    "EU-REGULATED": "Admitted to trading on an EU regulated market",
    "OTHER-EXCHANGE": "Another exchange",
}

_OPS = {
    ">":  lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<":  lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
}

# Combination logic for a regime's size tests.
#
# `all` / `any` / `any_2_of_3` are FLAT modes over the rule's `size_tests` list. They do
# not cover every real test: UK ESOS is
#
#     employees >= 250  OR  (turnover > GBP 44m  AND  balance sheet > GBP 38m)
#
# which is neither "any" (the financial limbs are conjunctive with each other) nor
# "any_2_of_3" (that is the Companies Act / CSRD shape, a different rule). Forcing it into
# a flat mode silently changes who is in scope, so nested rules instead carry a `size_expr`
# tree and leave `size_logic` unset. Expressing the test wrong is worse than not supporting
# it: a mis-scoped "not required" is a legal answer the user will act on.
ALL = "all"                  # every test must pass
ANY = "any"                  # at least one
ANY_2_OF_3 = "any_2_of_3"    # the classic EU/UK company-size test (2 of employees/turnover/assets)

# --- enforceability -----------------------------------------------------------------
# A statute's stated first reporting year is not the same as an enforceable obligation.
# California SB 261's date is on the books but the provision is enjoined pending appeal;
# a consumer reading `first_reporting_year` alone would overstate the duty. Status is
# therefore a first-class field rather than a caveat buried in prose.
IN_FORCE = "in_force"
ENJOINED = "enjoined"                # a court has suspended it
NOT_YET_TRANSPOSED = "not_yet_transposed"   # adopted but not yet law in member states
PROPOSED = "proposed"                # not yet adopted


def _profile(org) -> dict:
    """The entity profile as a plain dict, with JSON columns decoded defensively.

    NULL and `[]` mean DIFFERENT things and must not collapse: NULL is "never answered",
    `[]` is "answered: none". Collapsing them let an unrecorded listing status read as
    "not listed", which turned a quoted company — caught by SECR at any size — into a
    definite `not_required`. That is the exact failure this module exists to prevent.
    """
    def _list(raw):
        if raw is None:
            return None                      # never answered
        try:
            v = json.loads(raw)
        except ValueError:
            return None
        return [str(x).strip().upper() for x in v] if isinstance(v, list) else None

    return {
        "employees": org.employees,
        "annual_turnover": org.annual_turnover,
        "balance_sheet_total": org.balance_sheet_total,
        "currency": (org.financials_currency or "").upper() or None,
        "as_of": org.financials_as_of,
        "jurisdictions": _list(org.jurisdictions),
        "listed_markets": _list(org.listed_markets),
        "sector": org.sector,
    }


def _convert(value: Optional[float], have: Optional[str], want: Optional[str],
             rates: Optional[dict]) -> tuple:
    """(converted_value, note) — or (None, reason) when the conversion is unsafe.

    A EUR threshold tested against a GBP turnover without conversion is simply a wrong
    answer, so an unconvertible figure yields CANNOT_DETERMINE rather than a comparison
    the user would reasonably read as authoritative.
    """
    if value is None:
        return None, "not provided"
    if want and not have:
        # A bare number cannot be tested against a currency threshold: 600,000,000 is
        # EUR 600m or JPY 600m (~EUR 3.5m) and the tool cannot tell. Refuse.
        return None, (f"the figure has no currency, and the threshold is in {want} — "
                      f"set financials_currency before this test can be applied")
    if not want or have == want:
        return value, None
    entry = (rates or {}).get((have, want))
    if entry is None:
        return None, (f"the figure is in {have} but the threshold is in {want}, and no "
                      f"{have}->{want} rate is loaded — the test cannot be applied")
    rate, year = entry if isinstance(entry, tuple) else (entry, None)
    # The rate YEAR is part of the answer: a stale rate can flip a definite verdict, so
    # a reader must be able to see which one was applied rather than trust the number.
    stamp = f" (rate year {year})" if year is not None else ""
    return value * rate, f"converted {have}->{want} at {rate}{stamp}"


def _test_one(t: dict, prof: dict, rates: Optional[dict]) -> tuple:
    """Apply one threshold test. Returns (passed_or_None, detail)."""
    metric, op, threshold = t["metric"], t["operator"], t["value"]
    unit = t.get("unit")
    raw = prof.get(metric)
    # Monetary metrics carry a currency; headcount does not.
    if metric in ("annual_turnover", "balance_sheet_total"):
        value, note = _convert(raw, prof.get("currency"), unit, rates)
    else:
        value, note = raw, None
    if value is None:
        return None, {**t, "actual": None, "passed": None,
                      "why": note or f"{metric} not provided"}
    passed = _OPS[op](value, threshold)
    # `distance` is what makes the answer actionable: "you are 40 employees below the
    # threshold" is a plan; "not required" is a shrug that goes stale silently.
    # The rule's own `note` is the legal gloss ("net turnover; 'exceed' is strictly
    # greater than") and must survive — the conversion note goes in its own key rather
    # than overwriting it.
    return passed, {**t, "actual": value, "passed": passed,
                    "distance": round(value - threshold, 2), "conversion": note}


def _eval_expr(node: dict, prof: dict, rates: Optional[dict], detail: list,
               missing: list):
    """Three-valued (True/False/None) evaluation of a nested size expression.

    None propagates as "unknown", and — critically — short-circuits ONLY where the
    unknown cannot change the answer: `any` with a definite True is True whatever else is
    missing, `all` with a definite False is False. Everywhere else an unknown wins, so a
    missing input can never be silently read as a failing test.
    """
    if "metric" in node:                       # leaf
        passed, d = _test_one(node, prof, rates)
        detail.append(d)
        if passed is None:
            missing.append(node["metric"])
        return passed

    op = node["op"]
    vals = [_eval_expr(c, prof, rates, detail, missing) for c in node["of"]]
    if op == "any":
        if any(v is True for v in vals):
            return True
        return None if any(v is None for v in vals) else False
    if op == "all":
        if any(v is False for v in vals):
            return False
        return None if any(v is None for v in vals) else True
    if op == "any_2_of_3":
        # Generalised so it stays correct if a rule ever carries more than three limbs:
        # decidable only when the unknowns cannot change the outcome.
        n_true = sum(1 for v in vals if v is True)
        n_unknown = sum(1 for v in vals if v is None)
        if n_true >= 2:
            return True
        if n_true + n_unknown < 2:
            return False
        return None
    raise ValueError(f"unknown size-expression operator {op!r}")


def _evaluate_size(rule: dict, prof: dict, rates: Optional[dict]) -> dict:
    """Apply a regime's size tests. Returns verdict + per-test detail + what was missing."""
    # A nested rule supplies `size_expr`; a flat one supplies `size_tests` + `size_logic`,
    # which is wrapped into the same tree so there is exactly one evaluation path.
    expr = rule.get("size_expr")
    if expr is None:
        tests = rule.get("size_tests") or []
        if not tests:
            return {"verdict": REQUIRED, "tests": [], "missing": []}
        expr = {"op": rule.get("size_logic", ALL), "of": tests}

    detail: list = []
    missing: list = []
    result = _eval_expr(expr, prof, rates, detail, missing)
    verdict = (REQUIRED if result is True
               else NOT_REQUIRED if result is False
               else CANNOT_DETERMINE)
    return {"verdict": verdict, "tests": detail, "missing": sorted(set(missing))}


KINDS = ("mandatory", "conditional", "voluntary", "methodology")


def evaluate_one(key: str, rule: dict, org, rates: Optional[dict] = None) -> dict:
    # An unrecognised kind used to fall through every branch to the size evaluator, and
    # a rule with no size tests then returned REQUIRED — a typo'd kind inventing a legal
    # obligation out of nothing.
    if rule.get("kind") not in KINDS:
        raise ValueError(f"rule {key!r} has unknown kind {rule.get('kind')!r}; "
                         f"expected one of {KINDS}")
    prof = _profile(org)
    out = {
        "framework": key,
        "kind": rule["kind"],
        "label": rule.get("label", key),
        "jurisdiction": rule.get("jurisdiction"),
        "citation": rule.get("citation"),
        "summary": rule.get("summary"),
        "first_reporting_year": rule.get("first_reporting_year"),
        # Whether that year is actually ENFORCEABLE. A statute whose date is on the books
        # but whose operation a court has suspended must not be read as a live duty.
        "status": rule.get("status", IN_FORCE),
        "enforceable": rule.get("status", IN_FORCE) == IN_FORCE,
        "status_note": rule.get("status_note"),
        "phase_in": rule.get("phase_in"),
        "uncertainty": rule.get("uncertainty"),
        # What this rule provably does NOT test. Never dropped: a verdict that silently
        # ignores a real limb of the legal test is a confident wrong answer.
        "model_limitations": rule.get("model_limitations"),
        "confidence": rule.get("confidence", "medium"),
        "tests": [],
        "missing_inputs": [],
    }

    if rule["kind"] == "methodology":
        out["verdict"] = NOT_A_FILING
        out["reason"] = ("a standard you APPLY when preparing a disclosure, not a filing "
                         "you owe — there is no size threshold to meet")
        return out
    if rule["kind"] == "voluntary":
        out["verdict"] = VOLUNTARY_SCHEME
        out["reason"] = rule.get("voluntary_note") or (
            "no law compels this; it may still be required of you by a customer, "
            "investor or procurement process, which is a contract rather than a statute")
        return out

    # Territorial gate. A regime cannot compel an entity that operates nowhere in its
    # scope — but an entity that has stated NO jurisdictions is unknown, not exempt.
    applies_in = rule.get("applies_in")
    if applies_in:
        if not prof["jurisdictions"]:      # None (unanswered) or [] (answered: none)
            out["verdict"] = CANNOT_DETERMINE
            out["missing_inputs"] = ["jurisdictions"]
            out["reason"] = (f"this regime applies in {', '.join(applies_in)}; state where "
                             f"the entity operates or is established to test it")
            return out
        # A sub-national regime is not ruled out by naming only its parent territory:
        # "we operate in the US" does not tell us whether that includes California.
        # But ask the direct question FIRST. Declaring BOTH "US" and "US-CA" fully
        # determines the answer, and consulting the parent gate before the direct hit
        # threw that answer away — turning a known obligation into cannot_determine with
        # a reason ("you have not said whether that includes California") flatly
        # contradicted by the profile the caller submitted.
        parent = rule.get("territory_parent")
        direct_hit = bool(set(applies_in) & set(prof["jurisdictions"]))
        if parent and not direct_hit and parent in prof["jurisdictions"]:
            out["verdict"] = CANNOT_DETERMINE
            out["missing_inputs"] = ["jurisdictions"]
            named = ", ".join(JURISDICTIONS.get(j, j) for j in applies_in)
            out["reason"] = (f"you operate in {JURISDICTIONS.get(parent, parent)} but "
                             f"have not said whether that includes {named}, where this "
                             f"regime applies — add it if you do business there")
            return out
        if not direct_hit:
            out["verdict"] = OUT_OF_TERRITORY
            out["reason"] = (f"applies in {', '.join(applies_in)}; this entity operates "
                             f"in {', '.join(prof['jurisdictions'])}")
            return out

    if rule["kind"] == "conditional":
        out["verdict"] = ACTIVITY_BASED
        out["reason"] = rule.get("activity_test") or (
            "compelled by what the entity DOES rather than its size")
        return out

    # Listing gates. `listed_markets` is None when the entity has never answered, and
    # [] when it has answered "unlisted" — the two must not behave alike, because a
    # regime that catches quoted companies at ANY size cannot be ruled out by silence.
    listed = prof["listed_markets"]
    listing = rule.get("requires_listing")
    if listing:
        if listed is None:
            out["verdict"] = CANNOT_DETERMINE
            out["missing_inputs"] = ["listed_markets"]
            out["reason"] = (f"applies to entities listed on "
                             f"{', '.join(listing)} — state your listing status to test it")
            return out
        if not set(listing) & set(listed):
            out["verdict"] = NOT_REQUIRED
            out["reason"] = f"applies to entities listed on {', '.join(listing)}"
            return out
    caught_by_listing = rule.get("listed_regardless_of_size") or []
    if caught_by_listing:
        if listed is None:
            out["verdict"] = CANNOT_DETERMINE
            out["missing_inputs"] = ["listed_markets"]
            out["reason"] = ("this regime catches listed entities REGARDLESS of size, so "
                             "it cannot be ruled out without your listing status")
            return out
        hit = set(caught_by_listing) & set(listed)
        if hit:
            out["verdict"] = REQUIRED
            out["reason"] = (f"caught by listing status regardless of size "
                             f"({', '.join(sorted(hit))})")
            if not out["enforceable"]:
                out["reason"] += (f" — but the provision is currently {out['status']}: "
                                  f"{rule.get('status_note') or 'not presently enforceable'}")
            return out

    size = _evaluate_size(rule, prof, rates)
    out["verdict"] = size["verdict"]
    # An in-scope entity under a suspended provision is still IN SCOPE — the obligation
    # exists and may revive — so the verdict stands and the status qualifies it, rather
    # than the status quietly turning a "required" into a "no".
    out["tests"] = size["tests"]
    out["missing_inputs"] = size["missing"]
    if size["verdict"] == CANNOT_DETERMINE:
        out["reason"] = (f"cannot be determined without {', '.join(size['missing'])} — "
                         f"an unknown is NOT a 'no'")
    elif size["verdict"] == REQUIRED:
        out["reason"] = "meets the size test"
        if not out["enforceable"]:
            out["reason"] += (f" — but the provision is currently {out['status']}: "
                              f"{rule.get('status_note') or 'not presently enforceable'}")
    else:
        out["reason"] = "below the size test"
    return out


def evaluate(org, rules: dict, rates: Optional[dict] = None) -> dict:
    """Full applicability assessment for one organisation."""
    prof = _profile(org)
    results = [evaluate_one(k, r, org, rates) for k, r in sorted(rules.items())]

    buckets: dict = {}
    for r in results:
        buckets.setdefault(r["verdict"], []).append(r["framework"])

    # Profile completeness drives how much of the answer is even knowable, so it is stated
    # up front rather than left for the reader to infer from a pile of "cannot determine".
    absent = [f for f in ("employees", "annual_turnover", "balance_sheet_total")
              if prof.get(f) is None]
    if prof["jurisdictions"] is None or not prof["jurisdictions"]:
        absent.append("jurisdictions")
    if prof["listed_markets"] is None:
        absent.append("listed_markets")
    if prof.get("annual_turnover") is not None and not prof.get("currency"):
        absent.append("financials_currency")

    undetermined = len(buckets.get(CANNOT_DETERMINE, []))
    unenforceable = [r["framework"] for r in results
                     if r["verdict"] == REQUIRED and not r.get("enforceable", True)]
    return {
        "entity_profile": prof,
        "profile_complete": not absent,
        "profile_missing": absent,
        "results": results,
        "by_verdict": {k: sorted(v) for k, v in sorted(buckets.items())},
        "counts": {
            "required": len(buckets.get(REQUIRED, [])),
            "cannot_determine": undetermined,
            "activity_based": len(buckets.get(ACTIVITY_BASED, [])),
            "voluntary": len(buckets.get(VOLUNTARY_SCHEME, [])),
            "methodology": len(buckets.get(NOT_A_FILING, [])),
            "required_but_not_enforceable": len(unenforceable),
        },
        "required_but_not_enforceable": sorted(unenforceable),
        "caveats": [
            "INDICATIVE ONLY — not legal advice. Thresholds apply at group and "
            "consolidated level with jurisdiction-specific exemptions this model does "
            "not carry. Every test applied is shown with its citation so you can check "
            "it against the instrument itself.",
            "An unknown is never a 'no'. Frameworks under 'cannot_determine' are "
            "unanswered, not excluded — completing the entity profile resolves them.",
            "Monetary thresholds are stated in the regime's own currency. Where your "
            "figures are in another currency they are converted at a reference rate, and "
            "the rate and its year are shown on each test — a threshold you sit close to "
            "can turn on which rate applied.",
        ] + ([f"{undetermined} framework(s) could not be assessed because the entity "
              f"profile is missing {', '.join(absent)}."] if undetermined else [])
          + ([f"{', '.join(sorted(unenforceable))}: you meet the size test, but the "
              f"provision is not presently enforceable (see each entry's status). Scope "
              f"is stated as-if-in-force because a suspension can be lifted."]
             if unenforceable else []),
    }
