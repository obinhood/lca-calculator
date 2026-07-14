"""Nature module (TNFD LEAP + SBTN targets) on a spatial, qualitative substrate.

This is deliberately NOT the carbon inventory: nature disclosure is site-based
and largely qualitative, so there is no single CO2e number to compute. What is
computable is computed fail-closed (spatial footprint, exposure in sensitive
locations, water use in water-stressed basins, a priority screen); the narrative
TNFD pillars (governance, strategy) and scenario analysis are explicitly listed
as not covered rather than faked.

Everything is org-scoped. Impacts/dependencies are reached only through sites
the org owns (join on organisation_id), so there is no cross-tenant leak.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import NatureSite, NatureImpactDependency, NatureTarget

# --- Controlled vocabularies (fail-closed: unknown values are rejected) -------

WATER_STRESS_LEVELS = ("unknown", "none", "low", "medium", "high", "extreme")
# A basin is "water-stressed" for exposure metrics at these bands.
WATER_STRESSED = ("high", "extreme")

MATERIALITY = ("low", "medium", "high")

# IPBES direct drivers of nature change (what an activity does TO nature).
IMPACT_DRIVERS = (
    "land_use_change",       # incl. freshwater/sea-use change
    "freshwater_use",        # water withdrawal / consumption
    "marine_use",
    "resource_use",          # extraction of biotic/abiotic resources
    "pollution",             # to air / water / soil, incl. waste, nutrients, plastics
    "climate_change",
    "invasive_species",
)

# Ecosystem services an activity depends ON (what nature provides to it).
DEPENDENCY_SERVICES = (
    "water_provision",
    "water_purification",
    "pollination",
    "climate_regulation",
    "flood_and_storm_protection",
    "soil_quality",
    "erosion_control",
    "disease_and_pest_control",
    "raw_materials",
    "genetic_materials",
    "other",
)

REALMS = ("freshwater", "land", "ocean", "biodiversity")


def valid_driver(kind: str, driver: str) -> bool:
    if kind == "impact":
        return driver in IMPACT_DRIVERS
    if kind == "dependency":
        return driver in DEPENDENCY_SERVICES
    return False


# --- Locate -------------------------------------------------------------------

def sensitivity_reasons(site: NatureSite) -> list:
    reasons = []
    if site.in_protected_area:
        reasons.append("protected_area")
    if site.in_kba:
        reasons.append("key_biodiversity_area")
    if site.water_stress in WATER_STRESSED:
        reasons.append(f"water_stress:{site.water_stress}")
    return reasons


def site_is_sensitive(site: NatureSite) -> bool:
    return bool(sensitivity_reasons(site))


# --- LEAP assessment ----------------------------------------------------------

def leap_assessment(db: Session, organisation_id: int) -> dict:
    sites = (db.query(NatureSite)
             .filter(NatureSite.organisation_id == organisation_id)
             .order_by(NatureSite.id).all())

    if not sites:
        return {
            "framework": "TNFD LEAP",
            "report_ready": False,
            "blockers": ["no nature sites recorded — add at least one site (Locate)"],
            "locate": {"site_count": 0},
            "note": "TNFD LEAP: Locate, Evaluate, Assess, Prepare. Add sites to begin.",
        }

    site_ids = [s.id for s in sites]
    # Impacts/dependencies reached ONLY via this org's sites.
    items = (db.query(NatureImpactDependency)
             .filter(NatureImpactDependency.site_id.in_(site_ids))
             .order_by(NatureImpactDependency.id).all())
    by_site: dict = {}
    for it in items:
        by_site.setdefault(it.site_id, []).append(it)

    # -- Locate --
    total_area = sum(s.area_hectares or 0.0 for s in sites)
    sensitive_sites = [s for s in sites if site_is_sensitive(s)]
    area_sensitive = sum(s.area_hectares or 0.0 for s in sensitive_sites)
    locate = {
        "site_count": len(sites),
        "total_area_hectares": round(total_area, 3),
        "sensitive_site_count": len(sensitive_sites),
        "area_in_sensitive_locations_hectares": round(area_sensitive, 3),
        # None (not 0) when there is no area data, so exposure isn't read as "0%".
        "pct_area_in_sensitive_locations": (
            round(100.0 * area_sensitive / total_area, 2) if total_area > 0 else None),
        "sites": [{
            "id": s.id, "name": s.name, "country": s.country, "biome": s.biome,
            "area_hectares": round(s.area_hectares or 0.0, 3),
            "water_stress": s.water_stress,
            "sensitive": site_is_sensitive(s),
            "sensitivity_reasons": sensitivity_reasons(s),
        } for s in sites],
    }

    # -- Evaluate --
    impacts = [it for it in items if it.kind == "impact"]
    deps = [it for it in items if it.kind == "dependency"]

    def group(rows, keyattr):
        out: dict = {}
        for r in rows:
            b = out.setdefault(getattr(r, keyattr), {"count": 0, "high": 0, "medium": 0, "low": 0})
            b["count"] += 1
            if r.materiality in ("high", "medium", "low"):
                b[r.materiality] += 1
        return out

    stressed_site_ids = {s.id for s in sites if s.water_stress in WATER_STRESSED}
    unknown_stress_ids = {s.id for s in sites if s.water_stress == "unknown"}
    # Water WITHDRAWN in water-stressed basins — a TNFD core metric. Only counts
    # freshwater_use impacts whose site is high/extreme stress AND has a value.
    water_stressed_withdrawal = 0.0
    missing_value = 0            # stressed-basin freshwater impacts with no metric_value
    unclassifiable = 0           # freshwater impacts on unknown-stress sites
    withdrawal_units = set()     # metric_units summed — must be consistent to trust the total
    missing_unit = 0             # summed values carrying NO metric_unit (unit unverifiable)
    for it in impacts:
        if it.driver != "freshwater_use":
            continue
        if it.site_id in stressed_site_ids:
            if it.metric_value is None:
                missing_value += 1
            else:
                water_stressed_withdrawal += it.metric_value
                if it.metric_unit:
                    withdrawal_units.add(it.metric_unit)
                else:
                    missing_unit += 1
        elif it.site_id in unknown_stress_ids:
            unclassifiable += 1

    warnings = []
    if len(withdrawal_units) > 1:
        warnings.append("water withdrawal summed across INCONSISTENT metric_units "
                        f"{sorted(withdrawal_units)} — the total is not meaningful until "
                        "units are normalised")
    if missing_unit:
        warnings.append(f"{missing_unit} freshwater-use value(s) in the withdrawal total carry "
                        "NO metric_unit — the total's unit is unverified")
    if missing_value:
        warnings.append(f"{missing_value} freshwater-use impact(s) in water-stressed basins have no "
                        "metric_value — water withdrawal figure is INCOMPLETE, not zero")
    if unclassifiable:
        warnings.append(f"{unclassifiable} freshwater-use impact(s) sit on sites with unknown water "
                        "stress — cannot classify as stressed-basin withdrawal")
    sites_without_assessment = [s.id for s in sites if s.id not in by_site]
    if sites_without_assessment:
        warnings.append(f"{len(sites_without_assessment)} site(s) have no impacts/dependencies "
                        "recorded — Evaluate is incomplete for them")

    evaluate = {
        "impacts_by_driver": group(impacts, "driver"),
        "dependencies_by_service": group(deps, "driver"),
        "impact_count": len(impacts),
        "dependency_count": len(deps),
        "water_withdrawal_in_water_stressed_basins": round(water_stressed_withdrawal, 3),
        "water_withdrawal_unit": "m3 (per recorded metric_unit; verify units are consistent)",
    }

    # -- Assess --  priority interfaces: sensitive location AND a high-materiality impact.
    priority = []
    for s in sensitive_sites:
        high = [it for it in by_site.get(s.id, []) if it.kind == "impact" and it.materiality == "high"]
        if high:
            priority.append({
                "site_id": s.id, "name": s.name,
                "sensitivity_reasons": sensitivity_reasons(s),
                "high_materiality_impacts": [it.driver for it in high],
            })
    assess = {
        "priority_site_count": len(priority),
        "priority_sites": priority,
        "high_materiality_impacts": sum(1 for it in impacts if it.materiality == "high"),
        "high_materiality_dependencies": sum(1 for it in deps if it.materiality == "high"),
        "note": "Priority interface = site in a sensitive location AND carrying a "
                "high-materiality impact; these are the locations TNFD expects "
                "disclosed with location-specific detail.",
    }

    # -- Metrics (TNFD core global disclosure metrics — the computable subset) --
    metrics = {
        "total_spatial_footprint_hectares": round(total_area, 3),
        "area_in_sensitive_locations_hectares": round(area_sensitive, 3),
        "pct_area_in_sensitive_locations": locate["pct_area_in_sensitive_locations"],
        "sites_in_sensitive_locations": len(sensitive_sites),
        "water_withdrawal_in_water_stressed_basins": round(water_stressed_withdrawal, 3),
        "pollution_impacts": sum(1 for it in impacts if it.driver == "pollution"),
    }

    return {
        "framework": "TNFD LEAP",
        "report_ready": True,
        "blockers": [],
        "warnings": warnings,
        "locate": locate,
        "evaluate": evaluate,
        "assess": assess,
        "metrics": metrics,
        "prepare": {
            "covered": ["Locate (sites + sensitive-location exposure)",
                        "Evaluate (impact/dependency register by driver)",
                        "Assess (priority-interface screen)",
                        "Metrics (TNFD core spatial/water metrics — computable subset)"],
            "not_covered": ["Governance & Strategy narrative pillars",
                            "Scenario analysis",
                            "Financial-effect quantification of nature risks/opportunities",
                            "State-of-nature condition metrics (need ecological survey data)"],
        },
        "note": "TNFD is spatial and qualitative — no single CO2e figure. Figures "
                "here are the computable core; narrative pillars are not produced.",
    }


# --- SBTN targets -------------------------------------------------------------

def sbtn_report(db: Session, organisation_id: int) -> dict:
    targets = (db.query(NatureTarget)
               .filter(NatureTarget.organisation_id == organisation_id)
               .order_by(NatureTarget.id).all())

    lines = []
    for t in targets:
        delta = t.target_value - t.baseline_value
        pct = (round(100.0 * delta / t.baseline_value, 2)
               if t.baseline_value not in (0, 0.0) else None)
        lines.append({
            "id": t.id, "realm": t.realm, "name": t.name,
            "baseline_value": t.baseline_value, "baseline_unit": t.baseline_unit,
            "baseline_year": t.baseline_year,
            "target_value": t.target_value, "target_year": t.target_year,
            "change": round(delta, 4),          # signed: negative = reduction
            "change_pct": pct,
            "validated": t.validated,
        })

    by_realm: dict = {}
    for ln in lines:
        by_realm.setdefault(ln["realm"], 0)
        by_realm[ln["realm"]] += 1

    return {
        "framework": "SBTN",
        "report_ready": len(targets) > 0,
        "blockers": [] if targets else ["no nature targets set"],
        "target_count": len(targets),
        "targets_by_realm": by_realm,
        "validated_count": sum(1 for ln in lines if ln["validated"]),
        "targets": lines,
        "steps": ["1 Assess", "2 Interpret & prioritise", "3 Measure, set & disclose",
                  "4 Act", "5 Track"],
        "note": "SBTN science-based targets for nature (freshwater, land, ocean, "
                "biodiversity). 'validated' reflects SBTN validation status; change "
                "is signed (negative = reduction).",
    }
