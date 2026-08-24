"""Extra demo data, so the one-click demo shows the platform rather than a subset.

`POST /demo/seed` used to populate exactly two tables — EmissionFactor and
ActivityRecord — so an evaluator landed on a spend-and-energy calculator and saw none of
the hourly Scope 2 matching, period-over-period screening or Scope 3 screening that the
product is largely made of. Built, shipped, and invisible without hand-crafting fixtures.

It also, as of the intensity-denominator gates, needed a REPORTING PERIOD: an intensity
ratio divides a period-scoped total by a per-period quantity, so an unscoped demo run
would make SECR, CDP, ESRS E1 and EcoVadis all refuse. A demo whose reports all block is
a worse advert than no demo.

Everything here is idempotent and additive: it never touches a run, and re-seeding an org
that already has this data does nothing.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import (
    ReportingPeriod, HourlyLoad, GranularCertificate, HourlyGridIntensity,
    ActivityRecord, Scope3CategoryDeclaration,
)

DEMO_YEAR = 2025
DEMO_REGION = "GB"

# A representative winter day. 24 hours of load with a solar-shaped certificate supply,
# which is the shape that makes the point: annual matching would call this 100% clean,
# and hourly matching does not, because the certificates arrive in daylight and the load
# does not stop at night.
_LOAD_KWH = [
    40, 38, 36, 35, 35, 38, 52, 68, 82, 90, 94, 96,
    96, 94, 92, 90, 88, 86, 80, 72, 64, 56, 48, 44,
]
_CERT_KWH = [
    0, 0, 0, 0, 0, 0, 0, 6, 24, 52, 78, 96,
    104, 98, 80, 54, 26, 6, 0, 0, 0, 0, 0, 0,
]
# Residual mix is always >= the average: uncovered load is priced at the residual, never
# at the grid average, so the two are seeded separately.
_AVG = 0.19
_RESIDUAL = 0.31


def _hour(h: int) -> str:
    return f"{DEMO_YEAR}-06-15T{h:02d}:00:00"


def ensure_period(db: Session, organisation_id: int) -> ReportingPeriod:
    """The reporting year the demo activities fall in."""
    label = f"FY{DEMO_YEAR}"
    row = db.query(ReportingPeriod).filter(
        ReportingPeriod.organisation_id == organisation_id,
        ReportingPeriod.label == label).first()
    if row is None:
        row = ReportingPeriod(organisation_id=organisation_id, label=label,
                              start_date=f"{DEMO_YEAR}-01-01",
                              end_date=f"{DEMO_YEAR}-12-31", frozen=False)
        db.add(row); db.commit(); db.refresh(row)
    return row


def ensure_prior_period(db: Session, organisation_id: int) -> ReportingPeriod:
    """A prior year, so period-over-period screening has something to compare against."""
    label = f"FY{DEMO_YEAR - 1}"
    row = db.query(ReportingPeriod).filter(
        ReportingPeriod.organisation_id == organisation_id,
        ReportingPeriod.label == label).first()
    if row is None:
        row = ReportingPeriod(organisation_id=organisation_id, label=label,
                              start_date=f"{DEMO_YEAR - 1}-01-01",
                              end_date=f"{DEMO_YEAR - 1}-12-31", frozen=False)
        db.add(row); db.commit(); db.refresh(row)
    return row


def seed_hourly(db: Session, organisation_id: int, period_id: int) -> dict:
    """One metered day with certificates and grid intensity, for hourly Scope 2."""
    if db.query(HourlyLoad).filter(
            HourlyLoad.organisation_id == organisation_id).first() is not None:
        return {"seeded": False, "reason": "hourly data already present"}

    for h, kwh in enumerate(_LOAD_KWH):
        db.add(HourlyLoad(organisation_id=organisation_id, metering_point="HQ",
                          hour_start=_hour(h), kwh=float(kwh), grid_region=DEMO_REGION,
                          source_file="demo"))
    for h, kwh in enumerate(_CERT_KWH):
        if kwh <= 0:
            continue
        db.add(GranularCertificate(
            organisation_id=organisation_id, issuer="EnergyTag (demo)",
            certificate_ref=f"DEMO-GC-{h:02d}",
            production_start=_hour(h), production_end=_hour(h + 1) if h < 23
            else f"{DEMO_YEAR}-06-16T00:00:00",
            kwh=float(kwh), technology="solar", grid_region=DEMO_REGION,
            kg_co2e_per_kwh=0.0,
            retired_at=f"{DEMO_YEAR + 1}-01-15T00:00:00",
            retired_for_period_id=period_id))
    for h in range(24):
        if db.query(HourlyGridIntensity).filter(
                HourlyGridIntensity.grid_region == DEMO_REGION,
                HourlyGridIntensity.hour_start == _hour(h)).first() is None:
            db.add(HourlyGridIntensity(
                grid_region=DEMO_REGION, hour_start=_hour(h),
                kg_co2e_per_kwh_average=_AVG, kg_co2e_per_kwh_residual=_RESIDUAL,
                source="DEMO", version="v1"))
    db.commit()
    return {"seeded": True, "hours": 24,
            "certificate_kwh": float(sum(_CERT_KWH)), "load_kwh": float(sum(_LOAD_KWH))}


def seed_series_keys(db: Session, organisation_id: int) -> dict:
    """Enrol the energy activities in period-over-period screening.

    Declared, never inferred: the key is the thing that says "these rows are the same
    meter over time", and guessing it would make the screen compare unrelated series.
    """
    updated = 0
    for a in db.query(ActivityRecord).filter(
            ActivityRecord.organisation_id == organisation_id,
            ActivityRecord.series_key.is_(None)).all():
        if a.category in ("electricity", "gas"):
            a.series_key = f"demo:{a.category}:HQ"
            updated += 1
    db.commit()
    return {"activities_enrolled": updated}


def seed_scope3_declarations(db: Session, organisation_id: int, period_id: int,
                             *, categories_with_lines=None,
                             scope12_energy: bool = False) -> dict:
    """Screen all 15 categories, so SBTi significance is determinable.

    Without a screen every category reads as un-inventoried and significance suspends —
    correctly, but it makes the demo look broken rather than strict.

    `categories_with_lines` is READ FROM A RUN, not guessed. Declaring a category
    "included" that carries no lines is itself a blocker, and the first version of this
    seed guessed the set and produced exactly that — a demo shipping the defect the
    product exists to catch.
    """
    have = {d.category for d in db.query(Scope3CategoryDeclaration).filter(
        Scope3CategoryDeclaration.organisation_id == organisation_id,
        Scope3CategoryDeclaration.reporting_period_id == period_id).all()}
    if len(have) >= 15:
        return {"seeded": False, "reason": "already screened"}

    with_lines = set(categories_with_lines or ())
    # GHGP anti-gaming rule B9: category 3 (fuel- and energy-related activities) CANNOT
    # be not_applicable while the run reports Scope 1/2 energy — upstream fuel and T&D
    # necessarily occur. It is not_material with a screening estimate, or it is included.
    has_energy = bool(scope12_energy)
    added = 0
    for cat in range(1, 16):
        if cat in have:
            continue
        if cat in with_lines:
            kw = dict(status="included",
                      method_description="Activity data x emission factor for the "
                                         "period (demo dataset).")
        elif cat == 3 and has_energy:
            kw = dict(status="not_material", screening_estimate_tco2e=0.4,
                      materiality_threshold_pct=5.0,
                      screening_method="spend_and_energy_proxy",
                      justification="Upstream fuel and T&D on the demo energy volumes "
                                    "screen below the 5% threshold. A real inventory "
                                    "must screen this on its own figures.")
        else:
            kw = dict(status="not_applicable",
                      justification="Demo dataset does not model this category; a real "
                                    "inventory must justify each exclusion on its own "
                                    "facts, and this text would not satisfy an assuror.")
        db.add(Scope3CategoryDeclaration(
            organisation_id=organisation_id, reporting_period_id=period_id,
            category=cat, standard_version="2004-revised",
            screened_at=f"{DEMO_YEAR + 1}-01-15T00:00:00", declared_by="demo seed", **kw))
        added += 1
    db.commit()
    return {"categories_declared": added}


def scope3_categories_in_run(db: Session, run) -> set:
    """Which GHGP Scope 3 categories the run's frozen lines actually carry."""
    from ..models import EmissionLineItem
    from .frozen import parse_detail
    out = set()
    for (details,) in db.query(EmissionLineItem.details).filter(
            EmissionLineItem.run_id == run.id,
            EmissionLineItem.method == "location",
            EmissionLineItem.scope == "3").all():
        cat = parse_detail(details).get("ghgp_category")
        if isinstance(cat, int):
            out.add(cat)
    return out


