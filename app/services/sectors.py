"""Sector taxonomy and what a reporting entity's OWN sector may and may not do.

The distinction this module exists to enforce:

  A company's own sector ROUTES. Today it decides which Scope 3 categories a screening
  must defend with entity-specific evidence. (Framework applicability and sector-specific
  target benchmarks are NOT implemented — they are routed by size, listing status and
  jurisdiction, which this module does not model.)

  A company's own sector does NOT change a number. Emissions are activity x factor.
  Nothing here multiplies, scales, or estimates an emission figure from a sector label.

The two get conflated constantly, and the conflation is what makes sector-aware carbon
tools untrustworthy: a "manufacturing uplift" applied to a measured figure is a fabricated
number wearing a sector label. Where a sector-keyed factor IS legitimate — spend-based
EEIO — the sector that keys it is the SUPPLIER's, on the transaction, not the reporting
entity's; that lives with the factor data, not here.

`SCOPE3_RELEVANCE` encodes the GHG Protocol Scope 3 Standard's own sixth relevance
criterion ("sector guidance", Ch. 6) as a challenge, not an answer: a company in a sector
where a category is known to dominate must defend excluding it. The prior never fills a
category in, never estimates it, and never lowers a bar — it can only raise one.
"""
from typing import Optional

# Relevance levels for a Scope 3 category given the reporting entity's sector.
DOMINANT = "dominant"        # typically among the largest categories in this sector
TYPICAL = "typical"          # normally present and non-trivial
MINOR = "minor"              # usually small; no elevated challenge

# Sector taxonomy. Deliberately coarse: a long tail of narrow codes would imply a
# precision the priors do not have. `nace` / `sic` are navigational hints for a user
# mapping their own classification, not a machine mapping — nothing keys off them.
SECTORS: dict = {
    "manufacturing": dict(
        label="Manufacturing & industrial goods",
        nace="C", sic="20-39",
        note="Purchased materials and processing usually dominate."),
    "retail_consumer": dict(
        label="Retail & consumer goods",
        nace="G, C10-C15", sic="20-23, 52-59",
        note="Purchased goods for resale dominate; use-phase matters for durables, "
             "not for grocery."),
    "food_agriculture": dict(
        label="Food, beverage & agriculture",
        nace="A, C10-C11", sic="01-09, 20",
        note="Agricultural inputs dominate; land-use change is a distinct disclosure."),
    "construction_real_estate": dict(
        label="Construction & real estate",
        nace="F, L", sic="15-17, 65",
        note="Embodied materials dominate; downstream leased assets dominate for a "
             "landlord and are often nil for a contractor."),
    "transport_logistics": dict(
        label="Transport & logistics",
        nace="H", sic="40-47",
        note="Unusual among sectors: Scope 1 fuel is often the largest single source, "
             "with subcontracted haulage in Cat 4."),
    "energy_utilities": dict(
        label="Energy & utilities",
        nace="B, D", sic="10-14, 49",
        note="Scope 1 combustion dominates. Use of sold products dominates for FUEL "
             "producers; for electricity, networks and water it is normally nil."),
    "financial_services": dict(
        label="Financial services & insurance",
        nace="K", sic="60-64",
        note="Category 15 financed emissions typically exceed all others combined."),
    "professional_services": dict(
        label="Professional & business services",
        nace="M, N", sic="73, 87",
        note="Purchased services, travel and commuting dominate a small footprint."),
    "technology_software": dict(
        label="Technology & software",
        nace="J (software/IT services; hardware manufacture is C26)", sic="737",
        note="Purchased cloud and hardware dominate; use-phase matters only if you "
             "ship a product that consumes energy at the customer."),
    "healthcare_pharma": dict(
        label="Healthcare & pharmaceuticals",
        nace="Q, C21", sic="28, 80",
        note="Purchased goods and process gases; anaesthetics are a Scope 1 outlier."),
    "hospitality_leisure": dict(
        label="Hospitality, leisure & travel",
        nace="I, R", sic="58, 70, 79",
        note="Purchased goods, energy and franchise operations dominate."),
    "public_education": dict(
        label="Public sector & education",
        nace="O, P", sic="82, 91-97",
        note="Purchased goods and services plus commuting dominate."),
    "other": dict(
        label="Other / not listed",
        nace=None, sic=None,
        note="No sector prior applies; screening is judged on its own evidence."),
}

