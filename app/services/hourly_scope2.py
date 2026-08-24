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

    # ONE LEDGER PER HOUR, DEPLETED AS IT IS DRAWN ON.
    #
    # The pool used to be rebuilt independently for every (hour, region) load row and
    # never depleted, so a certificate deliverable to two regions was counted in full
    # against BOTH of their loads in the same hour: 100 kWh of certificates matched
    # 200 kWh of load, and the CFE score read 100% where the honest answer was 50%.
    # That is annual netting reintroduced across space instead of time, inside the one
    # method whose entire purpose is to forbid netting.
    #
    # ALLOCATION ORDER IS A DISCLOSED ASSUMPTION, not an implementation detail: when two
    # load regions compete for one certificate, somebody has to lose, and who loses moves
    # the reported figures. Own-region load is served first (a certificate issued where
    # the load sits is consumed there before being exported), then deliverable imports in
    # a deterministic region order. Both passes are stated in the payload.
    for hour in sorted({h for (h, _) in loads}):
        ledger = {}
        for (chour, cregion), items in certs.items():
            if chour == hour:
                ledger.setdefault(cregion, []).extend(
                    {"kwh": i["kwh"], "rate": i["kg_co2e_per_kwh"]} for i in items)

        regions = sorted(r for (h, r) in loads if h == hour)
        remaining = {r: loads[(hour, r)] for r in regions}
        matched_by = {r: 0.0 for r in regions}
        emissions_by = {r: 0.0 for r in regions}
        cross_by = {r: 0.0 for r in regions}
        available_by = {r: 0.0 for r in regions}
        for r in regions:
            servable = {r} | links.get(r, set())
            available_by[r] = sum(i["kwh"] for cr, its in ledger.items()
                                  if cr in servable for i in its)
            total_load += remaining[r]

        def _draw(region, cert_region):
            """Move kWh out of one region's remaining supply into one region's load."""
            need = remaining[region]
            for item in ledger.get(cert_region, []):
                if need <= _EPS_KWH:
                    break
                take = min(need, item["kwh"])
                if take <= _EPS_KWH:
                    continue
                item["kwh"] -= take
                need -= take
                matched_by[region] += take
                emissions_by[region] += take * item["rate"]
                if cert_region != region:
                    cross_by[region] += take
            remaining[region] = need

        for r in regions:                       # pass 1: own region
            _draw(r, r)
        for r in regions:                       # pass 2: deliverable imports
            for cregion in sorted(links.get(r, set())):
                if cregion != r:
                    _draw(r, cregion)

        # Certificates in regions that could not serve ANY load this hour, plus whatever
        # nobody drew. Surplus never carries to another hour — that restriction is the
        # reform — and it is reported because it is exactly what annual netting would
        # have allowed to offset some other hour's deficit.
        deliverable_to_someone = set()
        for r in regions:
            deliverable_to_someone |= {r} | links.get(r, set())
        for cregion, its in ledger.items():
            leftover = sum(i["kwh"] for i in its)
            if cregion in deliverable_to_someone:
                surplus_kwh += leftover
            else:
                rejected_undeliverable_kwh += leftover

        if regions and all(remaining[r] <= _EPS_KWH for r in regions) \
                and any(loads[(hour, r)] > _EPS_KWH for r in regions):
            # Counts HOURS, not (hour, region) buckets: an hour is fully matched only
            # when every region's load in it was matched.
            hours_fully_matched += 1

        for r in regions:
            load_kwh = loads[(hour, r)]
            matched = matched_by[r]
            unmatched = remaining[r]
            matched_total += matched
            cross_region_matched_kwh += cross_by[r]
            matched_emissions += emissions_by[r]

            info = intensity.get((hour, r))
            residual = (info or {}).get("residual")
            if unmatched > _EPS_KWH:
                if residual is None:
                    # NEVER substitute the average here: residual >= average always, so
                    # falling back would understate. The hour is reported as unpriced.
                    unpriced_kwh += unmatched
                    unpriced_hours.add(hour)
                    residual_missing_regions.add(
                        f"{r} (no intensity row)" if info is None
                        else f"{r} (no residual rate)")
                else:
                    unmatched_emissions += unmatched * residual

            per_hour.append({
                "hour": hour, "grid_region": r,
                "load_kwh": round(load_kwh, 6),
                # What COULD have served this region's load. Two regions served by
            # one certificate both see it here, so this column may sum to more than
            # the certificates that exist; matched_kwh is the spend and never does.
            "certificates_available_kwh": round(available_by[r], 6),
                "matched_kwh": round(matched, 6),
                "unmatched_kwh": round(unmatched, 6),
                "cross_region_matched_kwh": round(cross_by[r], 6),
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
            "hours_fully_matched_basis": (
                "Counts HOURS, not (hour, region) buckets: an hour counts only when "
                "EVERY region's load in it was matched."),
            "allocation": {
                "ledger": "depleting_per_hour",
                "order": ["own_region_load_first", "deliverable_imports_by_region_name"],
                "note": "Within an hour a certificate is spent ONCE. When two load "
                        "regions can both be served by the same certificate, own-region "
                        "load draws on it first and deliverable imports follow in a "
                        "deterministic region order. Who wins moves the reported figures, "
                        "so the rule is disclosed rather than left in the code. Surplus "
                        "never carries to another hour.",
            },
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
