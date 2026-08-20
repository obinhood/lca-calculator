// The disclosure-framework registry, shared by the reports catalogue and the report runner.
// Each entry carries BOTH the machine bits (endpoint path + params) and the human bits
// (category, plain-English blurb, what it needs) so the catalogue can explain itself.

export type Param = {
  name: string;                 // query-param name sent to the backend
  label: string;
  def: string;                  // default value
  options?: string[];           // present -> render a <select>
  help?: string;                // shown under the field
  width?: number;
};

export type Framework = {
  key: string;
  label: string;
  full?: string;                // long/official name
  // Must be one of CATEGORIES. Reports.tsx renders cards only inside categories drawn
  // from that tuple, so a free-string category silently makes an entry UNREACHABLE in
  // the catalogue — the card never renders and no chip ever selects it. Typing it
  // against the tuple makes that a compile error instead of an invisible hole.
  category: (typeof CATEGORIES)[number];
  blurb: string;                // one line, plain English: what this report is for
  needs: "run" | "assessment" | "portfolio" | "target" | "nature" | "year";
  path: string;                 // GET endpoint (assessment-scoped: /{id} appended)
  runScoped?: boolean;          // send run_id (default true; ignored when assessmentScoped)
  assessmentScoped?: boolean;
  params: Param[];
  note?: string;                // honest-scope caveat
  // Whether the server can render this as CSV/PDF. A report with no export
  // builder must not offer the buttons — a 404 behind a download is worse than
  // an absent control.
  exportable?: boolean;
  // Whether a required-data checklist exists for this key. Without one the
  // compliance panel has nothing to say, so it is not shown.
  hasChecklist?: boolean;
};

export const CATEGORIES = [
  "Corporate reporting",
  "Inventory & assurance",
  "Compliance & carbon pricing",
  "Finance",
  "Targets & projects",
  "Products & buildings",
  "Nature",
  "Ratings",
] as const;

// What each `needs` value means, shown as a requirement chip on the card.
export const NEEDS_LABEL: Record<Framework["needs"], string> = {
  run: "Needs a calculation run",
  assessment: "Needs an LCA assessment",
  portfolio: "Needs portfolio positions",
  target: "Needs a target",
  nature: "Needs nature sites",
  year: "Needs a reporting year",
};

