"""Deterministic emission-factor resolution with an explicit review gate.

Only an EXACT match (category + subcategory-as-given + geography) may bind a
factor automatically. Coarser fallbacks (category+geo, category-only, fuzzy
subcategory suggestions) are returned as SUGGESTIONS with a basis + confidence
so a human can approve or override them (Gap 6) — a plausible-but-wrong factor
silently bound is an audit finding waiting to happen.

Selection is deterministic and vintage-aware: factors that have been superseded
(their id appears in another factor's ``supersedes_id``) are never proposed, and
ties are broken by newest year then newest id — never by insertion order.
"""
from sqlalchemy.orm import Session
from rapidfuzz import process, fuzz
from typing import Optional, Tuple
from ..models import EmissionFactor, ActivityRecord

# Only proposals at or above this confidence bind without human review.
AUTO_BIND_THRESHOLD = 0.95

CONFIDENCE = {
    "exact": 1.0,
    # Same category+subcategory, but the factor is published with Global
    # geography (e.g. DEFRA flight factors): geography-agnostic by design,
    # so it binds automatically — unlike true geography fallbacks below.
    "exact_global": 0.95,
    "category_geo": 0.8,
    "fuzzy_subcategory": 0.75,
    "vintage_fallback": 0.7,
    "category_only": 0.6,
}


def _base_query(db: Session, gwp_set: Optional[str], year: Optional[int]):
    """Candidate factors: never superseded, deterministically ordered (newest first)."""
    superseded_ids = db.query(EmissionFactor.supersedes_id)\
        .filter(EmissionFactor.supersedes_id.isnot(None))
    q = db.query(EmissionFactor)\
        .filter(~EmissionFactor.id.in_(superseded_ids))\
        .order_by(EmissionFactor.year.desc(), EmissionFactor.id.desc())
    if gwp_set:
        q = q.filter(EmissionFactor.gwp_set == gwp_set)
    if year:
        q = q.filter(EmissionFactor.year == year)
    return q


def resolve_factor(db: Session, category: str, subcategory: str, geo: str,
                   gwp_set: str = "AR6", year: Optional[int] = None) -> Optional[EmissionFactor]:
    """Legacy helper: best factor by precedence, no basis information."""
    hit = propose_mapping(db, category, subcategory, None, geo, gwp_set=gwp_set, year=year)
    return hit[0] if hit else None


def _norm_text(s: Optional[str]) -> str:
    """Case/punctuation-insensitive form for fuzzy matching ('Short-Haul Economy'
    and 'short_haul_economy' must compare equal)."""
    import re
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def suggest_subcategory(db: Session, category: str, text: str) -> Optional[str]:
    subs = [r[0] for r in db.query(EmissionFactor.subcategory)
            .filter(EmissionFactor.category == category).distinct().all()]
    subs = [s for s in subs if s]
    if not subs:
        return None
    normalised = {_norm_text(s): s for s in subs}
    match = process.extractOne(_norm_text(text), list(normalised.keys()), scorer=fuzz.WRatio)
    if match and match[1] >= 75:
        return normalised[match[0]]
    return None


def _propose(db: Session, category: str, subcategory: Optional[str],
             description: Optional[str], geo: str, gwp_set: Optional[str],
             year: Optional[int]) -> Optional[Tuple[EmissionFactor, str, float]]:
    q = _base_query(db, gwp_set, year)

    # 1. Exact: category + subcategory-as-given (possibly empty) + geo.
    ef = q.filter(EmissionFactor.category == category,
                  EmissionFactor.subcategory == (subcategory or ""),
                  EmissionFactor.geography == (geo or "Global")).first()
    if ef:
        return ef, "exact", CONFIDENCE["exact"]

    # 1b. Exact category+subcategory against a Global-geography factor.
    if (geo or "Global") != "Global":
        ef = q.filter(EmissionFactor.category == category,
                      EmissionFactor.subcategory == (subcategory or ""),
                      EmissionFactor.geography == "Global").first()
        if ef:
            return ef, "exact_global", CONFIDENCE["exact_global"]

    # 2. Fuzzy subcategory (suggestion only). Uses the GIVEN subcategory text when
    #    present (catches typos/case like "Short-Haul Economy"), else free-text
    #    description — a mistyped subcategory must not fall through to a
    #    subcategory-blind pick.
    fuzz_text = (subcategory or "").strip() or (description or "")
    if fuzz_text:
        sub = suggest_subcategory(db, category, fuzz_text)
        if sub:
            for geog in ((geo or "Global"), "Global"):
                ef = q.filter(EmissionFactor.category == category,
                              EmissionFactor.subcategory == sub,
                              EmissionFactor.geography == geog).first()
                if ef:
                    return ef, "fuzzy_subcategory", CONFIDENCE["fuzzy_subcategory"]

    # 3. Category + geo, any subcategory (coarse: review required).
    ef = q.filter(EmissionFactor.category == category,
                  EmissionFactor.geography == (geo or "Global")).first()
    if ef:
        return ef, "category_geo", CONFIDENCE["category_geo"]

    # 4. Category only (coarsest: review required).
    ef = q.filter(EmissionFactor.category == category).first()
    if ef:
        return ef, "category_only", CONFIDENCE["category_only"]
    return None


def propose_mapping(db: Session, category: str, subcategory: Optional[str],
                    description: Optional[str], geo: str, gwp_set: str = "AR6",
                    year: Optional[int] = None) -> Optional[Tuple[EmissionFactor, str, float]]:
    """Best factor proposal as (factor, basis, confidence); None if nothing fits."""
    hit = _propose(db, category, subcategory, description, geo, gwp_set, year)
    if hit:
        return hit
    # Vintage fallback: nothing under the requested GWP set — propose from any
    # vintage but ALWAYS route to review (the calc engine's vintage check still
    # guards aggregate factors at run time, so this cannot silently mix sets).
    hit = _propose(db, category, subcategory, description, geo, None, year)
    if hit:
        factor, basis, _ = hit
        return factor, f"{basis}+vintage_fallback", CONFIDENCE["vintage_fallback"]
    return None


def auto_map_activity(db: Session, activity: ActivityRecord, gwp_set: str = "AR6") -> str:
    """Apply the mapping policy to one activity; returns the resulting status.

    Exact matches bind immediately ("auto"); anything coarser only fills
    ``suggested_factor_id`` and waits in the review queue ("needs_review").
    Safe to re-run on ``needs_review`` activities: the suggestion is refreshed,
    and upgrades to auto-bind if the catalog has since gained an exact match.
    """
    hit = propose_mapping(db, activity.category, activity.subcategory,
                          activity.description, activity.geo, gwp_set=gwp_set)
    if hit is None:
        activity.mapping_status = "unmapped"
        return activity.mapping_status
    factor, basis, confidence = hit
    activity.mapping_basis = basis
    activity.mapping_confidence = confidence
    if confidence >= AUTO_BIND_THRESHOLD:
        activity.factor_id = factor.id
        activity.suggested_factor_id = None
        activity.mapping_status = "auto"
    else:
        activity.suggested_factor_id = factor.id
        activity.mapping_status = "needs_review"
    return activity.mapping_status
