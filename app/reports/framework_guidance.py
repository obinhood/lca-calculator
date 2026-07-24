"""Framework / standard guidance registry.

A concise, machine-readable reference of the key guidelines for every framework,
standard and compliance regime the platform touches — both those it renders and
those it only references. Surfaced via /frameworks and attached inline to each
report payload so a user generating (say) an ESRS report also sees the ESRS
guidelines and what the platform does / does not cover.

`platform_support`: built (rendered/enforced) | partial | reference (guidance only).
This is guidance, not legal advice; verify against the current official text.
"""
from typing import Optional

FRAMEWORKS = {
    # --- Carbon accounting ---
    "ghg_protocol_corporate": {
        "name": "GHG Protocol Corporate Accounting & Reporting Standard",
        "category": "Carbon accounting", "jurisdiction": "global",
        "authority": "WRI & WBCSD", "platform_support": "built", "endpoint": None,
        "applies_to": "All organisations reporting an organisational GHG inventory.",
        "key_points": [
            "Set an organisational boundary via a consolidation approach: operational control, financial control, or equity share.",
            "Classify emissions into Scope 1 (direct), Scope 2 (purchased energy), Scope 3 (value chain).",
            "Report the seven Kyoto gases as CO2e using IPCC GWP-100; disclose the GWP vintage.",
            "Choose and disclose a base year; recalculate on significant structural change.",
            "Apply the principles: relevance, completeness, consistency, transparency, accuracy.",
        ],
    },
    "ghg_protocol_scope2": {
        "name": "GHG Protocol Scope 2 Guidance",
        "category": "Carbon accounting", "jurisdiction": "global",
        "authority": "WRI & WBCSD", "platform_support": "built",
        "endpoint": None,
        "applies_to": "Any entity reporting purchased electricity/steam/heat/cooling.",
        "key_points": [
            "DUAL report Scope 2 both location-based (grid average) AND market-based.",
            "Market-based uses contractual instruments in a hierarchy: energy attribute certificates / PPAs / supplier-specific rates, then residual mix, then grid average.",
            "Instrument MWh must not exceed metered consumption (volume matching); the remainder falls to residual mix / grid.",
            "Disclose when no residual-mix factor exists (double-counting risk).",
        ],
    },
    "ghg_protocol_scope3": {
        "name": "GHG Protocol Corporate Value Chain (Scope 3) Standard",
        "category": "Carbon accounting", "jurisdiction": "global",
        "authority": "WRI & WBCSD", "platform_support": "partial",
        "endpoint": None,
        "applies_to": "Value-chain (upstream + downstream) emissions, 15 categories.",
        "key_points": [
            "Prefer higher-quality methods: supplier-specific > hybrid > average-data (activity) > spend-based (EEIO).",
            "Disclose the calculation method and the primary-data share per category.",
            "The pending revision proposes a 95% coverage rule and mandatory data-quality tiering.",
            "Spend-based factors are per-currency at a base year/price basis — inflation-adjust and FX-convert before use.",
        ],
    },
    "ghg_protocol_product": {
        "name": "GHG Protocol Product Life Cycle Accounting & Reporting Standard",
        "category": "Product footprints", "jurisdiction": "global",
        "authority": "WRI & WBCSD", "platform_support": "reference", "endpoint": None,
        "applies_to": "Cradle-to-grave / cradle-to-gate product footprints.",
        "key_points": [
            "Define a functional unit and system boundary; document cut-off criteria.",
            "Treat biogenic and fossil carbon separately.",
        ],
    },
    # --- Organisational verification / assurance standards ---
    "iso_14064_1": {
        "name": "ISO 14064-1 (organisation GHG inventories)",
        "category": "Organisational verification", "jurisdiction": "global (ISO)",
        "authority": "ISO", "platform_support": "partial", "endpoint": "/reports/assurance_readiness",
        "applies_to": "Design/reporting of an organisation-level GHG inventory.",
        "key_points": [
            "Quantify direct emissions, indirect energy emissions, and other indirect (value chain) categories.",
            "Document methodologies, GWP source, base year, and uncertainty.",
            "Establish an inventory for verification against ISO 14064-3.",
        ],
    },
    "iso_14064_3": {
        "name": "ISO 14064-3 (validation & verification of GHG statements)",
        "category": "Assurance", "jurisdiction": "global (ISO)",
        "authority": "ISO", "platform_support": "built", "endpoint": "/assurance/engagements",
        "applies_to": "Independent verification of a GHG statement.",
        "key_points": [
            "Agree a level of assurance (limited or reasonable) and materiality.",
            "Assess completeness, accuracy, consistency, transparency and traceability to source.",
            "Issue a verification opinion; qualify it where evidence is insufficient.",
        ],
    },
    "isae_3410": {
        "name": "ISAE 3410 (assurance on GHG statements)",
        "category": "Assurance", "jurisdiction": "global (IAASB)",
        "authority": "IAASB", "platform_support": "built", "endpoint": "/assurance/engagements",
        "applies_to": "Assurance engagements on greenhouse-gas statements.",
        "key_points": [
            "Distinguish limited vs reasonable assurance in procedures and wording.",
            "Determine materiality; design procedures to detect material misstatement.",
            "Evidence must trace each reported figure to source records and factors.",
        ],
    },
    "issa_5000": {
        "name": "ISSA 5000 (general sustainability assurance)",
        "category": "Assurance", "jurisdiction": "global (IAASB)",
        "authority": "IAASB", "platform_support": "built", "endpoint": "/assurance/engagements",
        "applies_to": "Overarching sustainability-information assurance (incl. GHG).",
        "key_points": [
            "Framework-agnostic; applies to CSRD/ISSB-aligned disclosures.",
            "Escalating rigour from limited to reasonable assurance.",
        ],
    },
    # --- Product footprints / LCA ---
    "iso_14067": {
        "name": "ISO 14067 (carbon footprint of products)",
        "category": "Product footprints", "jurisdiction": "global (ISO)",
        "authority": "ISO", "platform_support": "built", "endpoint": "/lca/assessments",
        "applies_to": "Product-level carbon footprints.",
        "key_points": [
            "Report biogenic carbon separately from fossil; do not net into the fossil total.",
            "Declare functional unit, system boundary, allocation, and cut-off.",
        ],
    },
    "iso_14040_44": {
        "name": "ISO 14040 / 14044 (LCA principles & requirements)",
        "category": "Product footprints", "jurisdiction": "global (ISO)",
        "authority": "ISO", "platform_support": "built", "endpoint": "/lca/assessments",
        "applies_to": "Life-cycle assessment methodology.",
        "key_points": [
            "Four phases: goal & scope, inventory (LCI), impact assessment (LCIA), interpretation.",
            "Allocation hierarchy: avoid (subdivision/system expansion) > physical > economic.",
        ],
    },
    "iso_14025_epd": {
        "name": "ISO 14025 (Type III Environmental Product Declarations)",
        "category": "Product footprints", "jurisdiction": "global (ISO)",
        "authority": "ISO", "platform_support": "partial",
        "endpoint": "GET /reports/epd/{assessment_id}",
        "applies_to": "Third-party-verified EPDs against Product Category Rules (PCRs).",
        "key_points": [
            "The platform renders the GWP-indicator core in EN 15804 module form (A1-A3, "
            "A4, A5, B, C, D) from an en_15804 assessment, with Module D separate and "
            "biogenic CO2 separate.",
            "GWP ONLY: the other EN 15804+A2 impact categories are not computed.",
            "NOT a verified EPD — independently verify against the PCR and publish via a "
            "programme operator (ISO 14025 §8) before presenting it as one."],
    },
    "pef": {
        "name": "EU Product Environmental Footprint (PEF)",
        "category": "Product footprints", "jurisdiction": "EU",
        "authority": "European Commission", "platform_support": "reference", "endpoint": None,
        "applies_to": "EU-harmonised product environmental footprints.",
        "key_points": ["Use category rules (PEFCRs); multi-impact, not carbon-only."],
    },
    # --- Reporting ---
    "esrs_e1": {
        "name": "CSRD ESRS E1 (Climate change)",
        "category": "Reporting", "jurisdiction": "EU",
        "authority": "EFRAG / European Commission", "platform_support": "built",
        "endpoint": "/reports/esrs_e1",
        "applies_to": "Companies in scope of the CSRD (phased; see Omnibus changes).",
        "key_points": [
            "E1-6 gross Scope 1, 2 (location + market), 3 and total GHG; GHG intensity per net revenue.",
            "E1-5 energy consumption/mix; E1-7 removals & carbon credits (separate from gross); E1-4 targets; E1-1 transition plan.",
            "Double materiality; digital tagging in inline XBRL against the ESRS taxonomy.",
            "Assurance escalates from limited toward reasonable over the phase-in.",
        ],
    },
    "issb_s2": {
        "name": "ISSB IFRS S2 (Climate-related Disclosures)",
        "category": "Reporting", "jurisdiction": "global baseline",
        "authority": "ISSB (IFRS Foundation)", "platform_support": "built",
        "endpoint": "/reports/issb_s2",
        "applies_to": "Adopting jurisdictions: UK SRS, Japan SSBJ, Singapore, Hong Kong, and others.",
        "key_points": [
            "Disclose governance, strategy, risk management, and metrics & targets.",
            "Gross Scope 1/2/3 per the GHG Protocol; Scope 2 location-based with market-based information.",
            "Use the latest IPCC GWP values unless a jurisdiction requires otherwise.",
            "Industry-based (SASB-derived) metrics and scenario analysis apply.",
        ],
    },
    "issb_s1": {
        "name": "ISSB IFRS S1 (general sustainability disclosures)",
        "category": "Reporting", "jurisdiction": "global baseline",
        "authority": "ISSB", "platform_support": "reference", "endpoint": None,
        "applies_to": "General requirements accompanying S2.",
        "key_points": ["Connect sustainability disclosures to financial statements; same reporting period."],
    },
    "secr": {
        "name": "UK Streamlined Energy & Carbon Reporting (SECR)",
        "category": "Compliance", "jurisdiction": "UK",
        "authority": "UK DESNZ / Companies Act", "platform_support": "built",
        "endpoint": "/reports/secr",
        "applies_to": "Large UK companies/LLPs and quoted companies.",
        "key_points": [
            "Report UK energy use (kWh), associated Scope 1 & 2 GHG (tCO2e), and at least one intensity ratio.",
            "Include an energy-efficiency narrative; quoted companies report global Scope 1 & 2.",
            "Use the annual UK Government (DESNZ/DEFRA) conversion factors for the reporting year.",
        ],
    },
    "sb253": {
        "name": "California SB 253 (Climate Corporate Data Accountability Act)",
        "category": "Reporting", "jurisdiction": "US-California",
        "authority": "CARB", "platform_support": "built", "endpoint": "/reports/sb253",
        "applies_to": "Entities with >$1B revenue doing business in California.",
        "key_points": [
            "Report Scope 1 & 2 (phasing in Scope 3) per the GHG Protocol.",
            "Third-party assurance required: limited from the first cycle, reasonable from 2030.",
        ],
    },
    "gri": {
        "name": "GRI 305 Emissions / GRI 302 Energy",
        "category": "Reporting", "jurisdiction": "global",
        "authority": "GRI", "platform_support": "built", "endpoint": "/reports/gri",
        "applies_to": "Organisations reporting under GRI Standards.",
        "key_points": [
            "305-1/2/3 gross Scope 1/2/3 (biogenic reported separately in 305-1).",
            "305-4 intensity; 305-5 reductions vs a base; 302-1 energy consumption; 302-3 energy intensity.",
        ],
    },
    "cdp": {
        "name": "CDP Climate questionnaire",
        "category": "Reporting", "jurisdiction": "global",
        "authority": "CDP", "platform_support": "built", "endpoint": "/reports/cdp",
        "applies_to": "Companies responding to CDP (investor/customer requests).",
        "key_points": [
            "Report emissions (C6), breakdowns (C7), energy (C8), targets and verification (C10).",
            "Now ISSB/ESRS-aligned; CDP renumbered modules in the 2024 integrated questionnaire — verify current codes.",
        ],
    },
    "tcfd": {
        "name": "TCFD (Task Force on Climate-related Financial Disclosures)",
        "category": "Reporting", "jurisdiction": "global (legacy)",
        "authority": "FSB TCFD (now consolidated into ISSB)", "platform_support": "reference",
        "endpoint": "/reports/issb_s2",
        "applies_to": "Legacy framework; still referenced by SB 261, Switzerland, and others.",
        "key_points": ["Four pillars: governance, strategy, risk management, metrics & targets — now delivered via ISSB S2."],
    },
    # --- Target setting ---
    "sbti": {
        "name": "SBTi Corporate Net-Zero Standard",
        "category": "Target setting", "jurisdiction": "global",
        "authority": "Science Based Targets initiative", "platform_support": "built",
        "endpoint": "/reports/sbti",
        "applies_to": "Companies setting science-based near-term and net-zero targets.",
        "key_points": [
            "Near-term absolute contraction: minimum ~4.2%/yr linear reduction for 1.5°C (2.5%/yr for well-below-2°C).",
            "Net-zero requires ~90% absolute reduction by ~2050 with residual removals; near-term floors don't apply to the long-term target.",
            "Set a base year and cover Scope 1 & 2 (and Scope 3 where material); validate with SBTi.",
        ],
    },
    "iso_14068": {
        "name": "ISO 14068-1 (carbon neutrality) — supersedes PAS 2060",
        "category": "Carbon credits", "jurisdiction": "global (ISO)",
        "authority": "ISO", "platform_support": "built", "endpoint": "/reports/neutrality",
        "applies_to": "Entities making a carbon-neutrality claim.",
        "key_points": [
            "Follow the hierarchy: quantify, reduce first, then offset the residual.",
            "Only RETIRED credits count; prefer removals over avoidance; use credible, verified credits.",
            "EU ECGT (from Sept 2026) restricts offset-based 'carbon neutral' product claims.",
        ],
    },
    # --- Finance ---
    "pcaf": {
        "name": "PCAF (financed/facilitated/insurance-associated emissions)",
        "category": "Finance", "jurisdiction": "global",
        "authority": "PCAF", "platform_support": "built", "endpoint": "/reports/pcaf",
        "applies_to": "Financial institutions attributing portfolio emissions.",
        "key_points": [
            "Attribute investee emissions by an attribution factor per asset class.",
            "Score data quality 1 (best) to 5 (proxy); disclose the mix.",
        ],
    },
    "sfdr": {
        "name": "EU SFDR (Sustainable Finance Disclosure Regulation)",
        "category": "Finance", "jurisdiction": "EU",
        "authority": "European Commission / ESAs", "platform_support": "built", "endpoint": "/reports/sfdr_pai",
        "applies_to": "Financial market participants and products.",
        "key_points": ["Report Principal Adverse Impact indicators incl. financed GHG emissions, carbon footprint and intensity."],
    },
    "eu_taxonomy": {
        "name": "EU Taxonomy",
        "category": "Finance", "jurisdiction": "EU",
        "authority": "European Commission", "platform_support": "built", "endpoint": "/reports/eu_taxonomy",
        "applies_to": "CSRD-scope entities reporting taxonomy alignment.",
        "key_points": ["Report turnover/CapEx/OpEx alignment with climate mitigation/adaptation and Do No Significant Harm."],
    },
    "csddd": {
        "name": "EU CSDDD (Corporate Sustainability Due Diligence Directive)",
        "category": "Compliance", "jurisdiction": "EU",
        "authority": "European Commission", "platform_support": "reference", "endpoint": None,
        "applies_to": "Large companies conducting value-chain due diligence.",
        "key_points": ["Adopt and implement a climate transition plan; identify and address adverse value-chain impacts."],
    },
    # --- Compliance / carbon pricing ---
    "cbam": {
        "name": "EU CBAM (Carbon Border Adjustment Mechanism)",
        "category": "Compliance", "jurisdiction": "EU",
        "authority": "European Commission", "platform_support": "built", "endpoint": "/reports/cbam",
        "applies_to": "Importers of iron/steel, aluminium, cement, fertilisers, hydrogen, electricity.",
        "key_points": [
            "Declare embedded emissions per CN code; verified actual installation values, else default values.",
            "Certificate obligation = embedded x CBAM factor (2.5% in 2026 → 100% by 2034) x origin-carbon-price deduction.",
            "Indirect emissions enter the obligation only for Annex II goods (cement, fertilisers, electricity).",
            "Annual declaration by 31 May; ~50 t/year de minimis exemption.",
        ],
    },
    "eu_ets": {
        "name": "EU ETS (Emissions Trading System) — MRV",
        "category": "Compliance", "jurisdiction": "EU",
        "authority": "European Commission", "platform_support": "built", "endpoint": "/reports/ets_mrv",
        "applies_to": "Installations/operators under the EU ETS.",
        "key_points": ["Monitor, report and verify annual emissions under the MRR/AVR; surrender allowances."],
    },
    "uk_ets": {
        "name": "UK ETS — MRV",
        "category": "Compliance", "jurisdiction": "UK",
        "authority": "UK Government", "platform_support": "built", "endpoint": "/reports/ets_mrv",
        "applies_to": "UK ETS installations/operators.",
        "key_points": ["Monitor, report and verify annual emissions; surrender allowances."],
    },
    "esos": {
        "name": "UK ESOS (Energy Savings Opportunity Scheme)",
        "category": "Compliance", "jurisdiction": "UK",
        "authority": "UK Environment Agency", "platform_support": "built", "endpoint": "/reports/esos",
        "applies_to": "Large UK undertakings (four-yearly energy audits).",
        "key_points": ["Audit total energy consumption; identify cost-effective energy-saving measures."],
    },
    # --- Logistics ---
    "iso_14083": {
        "name": "ISO 14083 (transport chain GHG) / GLEC Framework",
        "category": "Logistics", "jurisdiction": "global",
        "authority": "ISO / Smart Freight Centre", "platform_support": "built", "endpoint": "/lca/assessments",
        "applies_to": "Transport and logistics chain emissions.",
        "key_points": ["Account emissions per transport leg (well-to-wheel); use tkm/pkm activity data."],
    },
    # --- Construction ---
    "en_15978_15804": {
        "name": "EN 15978 / EN 15804 (building & construction-product LCA), RICS Whole Life Carbon",
        "category": "Construction", "jurisdiction": "EU/UK",
        "authority": "CEN / RICS", "platform_support": "built", "endpoint": "/lca/assessments",
        "applies_to": "Whole-life carbon of buildings and construction products.",
        "key_points": ["Report by life-cycle module (A1-A5, B, C, D); EN 15804 EPDs feed EN 15978 building assessments."],
    },
    # --- Carbon credits / integrity ---
    "icvcm": {
        "name": "ICVCM Core Carbon Principles",
        "category": "Carbon credits", "jurisdiction": "global",
        "authority": "ICVCM", "platform_support": "partial", "endpoint": "/credits",
        "applies_to": "Quality benchmark for carbon credits.",
        "key_points": ["CCP-labelled credits meet threshold integrity criteria (additionality, permanence, MRV)."],
    },
    "vcmi": {
        "name": "VCMI Claims Code of Practice",
        "category": "Carbon credits", "jurisdiction": "global",
        "authority": "VCMI", "platform_support": "partial", "endpoint": "/credits",
        "applies_to": "Companies making voluntary carbon-market claims.",
        "key_points": ["Silver/Gold/Platinum claims require a validated inventory, near-term target progress, then high-quality credits."],
    },
    "verra_gold_standard": {
        "name": "Verra VCS / Gold Standard (crediting programmes)",
        "category": "Carbon credits", "jurisdiction": "global",
        "authority": "Verra / Gold Standard", "platform_support": "partial", "endpoint": "/credits",
        "applies_to": "Registries issuing verified carbon credits.",
        "key_points": ["Record registry, project id, serial, vintage; retire before claiming; avoid double counting."],
    },
    # --- Ratings & assessments (scored by a third party, not a reporting standard) ---
    "ecovadis": {
        "name": "EcoVadis (sustainability ratings)",
        "category": "Ratings & assessments", "jurisdiction": "global",
        "authority": "EcoVadis", "platform_support": "partial",
        "endpoint": "/reports/ecovadis",
        "applies_to": "Suppliers/companies rated for procurement; four themes — Environment, "
                      "Labour & Human Rights, Ethics, Sustainable Procurement.",
        "key_points": [
            "A RATINGS scheme, not a reporting standard: EcoVadis scores you (0-100) and awards a medal; you cannot self-certify.",
            "Each theme is assessed on a management-system model: Policies (commitments) -> Actions (measures taken) -> Results (reported KPIs) -> Reporting & verification.",
            "Environment evidence that moves the score: a quantified GHG inventory (Scopes 1/2/3), an energy KPI, a time-bound reduction target (SBTi validation strengthens it), demonstrated reduction vs a baseline, renewable-electricity procurement, ISO 14001, and third-party assurance.",
            "Evidence must be documented and current — assessors weight verified, published data far above self-declaration.",
            "Platform covers the CARBON/ENERGY portion of the Environment theme (evidence pack + gaps). It does NOT produce a score or medal, and does NOT cover Labour & Human Rights, Ethics, or Sustainable Procurement.",
        ],
    },
    # --- Nature (separate spatial/qualitative data model from carbon) ---
    "tnfd": {
        "name": "TNFD (Taskforce on Nature-related Financial Disclosures)",
        "category": "Nature", "jurisdiction": "global",
        "authority": "TNFD", "platform_support": "partial", "endpoint": "/reports/tnfd",
        "applies_to": "Nature-related dependencies, impacts, risks and opportunities.",
        "key_points": [
            "Follow the LEAP approach: Locate the interface with nature, Evaluate dependencies & impacts, Assess risks & opportunities, Prepare to respond and report.",
            "Report against the four pillars (governance, strategy, risk & impact management, metrics & targets) aligned to the ISSB structure.",
            "Prioritise assets/operations in sensitive locations: protected areas, Key Biodiversity Areas, and high/extreme water-stress basins.",
            "Disclose the TNFD core global metrics (land/freshwater/ocean use, water use in stressed areas, pollutants, waste).",
            "Platform covers Locate/Evaluate/Assess and the computable core metrics; governance & strategy narrative and scenario analysis are NOT produced.",
        ],
    },
    "sbtn": {
        "name": "SBTN (Science Based Targets for Nature)",
        "category": "Nature", "jurisdiction": "global",
        "authority": "SBTN", "platform_support": "partial", "endpoint": "/reports/sbtn",
        "applies_to": "Science-based targets for freshwater, land, ocean, biodiversity.",
        "key_points": [
            "Five steps: (1) Assess, (2) Interpret & prioritise, (3) Measure/set/disclose, (4) Act, (5) Track.",
            "Set targets by realm — freshwater (quantity & quality), land (footprint & ecosystem condition) first, then ocean and biodiversity.",
            "Targets require an SBTN-validated baseline; 'validated' status must be disclosed.",
            "Distinct from carbon (SBTi) targets; the platform tracks targets and validation status by realm.",
        ],
    },
}

