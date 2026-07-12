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
    DE_MINIMIS_TONNES,
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
        if price_ok:
            line["certificates_due_t"] = round(certificates_due(
                line["obligation_basis_t"], g.carbon_price_paid_eur_per_t,
                ets_price_eur_per_t, year), 6)
            certs_total += line["certificates_due_t"]
        lines.append(line)

    if not goods and not errors:
        blockers.append(f"no CBAM goods recorded for {year}")
    if errors:
        blockers.append(f"{len(errors)} goods line(s) unresolvable "
                        f"(no emissions basis or unparseable import date)")
    if not price_ok:
        blockers.append("ets_price_eur_per_t required (finite, > 0) for the "
                        "certificate estimate")

    notes = [
        "Verified actual installation values take precedence; unverified actuals "
        "are substituted with defaults and flagged per line. Verification is "
        "self-attested in this MVP — production requires verifier evidence.",
        f"Certificate obligation = obligation basis (indirect only for Annex II "
        f"goods) x CBAM factor {cbam_factor(year)} for {year} (free-allocation "
        f"phase-in) x simplified pro-rata origin-carbon-price deduction.",
        "Default values are DEMO data until the official Commission tables are loaded.",
    ]
    if 0 < total_mass <= DE_MINIMIS_TONNES:
        notes.append(f"Total imported mass {total_mass} t is within the "
                     f"{DE_MINIMIS_TONNES} t/year de minimis threshold — this "
                     f"importer may be exempt; verify eligibility.")

    total = total_direct + total_indirect
    return {
        "framework": "EU CBAM (definitive period)",
        "declaration_year": year,
        "cbam_factor": cbam_factor(year),
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
            "certificates_due_t": round(certs_total, 6) if price_ok else None,
            "ets_price_eur_per_t": ets_price_eur_per_t if price_ok else None,
        },
        "notes": notes,
    }
