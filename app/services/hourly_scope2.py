"""Hourly temporal matching for Scope 2 — the proposed GHG Protocol revision.

The revision under consultation would require energy attribute certificates to be
matched to consumption ON AN HOURLY BASIS and within physically deliverable
boundaries, plus a marginal-emissions-impact metric. This computes the hourly
figure as a PARALLEL method beside the annual location- and market-based totals.
It never rewrites them: `compute_co2e` is untouched, and an organisation with no
hourly data is unaffected in every respect.

WHY HOURLY IS A DIFFERENT NUMBER, NOT A REFINEMENT OF THE ANNUAL ONE
Annual market-based accounting nets a whole year: 100 MWh of surplus solar in June
cancels 100 MWh of grid draw in December, and the resulting figure can read zero
for an operation that ran on coal every winter night. Hourly matching forbids that
netting. Surplus in one hour NEVER carries to another — that single rule is the
entire reform, and it is why the hourly figure is typically far worse than the
annual one for the same portfolio and the same certificates.

THREE THINGS THIS REFUSES TO DO
1. Treat an unmetered hour as a zero-load hour. An hour with no meter reading is
   missing, and a missing hour scored as "load 0, matched 0" would count as
   perfectly matched and drag every CFE score toward 100%. Hour coverage is
   reported and an incomplete period is labelled as such.
2. Price unmatched load at the grid average. Unmatched market-based load takes the
   RESIDUAL intensity, which is always >= the average because other purchasers'
   attributes have been stripped out. Where no residual rate is published the hour
   is reported as unpriced — never silently substituted with the average, which
   would understate, the failure direction this platform privileges above all.
3. Move a certificate across a deliverability boundary on its own authority.
   Same-region is deliverable; anything else requires a declared link, and every
   rejected certificate is counted and reported rather than quietly dropped.
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from ..models import (
    DeliverabilityLink, GranularCertificate, HourlyGridIntensity, HourlyLoad,
    Organisation, ReportingPeriod,
)

HOURLY_SCOPE2_VERSION = "s2h-v1"

# Certificates whose production window spans more than one hour are apportioned
# EVENLY across the hours they cover. EnergyTag granular certificates are issued
# per hour, so this is a tolerance for imperfect inputs rather than the expected
# case — and it is disclosed per statement, because even apportionment is an
# assumption about a generation profile nobody measured.
_EPS_KWH = 1e-9


def _parse_hour(value: Optional[str]) -> Optional[datetime]:
    """ISO-8601 to a UTC-naive datetime, or None. Never guesses a format."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "").replace("z", "")
    if "+" in s[10:]:
        s = s[:10] + s[10:].split("+")[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%dT%H", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _hour_key(dt: datetime) -> str:
    """The canonical hour bucket: everything is attributed to the hour it starts in."""
    return dt.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:00:00")


def _hours_between(start: datetime, end: datetime) -> list:
    """Hour buckets a [start, end) window touches. End-exclusive."""
    if end <= start:
        return []
    out, cur = [], start.replace(minute=0, second=0, microsecond=0)
    while cur < end:
        out.append(_hour_key(cur))
        cur += timedelta(hours=1)
    return out


def _region(value: Optional[str]) -> str:
    """Region identity, normalised the same way on both sides of a match."""
    return (value or "").strip().upper()


def deliverable_regions(db: Session, organisation_id: int) -> dict:
    """{load_region: {certificate regions that may serve it}} from declared links.

    Same-region deliverability is implicit and needs no row; this returns only the
    declared exceptions, which the matcher unions with the identity relation.
    """
    out = defaultdict(set)
    for link in db.query(DeliverabilityLink).filter(
            DeliverabilityLink.organisation_id == organisation_id).all():
        out[_region(link.to_region)].add(_region(link.from_region))
    return dict(out)


def _load_by_hour(db: Session, organisation_id: int,
                  start: datetime, end: datetime) -> tuple:
    """{(hour, region): kwh} plus the set of metering points and unparseable rows."""
    rows = db.query(HourlyLoad).filter(
        HourlyLoad.organisation_id == organisation_id).all()
    by_hour, points, bad = defaultdict(float), set(), 0
    for r in rows:
        dt = _parse_hour(r.hour_start)
        if dt is None:
            bad += 1
            continue
        if not (start <= dt < end):
            continue
        by_hour[(_hour_key(dt), _region(r.grid_region))] += r.kwh or 0.0
        points.add(r.metering_point or "default")
    return dict(by_hour), points, bad


def _certificates_by_hour(db: Session, organisation_id: int, period_id: Optional[int],
                          start: datetime, end: datetime) -> tuple:
    """{(hour, region): [{kwh, intensity, ref}]} for certificates usable in this period.

    A certificate retired against a DIFFERENT period is excluded — that exclusion is
    the double-counting guard, and the count of excluded certificates is returned so
    it can be disclosed rather than inferred from a gap in the totals.
    """
    rows = db.query(GranularCertificate).filter(
        GranularCertificate.organisation_id == organisation_id).all()
    by_hour = defaultdict(list)
    retired_elsewhere, unparseable, outside = 0, 0, 0
    apportioned_multi_hour = 0

    for c in rows:
        if c.retired_for_period_id is not None and c.retired_for_period_id != period_id:
            retired_elsewhere += 1
            continue
        s, e = _parse_hour(c.production_start), _parse_hour(c.production_end)
        if s is None or e is None or e <= s:
            unparseable += 1
            continue
        hours = [h for h in _hours_between(s, e)
                 if start <= datetime.strptime(h, "%Y-%m-%dT%H:00:00") < end]
        if not hours:
            outside += 1
            continue
        full = _hours_between(s, e)
        if len(full) > 1:
            apportioned_multi_hour += 1
        # Even apportionment across the covered hours. Certificates issued per hour
        # (the EnergyTag norm) take the whole quantity and this is a no-op.
        share = (c.kwh or 0.0) / len(full)
        for h in hours:
            by_hour[(h, _region(c.grid_region))].append({
                "kwh": share,
                "kg_co2e_per_kwh": c.kg_co2e_per_kwh or 0.0,
                "certificate_ref": c.certificate_ref,
                "issuer": c.issuer,
                "technology": c.technology,
            })
    return (dict(by_hour), {"retired_for_another_period": retired_elsewhere,
                            "unparseable_window": unparseable,
                            "outside_period": outside,
                            "apportioned_multi_hour": apportioned_multi_hour})


def _intensities(db: Session, start: datetime, end: datetime) -> dict:
    """{(hour, region): {average, residual}} — latest source/version wins per key."""
    rows = db.query(HourlyGridIntensity).order_by(HourlyGridIntensity.id).all()
    out = {}
    for r in rows:
        dt = _parse_hour(r.hour_start)
        if dt is None or not (start <= dt < end):
            continue
        out[(_hour_key(dt), _region(r.grid_region))] = {
            "average": r.kg_co2e_per_kwh_average,
            "residual": r.kg_co2e_per_kwh_residual,
            "source": r.source, "version": r.version,
        }
    return out


def match(db: Session, organisation_id: int, period_id: int) -> dict:
    """Hour-by-hour certificate matching over one reporting period.

    Returns the CFE score, the matched/unmatched split, the hourly market-based
    emissions, hour coverage, and every reason a certificate did not count.
    """
    org = db.get(Organisation, organisation_id)
    period = db.query(ReportingPeriod).filter(
        ReportingPeriod.id == period_id,
        ReportingPeriod.organisation_id == organisation_id).first()
    if period is None:
        return {"available": False,
                "reason": f"reporting period {period_id} not found for this organisation"}

    start = _parse_hour(period.start_date)
    end_day = _parse_hour(period.end_date)
    if start is None or end_day is None:
        return {"available": False, "period_id": period_id,
                "reason": "the reporting period has no usable start/end date; hourly "
                          "matching cannot be scoped without one"}
    # ReportingPeriod.end_date is an INCLUSIVE day; the hour window runs to the end
    # of that day.
    end = end_day + timedelta(days=1)

    loads, points, bad_load_rows = _load_by_hour(db, organisation_id, start, end)
    if not loads:
        return {"available": False, "period_id": period_id,
                "reason": "no hourly load data for this period; an hourly Scope 2 "
                          "figure cannot be derived from annual consumption.",
                "malformed_load_rows": bad_load_rows}

    certs, cert_exclusions = _certificates_by_hour(db, organisation_id, period_id, start, end)
    intensity = _intensities(db, start, end)
    links = deliverable_regions(db, organisation_id)

    total_load = matched_total = 0.0
    matched_emissions = unmatched_emissions = 0.0
    unpriced_kwh = 0.0
    unpriced_hours = set()
    residual_missing_regions = set()
    cross_region_matched_kwh = 0.0
    hours_fully_matched = 0
    per_hour = []

    rejected_undeliverable_kwh = 0.0
    # Certificate supply that went unused in its own hour. Reported because it is
    # exactly what annual netting would have (wrongly) allowed to offset a deficit
    # in some other hour.
    surplus_kwh = 0.0

    for (hour, load_region) in sorted(loads):
        load_kwh = loads[(hour, load_region)]
        total_load += load_kwh

        servable = {load_region} | links.get(load_region, set())
        pool, rejected = [], 0.0
        for (chour, cregion), items in certs.items():
            if chour != hour:
                continue
            if cregion in servable:
                pool.extend(items)
            else:
                rejected += sum(i["kwh"] for i in items)
        rejected_undeliverable_kwh += rejected

        available = sum(i["kwh"] for i in pool)
        matched = min(load_kwh, available)
        unmatched = load_kwh - matched
        matched_total += matched
        surplus_kwh += max(0.0, available - load_kwh)
        if load_kwh > _EPS_KWH and unmatched <= _EPS_KWH:
            hours_fully_matched += 1
        if matched > _EPS_KWH:
            cross_region_matched_kwh += min(
                matched,
                sum(i["kwh"] for (ch, cr), its in certs.items() if ch == hour
                    and cr in servable and cr != load_region for i in its))

        # Matched kWh carry the certificates' own intensity — usually zero, but a
        # certificate is an attribute claim and its stated intensity is respected.
        if available > _EPS_KWH and matched > _EPS_KWH:
            weighted = sum(i["kwh"] * i["kg_co2e_per_kwh"] for i in pool) / available
            matched_emissions += matched * weighted

        info = intensity.get((hour, load_region))
        residual = (info or {}).get("residual")
        if unmatched > _EPS_KWH:
            if residual is None:
                # NEVER substitute the average here: residual >= average always, so
                # falling back would understate. The hour is reported as unpriced.
                unpriced_kwh += unmatched
                unpriced_hours.add(hour)
                if info is None:
                    residual_missing_regions.add(f"{load_region} (no intensity row)")
                else:
                    residual_missing_regions.add(f"{load_region} (no residual rate)")
            else:
                unmatched_emissions += unmatched * residual

        per_hour.append({
            "hour": hour, "grid_region": load_region,
            "load_kwh": round(load_kwh, 6),
            "certificates_available_kwh": round(available, 6),
            "matched_kwh": round(matched, 6),
            "unmatched_kwh": round(unmatched, 6),
            "residual_kg_co2e_per_kwh": residual,
            "unpriced": unmatched > _EPS_KWH and residual is None,
        })

    hours_with_load = len({h for (h, _) in loads})
    expected_hours = int((end - start).total_seconds() // 3600)
    complete = hours_with_load >= expected_hours

    cfe_pct = round(100.0 * matched_total / total_load, 4) if total_load > _EPS_KWH else None
    priced = unpriced_kwh <= _EPS_KWH

    return {
        "available": True,
        "version": HOURLY_SCOPE2_VERSION,
        "organisation": {"id": organisation_id, "name": org.name if org else None},
        "period": {"id": period.id, "label": period.label,
                   "start_date": period.start_date, "end_date": period.end_date},
        "cfe": {
            "cfe_score_pct": cfe_pct,
            "matched_kwh": round(matched_total, 6),
            "unmatched_kwh": round(total_load - matched_total, 6),
            "total_load_kwh": round(total_load, 6),
            "hours_fully_matched": hours_fully_matched,
            "hours_with_load": hours_with_load,
            "note": "Hourly carbon-free-energy score: matched kWh over consumed kWh, "
                    "computed hour by hour. Surplus in one hour never offsets a "
                    "deficit in another — that restriction IS the reform, and it is "
                    "why this figure is normally well below an annual matched claim.",
        },
        "emissions": {
            "hourly_market_based_kg_co2e": (
                round(matched_emissions + unmatched_emissions, 6) if priced else None),
            "matched_component_kg_co2e": round(matched_emissions, 6),
            "unmatched_component_kg_co2e": round(unmatched_emissions, 6),
            "priced_completely": priced,
            "unpriced_kwh": round(unpriced_kwh, 6),
            "unpriced_hours": len(unpriced_hours),
            "unpriced_reason": (
                "Unmatched load in these hours has no published RESIDUAL intensity. "
                "The grid average is deliberately NOT substituted: residual is always "
                ">= average, so the substitution would understate the figure. The "
                "hourly emissions total is withheld until every hour is priced."
            ) if not priced else None,
            "residual_gaps": sorted(residual_missing_regions),
        },
        "hour_coverage": {
            "hours_in_period": expected_hours,
            "hours_with_load_data": hours_with_load,
            "complete": complete,
            "malformed_load_rows": bad_load_rows,
            "metering_points": sorted(points),
            "warning": None if complete else (
                f"Load data covers {hours_with_load} of {expected_hours} hours. An "
                f"unmetered hour is MISSING, not a zero-load hour — scoring it as "
                f"'load 0, matched 0' would count as perfectly matched and inflate "
                f"the CFE score. This score describes only the metered hours."),
        },
        "deliverability": {
            "declared_links": {k: sorted(v) for k, v in sorted(links.items())},
            "cross_region_matched_kwh": round(cross_region_matched_kwh, 6),
            "rejected_undeliverable_kwh": round(rejected_undeliverable_kwh, 6),
            "note": "Same-region supply is deliverable implicitly; anything else needs "
                    "a declared link. Rejected certificate energy is reported, not "
                    "silently dropped.",
        },
        "certificates": {
            "excluded": cert_exclusions,
            "surplus_kwh_not_carried_forward": round(surplus_kwh, 6),
            "surplus_note": "Certificate energy exceeding load in its own hour. Under "
                            "ANNUAL market-based accounting this would have offset a "
                            "deficit in some other hour; hourly matching forbids that, "
                            "and this is the quantity the difference rests on.",
        },
        "hours": per_hour,
    }
