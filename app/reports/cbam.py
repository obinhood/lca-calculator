"""CBAM annual declaration renderer (definitive period).

One declaration per organisation per import year: every goods line with its
embedded direct/indirect emissions, basis (verified actuals vs defaults,
substitutions flagged), the certificate-obligation basis (indirect only where
Annex II puts it in scope), category totals, and the certificate estimate
(CBAM factor x origin-carbon-price deduction) at a caller-supplied EU ETS
reference price.

Fail-closed: unresolvable lines AND goods rows whose import_date cannot be
parsed are surfaced as errors and block declaration readiness — a malformed
date must not silently drop a line from every year's declaration forever.
"""
import math
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CbamGood
from ..services.calc import _parse_iso_date
from ..services.cbam import (
    line_embedded, certificates_due, cbam_factor, CbamResolutionError,
    DE_MINIMIS_TONNES, DEFINITIVE_PERIOD_START, obligation_phase_in,
)


def cbam_declaration(db: Session, organisation_id: int, year: int,
                     ets_price_eur_per_t: Optional[float] = None) -> dict:
    # Fetch ALL of the org's goods and partition by PARSED date — a string
    # range filter would silently exclude malformed dates from every year.
    all_goods = db.query(CbamGood).filter(
        CbamGood.organisation_id == organisation_id).order_by(CbamGood.id).all()

    goods = []
    errors = []
    for g in all_goods:
        d = _parse_iso_date(g.import_date)
        if d is None:
            errors.append({"good_id": g.id, "cn_code": g.cn_code,
                           "error": f"unparseable import_date {g.import_date!r} — "
                                    f"line cannot be attributed to any declaration year"})
            continue
        if d.year == year:
            goods.append(g)

    blockers = []
    lines = []
    total_direct = total_indirect = total_obligation = 0.0
    total_mass = 0.0
    certs_total = 0.0
    free_alloc_total = 0.0
    no_benchmark: list = []
    agnostic_defaults: list = []
    by_category: dict = {}
    price_ok = (ets_price_eur_per_t is not None
                and math.isfinite(ets_price_eur_per_t) and ets_price_eur_per_t > 0)

    for g in goods:
        try:
            line = line_embedded(db, g)
        except CbamResolutionError as exc:
            errors.append({"good_id": g.id, "cn_code": g.cn_code, "error": str(exc)})
            continue
        by_category[line["good_category"]] = \
            by_category.get(line["good_category"], 0.0) + line["embedded_total_t"]
        total_direct += line["embedded_direct_t"]
        total_indirect += line["embedded_indirect_t"]
        total_obligation += line["obligation_basis_t"]
        total_mass += g.quantity_tonnes
        if line["free_allocation_t"] is None:
            no_benchmark.append(line["cn_code"])
        else:
            free_alloc_total += line["free_allocation_t"]
        if line["default_country_basis"] == "country_agnostic_fallback":
            agnostic_defaults.append(line["cn_code"])
        if price_ok:
            certs = certificates_due(
                line["obligation_basis_t"], g.carbon_price_paid_eur_per_t,
                ets_price_eur_per_t, year, free_allocation=line["free_allocation_t"])
            line["certificates_due_t"] = None if certs is None else round(certs, 6)
            # A line with no benchmark contributes NOTHING to the total, and the
            # missing-benchmark blocker below stops the total being read as complete.
            certs_total += line["certificates_due_t"] or 0.0
        lines.append(line)

    if not goods and not errors:
        blockers.append(f"no CBAM goods recorded for {year}")
    if errors:
        blockers.append(f"{len(errors)} goods line(s) unresolvable "
                        f"(no emissions basis or unparseable import date)")
    if not price_ok:
        blockers.append("ets_price_eur_per_t required (finite, > 0) for the "
                        "certificate estimate")
    if no_benchmark and year >= DEFINITIVE_PERIOD_START:
        # Fail CLOSED. Without the EU benchmark the free-allocation adjustment cannot be
        # computed, and the only alternative (obligation x CBAM factor) understates the
        # bill for every above-benchmark importer — the direction that must never be
        # guessed. The line reports its emissions and abstains on the certificate count.
        blockers.append(
            f"no EU production benchmark loaded for {len(no_benchmark)} line(s) "
            f"({', '.join(sorted(set(no_benchmark))[:5])}) — the free-allocation "
            f"adjustment, and therefore the certificate count, cannot be computed")

    notes = [
        "Verified actual installation values take precedence; unverified actuals "
        "are substituted with defaults and flagged per line. Verification is "
        "self-attested in this MVP — production requires verifier evidence.",
        (f"Declaration year {year} falls in the TRANSITIONAL period (before "
         f"{DEFINITIVE_PERIOD_START}): embedded emissions are reported but no "
         f"certificates are surrendered, so the certificate count is zero by law and "
         f"not by calculation."
         if year < DEFINITIVE_PERIOD_START else
         f"Certificates = max(0, obligation basis - free allocation) x simplified "
         f"pro-rata origin-carbon-price deduction, where free allocation = quantity x "
         f"EU benchmark x CBAM factor {cbam_factor(year)} (the share of EU free "
         f"allocation still granted in {year}; {obligation_phase_in(year) * 100:g}% of "
         f"the gap to benchmark is payable). The adjustment is SUBTRACTED, not applied "
         f"as a scaling factor — the two coincide only for an importer exactly at "
         f"benchmark, and multiplying would understate every dirtier one. The Art. 9 "
         f"origin-carbon-price deduction is applied AFTER the subtraction, which is the "
         f"conservative of the two orderings."),
        "Default values and EU benchmarks are DEMO data until the official Commission "
        "tables are loaded.",
    ]
    if agnostic_defaults:
        notes.append(
            f"{len(agnostic_defaults)} line(s) fell back to a country-agnostic default "
            f"({', '.join(sorted(set(agnostic_defaults))[:5])}) — the Commission "
            f"publishes defaults per CN code AND country of origin, so these are "
            f"approximations pending the country-specific tables.")
    if 0 < total_mass <= DE_MINIMIS_TONNES:
        notes.append(f"Total imported mass {total_mass} t is within the "
                     f"{DE_MINIMIS_TONNES} t/year de minimis threshold — this "
                     f"importer may be exempt; verify eligibility.")

    # Before the definitive period the answer is a disclosable zero, not an abstention:
    # nothing is surrendered by law, so no benchmark is needed to say so.
    certs_suppressed = (not price_ok
                        or (bool(no_benchmark) and year >= DEFINITIVE_PERIOD_START))

    total = total_direct + total_indirect
    return {
        "framework": "EU CBAM (definitive period)",
        "declaration_year": year,
        "declaration_ready": not blockers,
        "blockers": blockers,
        "lines": lines,
        "line_errors": errors,
        "totals": {
            "goods_lines": len(goods),
            "imported_mass_t": round(total_mass, 6),
            "embedded_direct_t": round(total_direct, 6),
            "embedded_indirect_t": round(total_indirect, 6),
            "embedded_total_t": round(total, 6),
            "obligation_basis_t": round(total_obligation, 6),
            "by_good_category_t": {k: round(v, 6) for k, v in by_category.items()},
            # Both are suppressed together: a free-allocation total that silently omits
            # the unbenchmarked lines sits next to a COMPLETE obligation basis, and
            # subtracting the two would look like an answer.
            "free_allocation_t": (round(free_alloc_total, 6)
                                  if not certs_suppressed else None),
            "lines_without_benchmark": len(no_benchmark),
            "certificates_due_t": None if certs_suppressed else round(certs_total, 6),
            "cbam_factor": cbam_factor(year),
            "obligation_phase_in_share": obligation_phase_in(year),
            "ets_price_eur_per_t": ets_price_eur_per_t if price_ok else None,
        },
        "notes": notes,
    }
