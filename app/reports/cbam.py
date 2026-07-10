"""CBAM annual declaration renderer (definitive period).

One declaration per organisation per import year: every goods line with its
embedded direct/indirect emissions and basis (verified actuals vs defaults,
substitutions flagged), category totals, and the certificate estimate at a
caller-supplied EU ETS reference price. Fail-closed: unresolvable lines are
surfaced as errors and block declaration readiness — never silently dropped.
"""
import math
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CbamGood
from ..services.cbam import line_embedded, certificates_due, CbamResolutionError, resolve_default


def cbam_declaration(db: Session, organisation_id: int, year: int,
                     ets_price_eur_per_t: Optional[float] = None) -> dict:
    goods = db.query(CbamGood).filter(
        CbamGood.organisation_id == organisation_id,
        CbamGood.import_date >= f"{year}-01-01",
        CbamGood.import_date <= f"{year}-12-31").order_by(CbamGood.id).all()

    blockers = []
    lines = []
    errors = []
    total_direct = total_indirect = 0.0
    certs_total = 0.0
    by_category: dict = {}

    for g in goods:
        try:
            line = line_embedded(db, g)
        except CbamResolutionError as exc:
            errors.append({"good_id": g.id, "cn_code": g.cn_code, "error": str(exc)})
            continue
        default = resolve_default(db, g.cn_code, year)
        category = default.good_category if default else "unknown"
        by_category[category] = by_category.get(category, 0.0) + line["embedded_total_t"]
        total_direct += line["embedded_direct_t"]
        total_indirect += line["embedded_indirect_t"]
        if ets_price_eur_per_t:
            line["certificates_due_t"] = round(certificates_due(
                line["embedded_total_t"], g.carbon_price_paid_eur_per_t,
                ets_price_eur_per_t), 6)
            certs_total += line["certificates_due_t"]
        lines.append(line)

    if not goods:
        blockers.append(f"no CBAM goods recorded for {year}")
    if errors:
        blockers.append(f"{len(errors)} goods line(s) have no usable emissions basis")
    if ets_price_eur_per_t is None or not math.isfinite(ets_price_eur_per_t) \
            or ets_price_eur_per_t <= 0:
        blockers.append("ets_price_eur_per_t required (finite, > 0) for the "
                        "certificate estimate")

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
            "embedded_direct_t": round(total_direct, 6),
            "embedded_indirect_t": round(total_indirect, 6),
            "embedded_total_t": round(total, 6),
            "by_good_category_t": {k: round(v, 6) for k, v in by_category.items()},
            "certificates_due_t": round(certs_total, 6) if ets_price_eur_per_t else None,
            "ets_price_eur_per_t": ets_price_eur_per_t,
        },
        "notes": [
            "Verified actual installation values take precedence; unverified "
            "actuals are substituted with defaults and flagged per line.",
            "Certificate deduction for origin-country carbon price is the "
            "simplified pro-rata form, floored at zero.",
            "Default values are DEMO data until the official Commission tables "
            "are loaded.",
        ],
    }