export const FRAMEWORKS: Framework[] = [
  // --- Corporate reporting ---
  { key: "secr", label: "UK SECR", full: "Streamlined Energy & Carbon Reporting",
    category: "Corporate reporting", needs: "run",
    blurb: "UK annual-report disclosure: energy use, Scope 1 & 2, and an intensity ratio.",
    path: "/reports/secr", params: [
      { name: "intensity_denominator", label: "Intensity denominator", def: "1.0", width: 110,
        help: "e.g. revenue in £m, or floor area" },
      { name: "intensity_denominator_unit", label: "Unit", def: "£M revenue", width: 140 } ] },
  { key: "esrs_e1", label: "CSRD ESRS E1", full: "EU Corporate Sustainability Reporting Directive — Climate",
    category: "Corporate reporting", needs: "run",
    blurb: "EU climate disclosure: gross Scopes 1/2/3, energy mix and intensity per revenue.",
    path: "/reports/esrs_e1", params: [
      { name: "net_revenue_millions", label: "Net revenue (millions)", def: "10", width: 110 },
      { name: "revenue_currency", label: "Currency", def: "EUR", width: 90 } ] },
  { key: "issb_s2", label: "ISSB IFRS S2", full: "IFRS S2 Climate-related Disclosures",
    category: "Corporate reporting", needs: "run",
    blurb: "The global baseline standard, with UK / Japan / Singapore / Hong Kong profiles.",
    path: "/reports/issb_s2", params: [
      { name: "jurisdiction", label: "Jurisdiction", def: "ISSB",
        options: ["ISSB", "UK_SRS", "JP_SSBJ", "SG_SGX", "HK_HKEX"] } ] },
  { key: "gri", label: "GRI 305 / 302", full: "GRI Emissions & Energy",
    category: "Corporate reporting", needs: "run",
    blurb: "GRI content index: emissions, energy, intensity and reductions vs a base year.",
    path: "/reports/gri", params: [
      { name: "base_run_id", label: "Base run ID (for 305-5 reductions)", def: "", width: 130,
        help: "optional — an earlier run to compare against; must share this run's period "
              + "length, GWP set, consolidation approach and entity population" },
      { name: "intensity_denominator", label: "Intensity denominator", def: "1.0", width: 110 },
      { name: "intensity_denominator_unit", label: "Unit", def: "unit", width: 110 },
      { name: "intensity_denominator_period_days", label: "Denominator period (days)",
        def: "365", width: 130,
        help: "the span the denominator covers — must match the run's reporting period, "
              + "or the intensity ratio is out by the ratio of the two spans" } ] },
  { key: "cdp", label: "CDP Climate", full: "CDP Climate Change questionnaire",
    category: "Corporate reporting", needs: "run",
    blurb: "Export shaped for the CDP questionnaire (emissions, breakdowns, energy, verification).",
    path: "/reports/cdp", params: [
      { name: "intensity_denominator", label: "Intensity denominator", def: "1.0", width: 110 },
      { name: "intensity_denominator_unit", label: "Unit", def: "unit", width: 110 },
      { name: "verification_status", label: "Verification status", def: "no_third_party_verification",
        options: ["no_third_party_verification", "third_party_limited", "third_party_reasonable"] } ] },
  { key: "sb253", label: "California SB 253", full: "Climate Corporate Data Accountability Act",
    category: "Corporate reporting", needs: "run",
    blurb: "California filing for large businesses, including the assurance statement.",
    path: "/reports/sb253", params: [
      { name: "assurance_level", label: "Assurance level", def: "limited",
        options: ["none", "limited", "reasonable"] },
      { name: "assurance_provider", label: "Assurance provider", def: "", width: 180 } ] },
  { key: "tcfd", label: "TCFD", full: "Task Force on Climate-related Financial Disclosures",
    category: "Corporate reporting", needs: "run",
    blurb: "Maps your figures onto the four TCFD pillars (legacy framework, now via ISSB S2).",
    path: "/reports/tcfd", params: [
      { name: "jurisdiction_reference", label: "Jurisdiction reference", def: "", width: 180,
        help: "e.g. California SB 261" } ],
    note: "Cross-reference map: only Metrics & Targets (b) is produced by the platform; the other ten disclosures are narrative you write." },

  // --- Inventory & assurance ---
  { key: "scope3_inventory", label: "Scope 3 inventory", full: "GHG Protocol Scope 3, 15 categories",
    category: "Inventory & assurance", needs: "run",
    blurb: "Your value-chain emissions screened across all 15 GHG Protocol categories.",
    path: "/reports/scope3_inventory", params: [] },
  { key: "removals", label: "Carbon removals", full: "GHG Protocol Land Sector & Removals",
    category: "Inventory & assurance", needs: "run",
    blurb: "Removals inside your boundary, reported separately from gross emissions.",
    path: "/reports/removals", params: [] },
  { key: "neutrality", label: "Carbon neutrality", full: "ISO 14068-1 (supersedes PAS 2060)",
    category: "Inventory & assurance", needs: "run",
    blurb: "Tests a neutrality claim: reduce first, then retire credits against the residual.",
    path: "/reports/neutrality", params: [
      { name: "basis", label: "Basis", def: "location", options: ["location", "market"] } ] },
  { key: "assurance_readiness", label: "Assurance readiness", full: "ISO 14064-1 / ISAE 3410",
    category: "Inventory & assurance", needs: "run",
    blurb: "Checks whether your inventory would survive an external auditor's review.",
    path: "/reports/assurance_readiness", params: [] },

  // --- Compliance & carbon pricing ---
  { key: "cbam", label: "EU CBAM", full: "Carbon Border Adjustment Mechanism",
    category: "Compliance & carbon pricing", needs: "year",
    blurb: "Importer declaration of embedded emissions for steel, cement, aluminium and more.",
    path: "/reports/cbam", runScoped: false, params: [
      { name: "year", label: "Reporting year", def: "2026", width: 100 },
      { name: "ets_price_eur_per_t", label: "EU ETS price (EUR/t) — placeholder",
        def: "80", width: 210 } ] },
  { key: "ets_mrv", label: "EU / UK ETS", full: "Emissions Trading System — MRV",
    category: "Compliance & carbon pricing", needs: "run",
    blurb: "Monitoring, reporting & verification for installations under an ETS.",
    path: "/reports/ets_mrv", params: [
      { name: "scheme", label: "Scheme", def: "EU ETS", options: ["EU ETS", "UK ETS"] },
      { name: "verified", label: "Verified", def: "false", options: ["false", "true"] } ] },
  { key: "esos", label: "UK ESOS", full: "Energy Savings Opportunity Scheme",
    category: "Compliance & carbon pricing", needs: "run",
    blurb: "The four-yearly UK energy audit: total consumption and savings opportunities.",
    path: "/reports/esos", params: [] },
  { key: "eu_taxonomy", label: "EU Taxonomy", full: "EU Taxonomy alignment",
    category: "Compliance & carbon pricing", needs: "year",
    blurb: "Turnover / CapEx / OpEx alignment with the climate objectives.",
    path: "/reports/eu_taxonomy", runScoped: false, params: [
      { name: "reporting_year", label: "Reporting year", def: "2025", width: 110 } ] },
  { key: "csddd", label: "EU CSDDD", full: "Corporate Sustainability Due Diligence Directive",
    category: "Compliance & carbon pricing", needs: "run",
    blurb: "Readiness of the carbon inputs to an Article 22 climate transition plan.",
    path: "/reports/csddd", params: [
      { name: "target_id", label: "Target ID", def: "", width: 110 },
      { name: "current_year", label: "Current year", def: "", width: 110 } ],
    note: "Covers the Art 22 carbon INPUTS only — not the transition plan or the due-diligence process." },

  // --- Finance ---
  { key: "pcaf", label: "PCAF financed emissions", full: "Partnership for Carbon Accounting Financials",
    category: "Finance", needs: "portfolio",
    blurb: "Attributes investee emissions to your loans and investments, with data-quality scores.",
    path: "/reports/pcaf", runScoped: false, params: [
      { name: "include_scope3", label: "Include Scope 3", def: "true", options: ["true", "false"] },
      { name: "as_of", label: "As of (ISO timestamp)", def: "", width: 200,
        help: "optional — freeze the portfolio at a point in time" } ] },
  { key: "sfdr_pai", label: "SFDR PAI", full: "Principal Adverse Impact indicators",
    category: "Finance", needs: "portfolio",
    blurb: "EU fund disclosure: financed emissions, carbon footprint and intensity.",
    path: "/reports/sfdr_pai", runScoped: false, params: [
      { name: "portfolio_value_millions", label: "Portfolio value (millions)", def: "", width: 140 },
      { name: "include_scope3", label: "Include Scope 3", def: "true", options: ["true", "false"] } ] },

  // --- Targets & projects ---
  { key: "sbti", label: "SBTi target", full: "Science Based Targets initiative",
    category: "Targets & projects", needs: "target",
    blurb: "Checks a reduction target's ambition and tracks progress against its pathway.",
    path: "/reports/sbti", runScoped: false, params: [
      { name: "target_id", label: "Target ID", def: "", width: 110 } ] },
  { key: "iso_14064_2", label: "Project reductions", full: "ISO 14064-2 project quantification",
    category: "Targets & projects", needs: "run",
    blurb: "Quantifies a project's reduction: baseline scenario − project scenario − leakage.",
    path: "/reports/iso_14064_2", runScoped: false, params: [
      { name: "baseline_run_id", label: "Baseline run ID", def: "", width: 130 },
      { name: "project_run_id", label: "Project run ID", def: "", width: 130 },
      { name: "leakage_tco2e", label: "Leakage (tCO₂e)", def: "0", width: 120 },
      { name: "baseline_justification", label: "Baseline justification", def: "", width: 320,
        help: "why this is what would have happened without the project" },
      { name: "project_name", label: "Project name", def: "", width: 160 } ],
    note: "A separate account from your corporate inventory — never add the two together." },

  // --- Products & buildings ---
  { key: "lca", label: "LCA assessment", full: "Life-cycle assessment result",
    category: "Products & buildings", needs: "assessment",
    blurb: "The computed footprint of one product or building assessment, by life-cycle stage.",
    path: "/reports/lca", assessmentScoped: true, params: [] },
  { key: "epd", label: "EPD (EN 15804)", full: "ISO 14025 Type III Environmental Product Declaration",
    category: "Products & buildings", needs: "assessment",
    blurb: "A construction product's declaration in EN 15804 module form (A1-A3, A4, B, C, D).",
    path: "/reports/epd", assessmentScoped: true, params: [
      { name: "pcr_reference", label: "PCR reference", def: "", width: 200,
        help: "the Product Category Rule this declaration follows" } ],
    note: "The quantitative core a verifier would check — not a verified EPD." },
  { key: "rics", label: "RICS whole life carbon", full: "RICS Whole Life Carbon Assessment (2nd ed.)",
    category: "Products & buildings", needs: "assessment",
    blurb: "A building's upfront, embodied, operational and whole-life carbon, per m² GIA.",
    path: "/reports/rics", assessmentScoped: true, params: [
      { name: "gia_unit", label: "GIA unit", def: "", width: 110 } ],
    note: "GWP-fossil only; not a full RICS-compliant assessment." },
  { key: "pef", label: "EU PEF", full: "Product Environmental Footprint",
    category: "Products & buildings", needs: "assessment",
    blurb: "The climate-change category of the EU's 16-impact product method.",
    path: "/reports/pef", assessmentScoped: true, params: [],
    note: "1 of 16 impact categories — not a full PEF profile." },

  // --- Nature ---
  { key: "tnfd", label: "TNFD", full: "Taskforce on Nature-related Financial Disclosures",
    category: "Nature", needs: "nature",
    blurb: "Nature-related dependencies and impacts across your sites (LEAP approach).",
    path: "/reports/tnfd", runScoped: false, params: [] },
  { key: "sbtn", label: "SBTN targets", full: "Science Based Targets for Nature",
    category: "Nature", needs: "nature",
    blurb: "Freshwater, land and ocean targets with their validation status.",
    path: "/reports/sbtn", runScoped: false, params: [] },

  // --- Ratings ---
  { key: "ecovadis", label: "EcoVadis readiness", full: "EcoVadis Environment theme (carbon/energy)",
    category: "Ratings", needs: "run",
    blurb: "The evidence pack an EcoVadis assessor looks for, plus your gaps.",
    path: "/reports/ecovadis", params: [
      { name: "baseline_run_id", label: "Baseline run ID", def: "", width: 130 },
      { name: "intensity_denominator", label: "Intensity denominator", def: "1.0", width: 110 },
      { name: "denominator_unit", label: "Unit", def: "unit", width: 100 },
      { name: "has_environmental_policy", label: "Environmental policy", def: "false",
        options: ["false", "true"] },
      { name: "iso_14001_certified", label: "ISO 14001 certified", def: "false",
        options: ["false", "true"] },
      { name: "published_sustainability_report", label: "Published report", def: "false",
        options: ["false", "true"] } ],
    note: "Covers the carbon/energy part of the Environment theme — it does not produce a score or medal." },
  // --- Added with the 2026 build-out. These are computed reports rather than
  // disclosure renderers, so none has a CSV/PDF export builder or a
  // required-data checklist, and each says so rather than offering a control
  // that 404s.
  { key: "uncertainty", label: "Uncertainty interval", full: "Monte Carlo propagation of pedigree uncertainty",
    category: "Inventory & assurance", needs: "run",
    blurb: "The 95% interval around your total, and which factors drive its width.",
    path: "/reports/uncertainty", exportable: false, hasChecklist: false,
    note: "Lines sharing an emission factor share its error, so the default assumes that rather than independence; the narrowest and widest bounds are shown alongside.",
    params: [
      { name: "correlation", label: "Correlation", def: "by_factor", width: 140,
        options: ["by_factor", "independent", "perfect"],
        help: "by_factor is the default: lines on one factor share its error" },
      { name: "iterations", label: "Iterations", def: "10000", width: 100 },
      { name: "confidence", label: "Confidence", def: "0.95", width: 90 },
      { name: "include_crosswalk", label: "Include crosswalk error", def: "false", width: 110,
        options: ["false", "true"],
        help: "adds the declared classification chain's measured contribution" } ] },
  { key: "evidence_pack", label: "Evidence pack", full: "Assurance working-paper file",
    category: "Inventory & assurance", needs: "run",
    blurb: "The twelve-section file an assuror works from, hash-stamped so the handover is provable.",
    path: "/assurance/evidence_pack", exportable: false, hasChecklist: false,
    note: "evidence_gaps names what this platform cannot supply — reviewer identity, GL coding, trial-balance reconciliation — rather than omitting it.",
    params: [
      { name: "max_lines", label: "Max transaction lines", def: "5000", width: 120,
        help: "a truncated section says how many were omitted" },
      { name: "uncertainty_iterations", label: "MC iterations", def: "10000", width: 110 } ] },
  { key: "screening", label: "Screening register", full: "Pre-calculation exception register",
    category: "Inventory & assurance", needs: "run",
    blurb: "The misstatement ledger: what was flagged, what was cleared, and the uncorrected total against materiality.",
    path: "/reports/screening", exportable: false, hasChecklist: false,
    note: "Run POST /activities/screen first to populate the register. Advisory: a run is always produced, and blockers surface here rather than suppressing the figure.",
    params: [] },
  { key: "series_screen", label: "Period-over-period", full: "Year-on-year screening over declared series",
    category: "Inventory & assurance", needs: "run", runScoped: false,
    blurb: "The GHG Protocol ch.7 ten-percent rule, as a robust band on the log ratio.",
    path: "/reports/series_screen", exportable: false, hasChecklist: false,
    note: "Covers DECLARED series only. A row with no series_key is not screened, and the unenrolled share is reported by name.",
    params: [
      { name: "current_period_id", label: "Current period id", def: "", width: 120 },
      { name: "baseline_period_id", label: "Baseline period id", def: "", width: 120 } ] },
  { key: "hourly_scope2", label: "Hourly Scope 2", full: "Temporal matching (proposed GHG Protocol revision)",
    category: "Inventory & assurance", needs: "run", runScoped: false,
    blurb: "24/7 carbon-free-energy score: certificates matched to load hour by hour.",
    path: "/reports/hourly_scope2", exportable: false, hasChecklist: false,
    note: "A PARALLEL method — the annual location and market figures are untouched. Surplus in one hour never offsets a deficit in another.",
    params: [
      { name: "reporting_period_id", label: "Reporting period id", def: "", width: 120 },
      { name: "include_hours", label: "Include hourly series", def: "false", width: 110,
        options: ["false", "true"] } ] },
  { key: "sbti_v2", label: "SBTi Net-Zero v2.0", full: "SBTi Corporate Net-Zero Standard V2.0",
    category: "Targets & projects", needs: "run",
    blurb: "Significant Scope 3 categories at 5% of categories 1-14, and company categorisation.",
    path: "/reports/sbti_v2", exportable: false, hasChecklist: false,
    note: "Effective 2027-02-01. There is no aggregate coverage floor: the old 67% rule is gone and nothing backstops it.",
    params: [
      { name: "turnover_eur", label: "Group turnover (EUR)", def: "", width: 140,
        help: "consolidated group, two-year average" },
      { name: "fte", label: "FTE", def: "", width: 90 },
      { name: "balance_sheet_eur", label: "Balance sheet (EUR)", def: "", width: 140 },
      { name: "high_income_country", label: "High-income country", def: "false", width: 110,
        options: ["false", "true"] } ] },

];

export const byCategory = (cat: string) => FRAMEWORKS.filter((f) => f.category === cat);
export const findFramework = (key: string) => FRAMEWORKS.find((f) => f.key === key);
