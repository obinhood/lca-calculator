"""Shared helper: drive a run all the way to Scope 3 disclosure-ready.

The realistic workflow is screen-then-compute: declare all 15 GHGP categories,
then recompute so the screen is frozen onto the run. Categories that carry
emission lines are `included`; Category 3 is `not_material` when the run has
Scope 1/2 energy (upstream fuel/T&D necessarily occurs, so it can't be
not_applicable — anti-gaming rule B9); everything else is `not_applicable`.
"""
import json

from app.models import Scope3CategoryDeclaration, EmissionLineItem, ReportingPeriod
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
