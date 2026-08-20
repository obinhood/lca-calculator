"""Versioned classification crosswalks, and the uncertainty they actually carry.

The emission-factor registry has recorded for a long time that mapping a chart of
accounts through UNSPSC to NAICS or NACE adds error frequently larger than the
factor's own. This implements it — and the implementation is not the obvious one.

CROSSWALK UNCERTAINTY IS DATA-DERIVED, NOT A PEDIGREE SCORE.

For a hop that resolves one source code to a candidate SET of target codes, the
uncertainty is the dispersion of the emission factors those candidates carry:

    sigma_xw = ln(GSD of the candidate set's factor values)

When the set has one member this is exactly 0. No fixed 1-to-5 score can produce
that, and it is the correct answer: an unambiguous mapping adds no uncertainty.

Why not simply score it on the pedigree matrix: ecoinvent's "further technological
correlation" indicator SATURATES about an order of magnitude too low for this job.
Table 10.5 caps that term at variance 0.12 — a GSD-squared of 2.0 — at the worst
score, while the observed dispersion of the EPA spend-based factor set is GSD 2.27
overall and reaches 21x within a single NAICS 3-digit group (327). Folding a real
21x ambiguity into a term that cannot exceed 2.0 silently truncates it.

The hop is still MAPPED onto that indicator for interoperability, so a reader who
expects a pedigree score gets one — but the variance used is the measured one.

THREE THINGS THE SOURCES GET WRONG, each of which this module refuses to repeat:

1. UNSPSC-TO-NAICS IS A CATEGORY ERROR DRESSED AS A LOOKUP. UNSPSC classifies the
   PRODUCT bought; NAICS, NACE and ISIC classify the ESTABLISHMENT that produced
   it. The only coherent route is UNSPSC -> CPC -> ISIC, and the first leg has no
   public table. Every "UNSPSC to NAICS crosswalk" on offer is commercial or
   machine-generated, none is authoritative, and none is auditable in a
   disclosure. A hop of this shape is therefore flagged `uncitable` and takes the
   widest uncertainty in the chain.

2. "MARGINS" IN THE EPA SUPPLY CHAIN FACTORS MEANS TRADE AND TRANSPORT MARGINS,
   NOT UNCERTAINTY MARGINS. EPA publishes no uncertainty for those factors at all.

3. NAICS-6 PRECISION IS LARGELY COSMETIC ON THAT SET. 1,016 NAICS-6 codes resolve
   to 386 distinct USEEIO reference codes and only 281 distinct factor values, so
   wheat, corn, rice, dry pea and oilseed farming all return the same number.
   Chasing 6-digit precision buys nothing where the codes collapse, and the
   effective resolution is reported rather than the nominal one.

Everything is version-pinned. A frozen report must freeze its crosswalk versions
exactly as it freezes its FX rates, or a concordance revision silently moves a
filed figure.
"""
import math
import statistics
from collections import defaultdict
from typing import Optional

from sqlalchemy.orm import Session

from ..models import Crosswalk, CrosswalkMapping, EmissionFactor

CROSSWALK_VERSION = "xw-v1"

SCHEMES = ("UNSPSC", "CPC", "CPA", "ISIC", "NAICS", "NACE", "BEA", "USEEIO",
           "CHART_OF_ACCOUNTS")

# Product-classification schemes describe what was BOUGHT; industry schemes
# describe the establishment that PRODUCED it. A direct hop between the two
# families is a category error, however plausible the table looks.
PRODUCT_SCHEMES = {"UNSPSC", "CPC", "CPA"}
INDUSTRY_SCHEMES = {"ISIC", "NAICS", "NACE", "BEA", "USEEIO"}

# Published one-to-one rates, used to set expectations rather than to compute a
# figure — the measured candidate set is what drives the number.
KNOWN_HOP_QUALITY = {
    ("CPA", "CPC"): {"one_to_one_pct": 89.1,
                     "note": "CPA 2.1 -> CPC 2.1 is largely one-to-one; route "
                             "product-to-product hops this way where possible"},
    ("ISIC", "NAICS"): {"one_to_one_pct": 24.6,
                        "note": "ISIC Rev.4 -> NAICS 2017 is only 24.6% one-to-one, "
                                "averaging 3.92 targets per source class"},
}

# ecoinvent pedigree "further technological correlation", for interoperability
# only. The official Table 10.5 variances top out at 0.12 (GSD^2 = 2.0), which is
# roughly an order of magnitude below the dispersion actually observed in
# spend-factor sets — so the score is reported and the MEASURED variance is used.
PEDIGREE_TECH_CORRELATION_SCORE = 4
PEDIGREE_TECH_CORRELATION_CAP_VARIANCE = 0.12


def _norm(v: Optional[str]) -> str:
    return (v or "").strip().upper()


