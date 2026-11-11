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


def _scope_run(db, spec, name, ghgp_category):
    from app.models import EmissionFactor, ActivityRecord, Organisation
    org = Organisation(name=name)
    db.add(org); db.commit(); db.refresh(org)
    for cat, unit, val, qty in spec:
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
        if ghgp_category is not None:
            a.ghgp_category = ghgp_category
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
    """
    return _scope_run(db, _DECLARED_SPEC, name, ghgp_category=None)