def _run_has_scope12_energy(db: Session, run) -> bool:
    from ..models import EmissionLineItem
    return db.query(EmissionLineItem).filter(
        EmissionLineItem.run_id == run.id,
        EmissionLineItem.method == "location",
        EmissionLineItem.scope.in_(("1", "2"))).first() is not None


def seed_all(db: Session, organisation_id: int, *, run=None) -> dict:
    """Everything above, in order. Returns what each step did.

    `run` is a first-pass calculation used only to read which Scope 3 categories actually
    carry lines. The realistic workflow is screen-then-compute: you cannot honestly
    declare a category included until you have looked at what is in it.
    """
    period = ensure_period(db, organisation_id)
    prior = ensure_prior_period(db, organisation_id)
    with_lines = scope3_categories_in_run(db, run) if run is not None else set()
    has_energy = _run_has_scope12_energy(db, run) if run is not None else False
    return {
        "reporting_period": {"id": period.id, "label": period.label},
        "prior_period": {"id": prior.id, "label": prior.label},
        "hourly_scope2": seed_hourly(db, organisation_id, period.id),
        "series_screening": seed_series_keys(db, organisation_id),
        "scope3_screen": seed_scope3_declarations(
            db, organisation_id, period.id, categories_with_lines=with_lines,
            scope12_energy=has_energy),
    }