# Categories whose exclusion a company in this sector must defend, per the GHG Protocol
# Scope 3 Standard's own sector-guidance criterion. Anything unlisted is MINOR — i.e. no
# elevated challenge, NOT an endorsement of excluding it. The seven-criteria screening
# gate applies to every category regardless; these only sharpen it.
SCOPE3_RELEVANCE: dict = {
    "manufacturing":            {1: DOMINANT, 4: DOMINANT, 2: TYPICAL, 3: TYPICAL,
                                 5: TYPICAL, 9: TYPICAL, 11: TYPICAL, 12: TYPICAL},
    # Cat 11 is TYPICAL, not dominant: the bucket spans NACE G and C10-C15, and use-phase
    # emissions of sold goods are negligible for grocery and food retail.
    "retail_consumer":          {1: DOMINANT, 11: TYPICAL, 4: TYPICAL, 9: TYPICAL,
                                 12: TYPICAL, 5: TYPICAL},
    "food_agriculture":         {1: DOMINANT, 4: DOMINANT, 3: TYPICAL, 5: TYPICAL,
                                 9: TYPICAL, 12: TYPICAL},
    # Cat 13 dominates for a REIT and is often nil for a pure NACE F contractor.
    "construction_real_estate": {1: DOMINANT, 2: DOMINANT, 13: TYPICAL, 4: TYPICAL,
                                 5: TYPICAL, 11: TYPICAL},
    # NOT Cat 9: a carrier's sold product IS transport, so own fleet is Scope 1 and
    # subcontracted haulage is Cat 4 — booking it again downstream double-counts.
    "transport_logistics":      {3: DOMINANT, 4: DOMINANT, 9: TYPICAL, 1: TYPICAL,
                                 2: TYPICAL, 8: TYPICAL},
    # Cat 11 dominates for FUEL producers but not for electricity: sold power emits
    # nothing at the customer (generation is the utility's own Scope 1, and T&D losses
    # are the customer's Cat 3), and network and water operators report it nil.
    "energy_utilities":         {3: DOMINANT, 11: TYPICAL, 1: TYPICAL, 2: TYPICAL,
                                 10: TYPICAL, 15: TYPICAL},
    "financial_services":       {15: DOMINANT, 1: TYPICAL, 6: TYPICAL, 7: TYPICAL,
                                 8: TYPICAL},
    "professional_services":    {1: DOMINANT, 6: TYPICAL, 7: TYPICAL, 8: TYPICAL,
                                 2: TYPICAL},
    # Cat 11 dominates for hardware and shipped client software; for pure SaaS the
    # compute energy is already the vendor's Scope 2 / Cat 1 and Cat 11 double-counts.
    "technology_software":      {1: DOMINANT, 11: TYPICAL, 2: TYPICAL, 8: TYPICAL,
                                 6: TYPICAL, 12: TYPICAL},
    "healthcare_pharma":        {1: DOMINANT, 4: TYPICAL, 2: TYPICAL, 5: TYPICAL,
                                 6: TYPICAL, 11: TYPICAL},
    "hospitality_leisure":      {1: DOMINANT, 2: TYPICAL, 4: TYPICAL, 5: TYPICAL,
                                 6: TYPICAL, 14: TYPICAL},
    "public_education":         {1: DOMINANT, 2: TYPICAL, 5: TYPICAL, 6: TYPICAL,
                                 7: TYPICAL, 8: TYPICAL},
    "other":                    {},
}

# Every entry above must be a category the taxonomy actually has, and every sector must
# have a relevance map — a typo'd key would silently stop challenging a category.
assert set(SCOPE3_RELEVANCE) == set(SECTORS), "sector taxonomy and relevance map diverged"
assert all(1 <= c <= 15 for m in SCOPE3_RELEVANCE.values() for c in m), \
    "relevance map references a Scope 3 category outside 1-15"


def is_valid(sector: Optional[str]) -> bool:
    return sector in SECTORS


def label(sector: Optional[str]) -> Optional[str]:
    entry = SECTORS.get(sector or "")
    return entry["label"] if entry else None


def relevance(sector: Optional[str], category: int) -> str:
    """Relevance of a Scope 3 category for a sector. Unknown sector -> MINOR.

    MINOR means "this prior raises no elevated challenge", never "this is immaterial" —
    materiality is decided by the company's own screening evidence, which the seven-
    criteria gate polices for every category independently of anything here.
    """
    return SCOPE3_RELEVANCE.get(sector or "", {}).get(category, MINOR)


def dominant_categories(sector: Optional[str]) -> list:
    return sorted(c for c, r in SCOPE3_RELEVANCE.get(sector or "", {}).items()
                  if r == DOMINANT)


def sector_note(sector: Optional[str]) -> Optional[str]:
    entry = SECTORS.get(sector or "")
    return entry["note"] if entry else None


def catalogue() -> list:
    """The taxonomy as a list, for a picker. Ordered by label, `other` last."""
    rows = [dict(key=k, dominant_scope3=dominant_categories(k), **v)
            for k, v in SECTORS.items()]
    return sorted(rows, key=lambda r: (r["key"] == "other", r["label"]))
