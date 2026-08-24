"""Remaining compliance renderers on the shared inventory substrate:
EU Taxonomy alignment (KPIs), EU/UK ETS MRV, and UK ESOS energy.

All fail-closed and org-scoped; ETS/ESOS read a run's frozen figures, Taxonomy
reads the org's declared economic activities.
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import TaxonomyActivity, CalculationRun
from ..services.boundary import boundary_completeness
from .summary import summary
from .secr import _energy_kwh


def taxonomy_report(db: Session, organisation_id: int, reporting_year: int) -> dict:
    acts = db.query(TaxonomyActivity).filter(
        TaxonomyActivity.organisation_id == organisation_id,
        TaxonomyActivity.reporting_year == reporting_year).all()

    def kpi(attr):
        total = sum(getattr(a, attr) for a in acts)
        eligible = sum(getattr(a, attr) for a in acts if a.eligible)
        aligned = sum(getattr(a, attr) for a in acts
                      if a.eligible and a.substantial_contribution
                      and a.dnsh_pass and a.minimum_safeguards_pass)
        return {
            "total": round(total, 2),
            "eligible": round(eligible, 2),
            "aligned": round(aligned, 2),
            "eligible_pct": round(100.0 * eligible / total, 2) if total else 0.0,
            "aligned_pct": round(100.0 * aligned / total, 2) if total else 0.0,
        }

    blockers = []
    if not acts:
        blockers.append(f"no economic activities recorded for {reporting_year}")

    return {
        "framework": "EU Taxonomy",
        "reporting_year": reporting_year,
        "disclosure_ready": not blockers,
        "blockers": blockers,
        "activities": len(acts),
        "turnover": kpi("turnover"),
        "capex": kpi("capex"),
        "opex": kpi("opex"),
        "note": "Alignment = eligible AND substantial contribution AND DNSH AND "
                "minimum safeguards. KPIs as % of turnover/CapEx/OpEx.",
    }


def ets_mrv_report(db: Session, organisation_id: int, scheme: str,
                   run_id: Optional[int] = None, verified: bool = False) -> dict:
    """EU/UK ETS Monitoring, Reporting & Verification — annual Scope 1 (direct)
    emissions from the run, which is what an ETS installation surrenders against."""
    s = summary(db, organisation_id=organisation_id, run_id=run_id)
    run_info = s.get("run")
    if run_info is None:
        return {"framework": f"{scheme} MRV", "report_ready": False,
                "blockers": ["no calculation run exists"]}
    run = db.get(CalculationRun, run_info["id"])
    scope1 = next((r["co2e"] for r in s["by_scope"] if r["scope"] == "1"), 0.0)

    blockers = []
    if s.get("partial"):
        blockers.append(f"run is PARTIAL — {s['partial_reasons']}")
    if s["coverage"]["stale"]:
        blockers.append("run is STALE — recompute")
    if not verified:
        blockers.append("ETS requires accredited third-party verification — set "
                        "verified=true once an accredited verifier has issued an opinion")

    return {
        "framework": f"{scheme} MRV",
        "report_ready": not blockers,
        "blockers": blockers,
        "run": run_info,
        # An unrecognised category defaults to Scope 3, so a fugitive or process source
        # whose category this build does not know is assumed OUT of the direct figure an
        # installation surrenders allowances against — an UNDERSTATEMENT, and the one
        # caveat this report cannot afford to drop. None when nothing was assumed.
        "scope_assumptions": s.get("scope_assumptions"),
        "direct_emissions_tco2e": round(scope1 / 1000.0, 3),  # Scope 1 = ETS direct
        "verified": verified,
        "note": "Reportable emissions are direct (Scope 1) under the MRR/AVR; "
                "verification is mandatory before surrendering allowances.",
    }


def esos_report(db: Session, organisation_id: int, run_id: Optional[int] = None) -> dict:
    """UK ESOS — total energy consumption (kWh) and significant-use split, from
    the run's energy-carrier activities."""
    s = summary(db, organisation_id=organisation_id, run_id=run_id)
    run_info = s.get("run")
    if run_info is None:
        return {"framework": "UK ESOS", "report_ready": False,
                "blockers": ["no calculation run exists"]}
    run = db.get(CalculationRun, run_info["id"])
    # ESOS audits significant energy USE (gross physical energy at operated sites),
    # but its emissions context is consolidated — gate on the boundary.
    energy = _energy_kwh(db, run)
    total_kwh = energy["total_kwh"]
    by_carrier = {c: energy[c] for c in ("electricity", "gas", "diesel")}
    # The share denominator is the REPORTED carriers, not all energy the org consumed.
    # Where carriers are omitted those two differ, and calling the result a share of
    # "total energy" would overstate every carrier by the same factor.
    omitted = energy.get("carriers_omitted") or {}
    significant = ({c: round(100.0 * v / total_kwh, 1) for c, v in by_carrier.items()}
                   if total_kwh else {})

    # ESOS was the one renderer with no completeness gate at all — an incomplete or
    # stale inventory would still emit a "ready" energy figure. Same doctrine as
    # SECR/SB253/ETS: fail closed.
    blockers = []
    if s.get("partial"):
        blockers.append(f"run is PARTIAL — {s['partial_reasons']}")
    if s["coverage"]["stale"]:
        blockers.append("run is STALE — recompute")
    if total_kwh <= 0:
        blockers.append("no energy-carrier activity in this run")
    blockers.extend(boundary_completeness(db, run).get("blockers", []))
    # _energy_kwh MEASURES which energy-denominated carriers it could not include, and
    # this renderer used to discard that and publish "total energy consumption" anyway —
    # over a denominator the engine itself knew was short. ESOS Regulation 4 is an audit
    # of TOTAL energy consumption, so an inventory known to be incomplete is not a
    # smaller total, it is not the total. Fail closed, and name the carriers.
    if omitted:
        blockers.append(
            f"energy-denominated activities in {', '.join(sorted(omitted))} are not "
            f"included in this kWh total, though their emissions are in the figures "
            f"beside it — ESOS assesses TOTAL energy consumption, so a total measured as "
            f"incomplete cannot be published as one. Map these carriers or state their "
            f"energy explicitly.")

    return {
        "framework": "UK ESOS",
        "report_ready": not blockers,
        "blockers": blockers,
        "run": run_info,
        "total_energy_kwh": round(total_kwh, 3),
        "by_carrier_kwh": {c: round(v, 3) for c, v in by_carrier.items()},
        # Named for the denominator it actually uses.
        "share_of_reported_carriers_pct": significant,
        "carriers_reported": energy.get("carriers_reported"),
        "carriers_omitted": sorted(omitted) if omitted else [],
        "energy_basis": energy.get("basis"),
        "energy_notes": energy.get("notes") or [],
        "note": "Energy consumption across the carriers this run reports. ESOS audits "
                "significant energy use and identifies efficiency measures. Shares are "
                "over `total_energy_kwh` — which equals total consumption only when "
                "`carriers_omitted` is empty.",
    }
