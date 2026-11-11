"""Shared helper: drive a run all the way to Scope 3 disclosure-ready.

The realistic workflow is screen-then-compute: declare all 15 GHGP categories,
then recompute so the screen is frozen onto the run. Categories that carry
emission lines are `included`; Category 3 is `not_material` when the run has
Scope 1/2 energy (upstream fuel/T&D necessarily occurs, so it can't be
not_applicable — anti-gaming rule B9); everything else is `not_applicable`.
"""
import json

from app.models import (
    Scope3CategoryDeclaration, EmissionLineItem, ReportingPeriod, RunFinancedLine,
)
from app.reports.scope3 import scope3_by_ghgp_category
from app.services.ghgp import SEVEN_CRITERIA
from app.services.calc import compute_co2e


def make_period(db, org_id, label="FY25", start="2025-01-01", end="2025-12-31"):
    p = ReportingPeriod(organisation_id=org_id, label=label,
                        start_date=start, end_date=end, frozen=False)
    db.add(p); db.commit(); db.refresh(p)
    return p


def screen_complete(db, org_id, period_id, run):
    """Declare all 15 categories against `run`'s observed lines. Does NOT recompute."""
    inv = scope3_by_ghgp_category(db, run)
    has_lines = {int(k) for k, v in inv["categories"].items() if v["line_count"]}
    # Cat 15 is 'included' when the run froze financed (PCAF) lines — those are
    # RunFinancedLine rows, not EmissionLineItem lines, so they aren't in has_lines.
    if db.query(RunFinancedLine).filter(RunFinancedLine.run_id == run.id).count():
        has_lines.add(15)
    has_energy = db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id, EmissionLineItem.method == "location",
        EmissionLineItem.scope.in_(("1", "2"))).first() is not None
    crit = {k: {"met": False, "note": "screened immaterial"} for k in SEVEN_CRITERIA}
    for c in range(1, 16):
        if c in has_lines:
            kw = dict(status="included",
                      method_description="Activity data x emission factor for the period, "
                                         "supplier- and average-data methods.")
        elif c == 3 and has_energy:
            kw = dict(status="not_material", screening_estimate_tco2e=0.001,
                      materiality_threshold_pct=5.0, criteria=json.dumps(crit),
                      justification="Upstream fuel and grid T&D losses screened at under 1% "
                                    "of gross emissions across all seven criteria; immaterial.")
        else:
            kw = dict(status="not_applicable",
                      justification="This value-chain activity does not occur in the "
                                    "reporting entity's operations for the period.")
        db.add(Scope3CategoryDeclaration(
            organisation_id=org_id, reporting_period_id=period_id, category=c,
            screened_at="2025-06-30", updated_at="2025-06-30", **kw))
    db.commit()


def ready_run(db, org_id):
    """A period-scoped, fully-screened, disclosure-ready run for the org's activities."""
    p = make_period(db, org_id)
    discover = compute_co2e(db, org_id, reporting_period_id=p.id)
    screen_complete(db, org_id, p.id, discover)
    return compute_co2e(db, org_id, reporting_period_id=p.id), p


# --- The scope-declaration gate: a matched pair of runs -----------------------------
# Same three real sources, same quantities, same factor values. The ONLY difference is
# whether the category name happens to be one of the ~28 keys in SCOPE_RULES. That makes
# the second run a true negative control for any gate keyed on an assumed scope: a gate
# that refuses both is refusing everything, which is as useless as one that refuses
# neither.
_ASSUMED_SPEC = [("purchased_steam_site_a", "kWh", 0.20, 50_000),   # really Scope 2
                 ("r410a_topup", "kg", 2088.0, 12),                 # really Scope 1
                 ("site_generator_fuel", "L", 2.68, 3_000)]         # really Scope 1
_DECLARED_SPEC = [("electricity", "kWh", 0.20, 50_000),             # Scope 2 by rule
                  ("refrigerant", "kg", 2088.0, 12),                # Scope 1 by rule
                  ("diesel", "L", 2.68, 3_000)]                     # Scope 1 by rule

ASSUMED_CATEGORIES = tuple(c for c, *_ in _ASSUMED_SPEC)


def _scope_run(db, spec, name, ghgp_category, scopes=None):
    from app.models import EmissionFactor, ActivityRecord, Organisation
    org = Organisation(name=name)
    db.add(org); db.commit(); db.refresh(org)
    for row in spec:
        # A 5th element overrides `ghgp_category` for that line alone, which is what a
        # MIXED run needs: a Scope 3 line carries its GHGP category while the Scope 1
        # and Scope 2 lines beside it must not.
        cat, unit, val, qty = row[:4]
        line_category = row[4] if len(row) > 4 else ghgp_category
        f = EmissionFactor(source="DEFRA_DEMO", version="2024.1", geography="GB",
                           year=2024, category=cat, subcategory="", unit=unit,
                           gwp_set="AR6", value=val)
        db.add(f); db.commit(); db.refresh(f)
        a = ActivityRecord(organisation_id=org.id, date="2025-01-01", category=cat,
                           subcategory="", description="", quantity=qty, unit=unit,
                           geo="GB", factor_id=f.id)
        # Set ON PURPOSE on the assumed run. Without it these renderers block on the
        # GHGP *Scope 3 category* gate instead — a different fact with a different
        # remedy, and following that blocker's own instruction (assign a category)
        # clears it while leaving Scope 2 at 0.0. A fixture that has not discharged it
        # cannot tell you whether the scope gate fires at all.
        #
        # None on the declared run, and necessarily so: there every line is correctly
        # Scope 1 or Scope 2, and a GHGP Scope 3 category on a non-Scope-3 line is
        # itself a conflict the inventory gate reports. The category travels with the
        # scope, so the two runs cannot share one value.
        if line_category is not None:
            a.ghgp_category = line_category
        # `scopes` declares the scope ON THE ACTIVITY, which calc.py:848 reads FIRST:
        # `if a.scope: scope, scope_source = a.scope, "explicit"`. That is the discharge
        # `scope_declaration_completeness` itself prescribes, and it is what lets a
        # control hold the category STRING fixed while changing the split.
        if scopes:
            a.scope = scopes[cat]
        db.add(a)
    db.commit()
    run, _period = ready_run(db, org.id)
    return org, run