# Report `framework` display strings -> guidance keys, for inline attachment.
_NAME_TO_KEY = [
    ("CSRD ESRS E1", "esrs_e1"),
    ("ISSB IFRS S2", "issb_s2"),
    ("UK SECR", "secr"),
    ("California SB 253", "sb253"),
    ("EU CBAM", "cbam"),
    ("GRI 305", "gri"),
    ("CDP", "cdp"),
    ("SBTi", "sbti"),
    ("ISO 14068", "iso_14068"),
    ("PCAF financed emissions", "pcaf"),
    ("SFDR Principal Adverse Impacts", "sfdr"),
    ("ISO 14067", "iso_14067"),
    ("ISO 14040", "iso_14040_44"),
    ("ISO 14083", "iso_14083"),
    ("EN 15804", "en_15978_15804"),
    ("EN 15978", "en_15978_15804"),
    ("EU Taxonomy", "eu_taxonomy"),
    ("EU ETS MRV", "eu_ets"),
    ("UK ETS MRV", "uk_ets"),
    ("UK ESOS", "esos"),
    ("TNFD", "tnfd"),
    ("SBTN", "sbtn"),
    ("EcoVadis", "ecovadis"),
]


def guidance_key_for(framework_name: str) -> Optional[str]:
    for prefix, key in _NAME_TO_KEY:
        if (framework_name or "").startswith(prefix):
            return key
    return None


def guidance_ref(framework_name: str) -> Optional[dict]:
    """Compact guidance reference to attach inline to a report payload."""
    key = guidance_key_for(framework_name)
    if key is None:
        return None
    g = FRAMEWORKS[key]
    return {"key": key, "name": g["name"], "authority": g["authority"],
            "key_points": g["key_points"], "full_guidance": f"/frameworks/{key}"}


def with_guidance(payload: dict) -> dict:
    """Attach a guidance reference to a report payload (no-op if unmapped)."""
    ref = guidance_ref(payload.get("framework", ""))
    if ref is not None:
        payload["guidance"] = ref
    return payload


def list_frameworks() -> list:
    return [{"key": k, "name": g["name"], "category": g["category"],
             "jurisdiction": g["jurisdiction"], "platform_support": g["platform_support"],
             "endpoint": g["endpoint"]} for k, g in FRAMEWORKS.items()]