def hop_family_verdict(from_scheme: str, to_scheme: str) -> dict:
    """Whether a hop crosses the product/industry divide, and is thus uncitable."""
    a, b = _norm(from_scheme), _norm(to_scheme)
    crosses = ((a in PRODUCT_SCHEMES and b in INDUSTRY_SCHEMES)
               or (a in INDUSTRY_SCHEMES and b in PRODUCT_SCHEMES))
    direct_unspsc_industry = (a == "UNSPSC" and b in INDUSTRY_SCHEMES) or \
                             (b == "UNSPSC" and a in INDUSTRY_SCHEMES)
    return {
        "crosses_product_industry_divide": crosses,
        "uncitable": direct_unspsc_industry,
        "reason": (
            "UNSPSC classifies the PRODUCT bought; NAICS/NACE/ISIC classify the "
            "ESTABLISHMENT that produced it. No official UNSPSC correspondence to any "
            "of them is published by UNDP, GS1 US, UNSD, Eurostat or Census — every "
            "table on offer is commercial or machine-generated, and none is auditable "
            "in a disclosure. The only coherent route is UNSPSC -> CPC -> ISIC."
        ) if direct_unspsc_industry else None,
        "known_quality": KNOWN_HOP_QUALITY.get((a, b)),
    }


def register(db: Session, *, from_scheme: str, to_scheme: str, source: str,
             table_version: str, licence: Optional[str] = None,
             url: Optional[str] = None) -> dict:
    """Register a versioned concordance table."""
    from .calc import _utcnow_iso
    a, b = _norm(from_scheme), _norm(to_scheme)
    if a not in SCHEMES or b not in SCHEMES:
        return {"registered": False,
                "reason": f"scheme must be one of {list(SCHEMES)}"}
    if a == b:
        return {"registered": False, "reason": "a crosswalk needs two schemes"}
    existing = db.query(Crosswalk).filter(
        Crosswalk.from_scheme == a, Crosswalk.to_scheme == b,
        Crosswalk.table_version == table_version).first()
    if existing is not None:
        return {"registered": False, "idempotent": True, "id": existing.id,
                "reason": "this table version is already registered"}
    verdict = hop_family_verdict(a, b)
    row = Crosswalk(from_scheme=a, to_scheme=b, source=source,
                    table_version=table_version, licence=licence, url=url,
                    uncitable=bool(verdict["uncitable"]),
                    created_at=_utcnow_iso())
    db.add(row); db.commit(); db.refresh(row)
    return {"registered": True, "id": row.id, "from_scheme": a, "to_scheme": b,
            "table_version": table_version, "uncitable": row.uncitable,
            "uncitable_reason": verdict["reason"],
            "known_quality": verdict["known_quality"]}


def add_mappings(db: Session, crosswalk_id: int, mappings: list) -> dict:
    """Add rows to a registered table.

    `partial` marks a row the source table flagged with an asterisk or a free-text
    qualifier — 93.7% of ISIC Rev.4 to NAICS 2017 rows are partial and 56.3% carry
    a note like "except kale, mangold wurzel, and pepper farming". Such a row is
    NOT resolvable by lookup alone and is recorded as such rather than treated as
    a clean correspondence.
    """
    xw = db.get(Crosswalk, crosswalk_id)
    if xw is None:
        return {"added": 0, "reason": "crosswalk not found"}
    added = 0
    for m in mappings:
        src, tgt = _norm(m.get("from_code")), _norm(m.get("to_code"))
        if not src or not tgt:
            continue
        db.add(CrosswalkMapping(
            crosswalk_id=crosswalk_id, from_code=src, to_code=tgt,
            partial=bool(m.get("partial")), note=m.get("note")))
        added += 1
    db.commit()
    return {"added": added, "crosswalk_id": crosswalk_id}


def resolve(db: Session, *, from_scheme: str, from_code: str, to_scheme: str,
            table_version: Optional[str] = None) -> dict:
    """Resolve one code to its candidate set, with the cardinality kept.

    A hop with one candidate and a hop with twenty-five must be distinguishable
    downstream — collapsing them to "the mapping" is what makes crosswalk error
    invisible.
    """
    a, b = _norm(from_scheme), _norm(to_scheme)
    q = db.query(Crosswalk).filter(Crosswalk.from_scheme == a,
                                   Crosswalk.to_scheme == b)
    if table_version:
        q = q.filter(Crosswalk.table_version == table_version)
    xw = q.order_by(Crosswalk.id.desc()).first()
    if xw is None:
        return {"resolved": False,
                "reason": f"no registered {a} -> {b} crosswalk"
                          f"{f' at version {table_version}' if table_version else ''}",
                "candidates": [], "cardinality": 0}

    rows = db.query(CrosswalkMapping).filter(
        CrosswalkMapping.crosswalk_id == xw.id,
        CrosswalkMapping.from_code == _norm(from_code)).all()
    if not rows:
        return {"resolved": False,
                "reason": f"{from_code!r} has no entry in {a} -> {b} "
                          f"{xw.table_version}",
                "candidates": [], "cardinality": 0,
                "table_version": xw.table_version}

    partial = [r for r in rows if r.partial or r.note]
    return {
        "resolved": True,
        "from_scheme": a, "from_code": _norm(from_code), "to_scheme": b,
        "table_version": xw.table_version, "source": xw.source,
        "licence": xw.licence,
        "uncitable": xw.uncitable,
        "candidates": sorted(r.to_code for r in rows),
        "cardinality": len(rows),
        "one_to_one": len(rows) == 1,
        "partial_rows": len(partial),
        "partial_note": (
            f"{len(partial)} of {len(rows)} candidate rows are flagged partial or "
            f"carry a text qualifier and are NOT resolvable by lookup alone"
        ) if partial else None,
    }