def assumed_scope_run(db, name="UnrecognisedCats"):
    """(org, run) whose EVERY category misses SCOPE_RULES.

    Each line freezes ``scope_source == "assumed_scope3"``, so Scope 1 and Scope 2 are
    both absent and the entire inventory sits in Scope 3 — the zeros are a GUESS.
    """
    return _scope_run(db, _ASSUMED_SPEC, name, ghgp_category=1)


def declared_scope_run(db, name="RecognisedCats"):
    """(org, run) identical to :func:`assumed_scope_run` but with recognised categories.

    Freezes ``scope_source == "category_rule"`` on every line, so the scope split is
    derived from a rule rather than guessed and the gate must stay silent.

    NOTE WHAT THIS PAIR DOES *NOT* ISOLATE. It changes the category STRING, and the
    string is read by at least two independent mechanisms: ``SCOPE_RULES`` in
    services/calc.py, and the ``_ENERGY_CARRIERS`` allowlist in
    ``reports/secr._energy_kwh``, which never looks at scope at all. So a figure that
    moves across this pair is not necessarily produced by the scope split — it may be
    produced by the renaming. Measured: ``esos`` moves 9 numbers across this pair
    (``total_energy_kwh``, ``by_carrier_kwh.*``, ``share_of_reported_carriers_pct.*``)
    and publishes no scope figure anywhere in its payload. Use
    :func:`explicit_scope_run` to attribute a movement to the split.
    """
    return _scope_run(db, _DECLARED_SPEC, name, ghgp_category=None)


_EXPLICIT_SCOPES = {"purchased_steam_site_a": "2",   # really Scope 2
                    "r410a_topup": "1",              # really Scope 1
                    "site_generator_fuel": "1"}      # really Scope 1


def explicit_scope_run(db, name="ExplicitScopes"):
    """(org, run) with the SAME category strings as :func:`assumed_scope_run`, but the
    correct scope DECLARED on each activity.

    This is the control that isolates the scope split. Every category name is
    byte-identical to the assumed run, so no category-keyed mechanism anywhere in the
    product can move — only the split does, because `a.scope` is set and each line
    freezes ``scope_source == "explicit"`` instead of ``"assumed_scope3"``.

    Measured against :func:`assumed_scope_run`: ``esos``'s nine moving numbers drop to
    three (``run.id``, ``run.organisation_id``, ``run.reporting_period_id`` — identity,
    which a twin run cancels), while ``scope3_inventory`` and ``ets_mrv`` move exactly
    as many numbers as before. That is the difference between a control and a confound.

    `ghgp_category` is None here for the same reason as on the declared run: these lines
    are Scope 1 and Scope 2, and a GHGP Scope 3 category on a non-Scope-3 line is itself
    a conflict the inventory gate reports. The category travels with the scope.
    """
    return _scope_run(db, _ASSUMED_SPEC, name, ghgp_category=None,
                      scopes=_EXPLICIT_SCOPES)


# --- The negative control the pair above CANNOT provide -----------------------------
# `declared_scope_run` is a true control for a gate keyed on an assumed scope, but it is
# NOT one for anything computed over Scope 3: every line on it is correctly Scope 1 or
# Scope 2, so scope 3 categories 1-14 total zero and `significance()` already answers
# determinable: False for its OWN legitimate reason ("categories 1-14 total zero"). A
# significance gate tested only against that pair reads as working while refusing
# everything, because both members come back non-determinable.
#
# So the control has to carry REAL Scope 3 emissions beside a declared Scope 1/2 split:
# recognised categories throughout, one of them a Scope 3 carrier with its GHGP category.
# Significance is then genuinely determinable and a correct gate must stay silent on it.
_DECLARED_WITH_SCOPE3_SPEC = [
    ("electricity", "kWh", 0.20, 50_000, None),      # Scope 2 by rule
    ("diesel", "L", 2.68, 3_000, None),              # Scope 1 by rule
    ("freight", "tonne.km", 0.10, 100_000, 4),       # Scope 3 by rule, GHGP cat 4
]


def declared_scope_run_with_scope3(db, name="RecognisedCatsWithScope3"):
    """(org, run) with a RULE-DERIVED scope split and genuine Scope 3 emissions.

    Every category is in SCOPE_RULES, so no line freezes ``assumed_scope3``, and the
    Scope 3 freight line gives categories 1-14 a non-zero denominator. This is the run on
    which a significance answer must still be produced.
    """
    return _scope_run(db, _DECLARED_WITH_SCOPE3_SPEC, name, ghgp_category=None)