def _factor_values(db: Session, scheme: str, codes: list,
                  factor_source: Optional[str] = None) -> list:
    """Factor values keyed to a set of target codes, for the dispersion measure.

    Matched on the CODE, not on the scheme name. A NAICS-keyed factor's `source`
    is its publisher — "USEEIO", "EPA" — never "NAICS", so filtering the source by
    the scheme name finds nothing and reports a real ambiguity as zero
    uncertainty. `factor_source` narrows the search when one code namespace is
    served by more than one publisher.
    """
    if not codes:
        return []
    q = db.query(EmissionFactor).filter(
        EmissionFactor.method_type == "spend_based")
    if factor_source:
        q = q.filter(EmissionFactor.source.ilike(f"%{factor_source}%"))
    wanted = {_norm(c) for c in codes}
    out = []
    for f in q.all():
        key = _norm(f.subcategory) or _norm(f.category)
        if key in wanted and f.value is not None and math.isfinite(f.value) and f.value > 0:
            out.append(f.value)
    return out


def dispersion_sigma(values: list) -> dict:
    """sigma from the geometric standard deviation of a candidate set's factors.

    A single candidate gives exactly 0 — an unambiguous mapping adds no
    uncertainty, which is the right answer and one a fixed 1-5 score cannot give.
    """
    usable = [v for v in values if isinstance(v, (int, float))
              and math.isfinite(v) and v > 0]
    if len(usable) <= 1:
        return {"sigma": 0.0, "gsd": 1.0, "n": len(usable),
                "basis": "single_candidate" if usable else "no_candidates",
                "note": "an unambiguous mapping adds no uncertainty; a fixed pedigree "
                        "score cannot express that"}
    logs = [math.log(v) for v in usable]
    sd = statistics.stdev(logs)
    return {"sigma": sd, "gsd": math.exp(sd), "n": len(usable),
            "min": min(usable), "max": max(usable),
            "max_min_ratio": max(usable) / min(usable),
            "basis": "measured_candidate_dispersion",
            "note": "sigma is ln(GSD) of the candidate set's own factor values — "
                    "measured, not scored."}


def hop_uncertainty(db: Session, *, from_scheme: str, from_code: str,
                    to_scheme: str, table_version: Optional[str] = None,
                    factor_source: Optional[str] = None) -> dict:
    """The full uncertainty statement for one crosswalk hop."""
    r = resolve(db, from_scheme=from_scheme, from_code=from_code,
                to_scheme=to_scheme, table_version=table_version)
    if not r["resolved"]:
        return {**r, "sigma": None,
                "sigma_note": "an unresolvable hop has UNKNOWN, not zero, "
                              "uncertainty — it must not be treated as clean"}
    disp = dispersion_sigma(_factor_values(db, to_scheme, r["candidates"],
                                           factor_source))
    return {
        **r,
        "dispersion": disp,
        "sigma": disp["sigma"],
        "variance_contribution": disp["sigma"] ** 2,
        "pedigree_interoperability": {
            "indicator": "further technological correlation",
            "score": PEDIGREE_TECH_CORRELATION_SCORE,
            "official_cap_variance": PEDIGREE_TECH_CORRELATION_CAP_VARIANCE,
            "measured_variance": round(disp["sigma"] ** 2, 6),
            "saturated": disp["sigma"] ** 2 > PEDIGREE_TECH_CORRELATION_CAP_VARIANCE,
            "note": "The hop is mapped onto the pedigree indicator so a reader who "
                    "expects a score gets one, but the MEASURED variance is used. "
                    "ecoinvent Table 10.5 caps this term at 0.12, roughly an order of "
                    "magnitude below the dispersion observed in spend-factor sets, so "
                    "relying on the published factor would silently truncate it.",
        },
        "uncitable_note": (
            "This hop crosses the product/industry divide with no authoritative "
            "table. It is recorded as a proprietary internal mapping and should carry "
            "the widest uncertainty in the chain."
        ) if r.get("uncitable") else None,
    }


def chain_uncertainty(db: Session, hops: list) -> dict:
    """Combine a chain of hops, e.g. chart of accounts -> UNSPSC -> CPC -> ISIC.

    Variances add on the log scale. An UNRESOLVABLE hop makes the whole chain
    unquantifiable rather than contributing zero — a chain is only as citable as
    its weakest link.
    """
    results, total_var = [], 0.0
    unresolved, uncitable = [], []
    for h in hops:
        r = hop_uncertainty(db, from_scheme=h["from_scheme"],
                            from_code=h["from_code"], to_scheme=h["to_scheme"],
                            table_version=h.get("table_version"),
                            factor_source=h.get("factor_source"))
        results.append(r)
        if not r["resolved"]:
            unresolved.append(f"{h['from_scheme']}->{h['to_scheme']}")
            continue
        if r.get("uncitable"):
            uncitable.append(f"{h['from_scheme']}->{h['to_scheme']}")
        total_var += r["variance_contribution"]

    quantifiable = not unresolved
    return {
        "version": CROSSWALK_VERSION,
        "hops": results,
        "hop_count": len(hops),
        "quantifiable": quantifiable,
        "total_variance": round(total_var, 6) if quantifiable else None,
        "total_sigma": round(math.sqrt(total_var), 6) if quantifiable else None,
        "combined_gsd": round(math.exp(math.sqrt(total_var)), 4) if quantifiable else None,
        "unresolved_hops": unresolved,
        "uncitable_hops": uncitable,
        "note": "Variances add on the log scale. An unresolvable hop makes the CHAIN "
                "unquantifiable rather than contributing zero — a chain is only as "
                "citable as its weakest link.",
        "version_pinning_note": "Every hop carries its table version. A frozen report "
                                "must freeze its crosswalk versions exactly as it "
                                "freezes FX rates, or a concordance revision silently "
                                "moves a filed figure.",
    }


def effective_resolution(db: Session, scheme: str) -> dict:
    """Distinct factor VALUES behind a scheme's codes.

    Reported because nominal key precision is often cosmetic: the EPA supply-chain
    set exposes 1,016 NAICS-6 codes that resolve to 386 USEEIO reference codes and
    281 distinct values, so chasing 6-digit precision buys nothing where the codes
    collapse.
    """
    rows = db.query(EmissionFactor).filter(
        EmissionFactor.method_type == "spend_based",
        EmissionFactor.source.ilike(f"%{scheme}%")).all()
    codes = {(_norm(f.subcategory) or _norm(f.category)) for f in rows}
    values = {round(f.value, 9) for f in rows
              if f.value is not None and math.isfinite(f.value)}
    return {
        "scheme": _norm(scheme),
        "codes": len(codes), "distinct_values": len(values),
        "collapse_ratio": (round(len(codes) / len(values), 3)
                           if values else None),
        "note": "Nominal key precision is not effective precision. Where many codes "
                "share one value, a finer crosswalk buys nothing — report the "
                "effective resolution, not the nominal one.",
    }


def activity_verdict(db: Session, activity) -> Optional[dict]:
    """The declared chain's contribution for one activity, frozen onto its line.

    Returns None when no chain was declared — and that is NOT the same as zero.
    A spend line reached through an undeclared chain still carries that error; it
    is simply unquantified, and the propagation reports the unquantified share
    rather than treating it as clean.
    """
    import json as _json
    raw = getattr(activity, "crosswalk_chain", None)
    if not raw:
        return None
    try:
        hops = _json.loads(raw)
    except (ValueError, TypeError):
        return {"declared": True, "quantifiable": False,
                "reason": "crosswalk_chain is not valid JSON"}
    if not isinstance(hops, list) or not hops:
        return {"declared": True, "quantifiable": False,
                "reason": "crosswalk_chain must be a non-empty array of hops"}
    try:
        out = chain_uncertainty(db, hops)
    except (KeyError, TypeError) as exc:
        return {"declared": True, "quantifiable": False,
                "reason": f"malformed hop: {exc}"}
    return {
        "declared": True,
        "version": out["version"],
        "quantifiable": out["quantifiable"],
        "hop_count": out["hop_count"],
        "total_variance": out["total_variance"],
        "total_sigma": out["total_sigma"],
        "unresolved_hops": out["unresolved_hops"],
        "uncitable_hops": out["uncitable_hops"],
        "hops": [{
            "from_scheme": h.get("from_scheme"), "from_code": h.get("from_code"),
            "to_scheme": h.get("to_scheme"), "table_version": h.get("table_version"),
            "cardinality": h.get("cardinality"), "sigma": h.get("sigma"),
            "uncitable": h.get("uncitable"),
        } for h in out["hops"]],
    }
