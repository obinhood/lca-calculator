"""Per-framework REQUIRED-DISCLOSURE registry.

For each framework this lists what the standard actually requires, with its official
reference, and classifies each requirement by who can produce it:

  "platform"  — this engine computes it. `path` is where it lives in the report payload, so
                its presence can be VERIFIED rather than asserted.
  "preparer"  — narrative, governance, policy or forward-looking content a calculation engine
                cannot produce. Required by the standard; you must write it.
  "assurance" — needs an independent third party (verification / audit opinion).

This is what makes a compliance claim checkable: the engine can prove the fields it owns are
present, and it names — rather than hides — the ones it cannot. It is NOT legal advice, and a
complete checklist is not by itself compliance; see `compliance.py` for the wording used.
"""
from typing import Dict, List, TypedDict


class Req(TypedDict, total=False):
    ref: str          # official citation
    what: str         # the requirement, in plain terms
    source: str       # platform | preparer | assurance
    path: str         # dotted payload path (platform requirements only)


def _p(ref, what, path):        return {"ref": ref, "what": what, "source": "platform", "path": path}
def _u(ref, what):              return {"ref": ref, "what": what, "source": "preparer"}
def _a(ref, what):              return {"ref": ref, "what": what, "source": "assurance"}


REQUIREMENTS: Dict[str, List[Req]] = {
    # ---------------------------------------------------------------- Corporate reporting
    # SECR = SI 2018/1155, inserting Pt. 7A into Sch. 7 of SI 2008/410. This checklist covers
    # the LARGE UNQUOTED company / LLP route (Pt. 7A), which is what app/reports/secr.py
    # implements. QUOTED companies fall under Sch. 7 Pt. 7 (paras 15-19A) and must report
    # GLOBAL energy and emissions with the UK/offshore portion identified — a different list.
    "secr": [
        _p("SI 2008/410 Sch. 7 Pt. 7A para. 20D(3)",
           "Aggregate energy consumed in kWh (non-UK may be excluded under 20D(5))",
           "energy_use_kwh.total_kwh"),
        _p("SI 2008/410 Sch. 7 Pt. 7A para. 20D(1)",
           "Emissions from gas combustion and transport fuel, tCO2e (Scope 1)",
           "emissions_tco2e.scope1"),
        _p("SI 2008/410 Sch. 7 Pt. 7A para. 20D(2)",
           "Emissions from purchased electricity, tCO2e (Scope 2)",
           "emissions_tco2e.scope2_location_based"),
        _p("SI 2008/410 Sch. 7 Pt. 7A para. 20G", "At least one intensity ratio",
           "intensity_ratio"),
        _p("SI 2008/410 Sch. 7 Pt. 7A para. 20F", "Methodologies used to calculate the figures",
           "methodology_statement"),
        _u("SI 2008/410 Sch. 7 Pt. 7A para. 20D(4)",
           "Principal energy-efficiency measures taken in the reporting year (narrative)"),
        _u("SI 2008/410 Sch. 7 Pt. 7A para. 20H",
           "Prior-year comparatives (after the first year of reporting)"),
        _u("SI 2008/410 Sch. 7 Pt. 7A para. 20D(5)-(6)",
           "Organisational boundary, any non-UK exclusion, and anything omitted with the reason why"),
    ],
    "esrs_e1": [
        _u("ESRS E1-1", "Transition plan for climate change mitigation"),
        _u("ESRS 2 SBM-3 / IRO-1", "Double-materiality assessment and material impacts"),
        _u("ESRS E1-2", "Policies related to climate change mitigation and adaptation"),
        _u("ESRS E1-3", "Actions and resources for climate policies"),
        _u("ESRS E1-4", "Targets for climate change mitigation and adaptation"),
        _p("ESRS E1-5", "Energy consumption and mix", "e1_5_energy_consumption"),
        _p("ESRS E1-6 ¶44(a)", "Gross Scope 1 GHG emissions", "e1_6_gross_ghg_emissions_tco2e.scope1"),
        _p("ESRS E1-6 ¶44(b)", "Gross Scope 2 (location- and market-based)",
           "e1_6_gross_ghg_emissions_tco2e.scope2_location_based"),
        _p("ESRS E1-6 ¶44(c)", "Gross Scope 3 GHG emissions",
           "e1_6_gross_ghg_emissions_tco2e.scope3"),
        _p("ESRS E1-6 ¶44(d)", "Total GHG emissions", "e1_6_gross_ghg_emissions_tco2e.total_location_based"),
        _p("ESRS E1-6 ¶53", "GHG intensity per net revenue", "e1_6_gross_ghg_emissions_tco2e.ghg_intensity"),
        _p("ESRS E1-6 AR 46(i)", "Scope 3 category screening across all 15 categories",
           "e1_6_scope3_screening"),
        _p("ESRS E1-7", "GHG removals and carbon credits, separate from gross",
           "e1_7_removals_and_credits"),
        _u("ESRS E1-8", "Internal carbon pricing schemes"),
        _u("ESRS E1-9", "Anticipated financial effects from physical and transition risks"),
        _u("ESRS 1 §7", "Digital tagging of the disclosure (inline XBRL, ESEF taxonomy)"),
        _a("CSRD Art. 34a", "Limited assurance opinion on the sustainability statement"),
    ],
    "issb_s2": [
        _u("IFRS S2 ¶5-7", "Governance of climate-related risks and opportunities"),
        _u("IFRS S2 ¶9-13", "Strategy: climate risks/opportunities and business-model effects"),
        _u("IFRS S2 ¶22", "Climate resilience / scenario analysis"),
        _u("IFRS S2 ¶24-26", "Risk-management processes"),
        _p("IFRS S2 ¶29(a)(i)", "Gross Scope 1 GHG emissions", "ghg_emissions_tco2e.scope1_gross"),
        _p("IFRS S2 ¶29(a)(i)", "Gross Scope 2 GHG emissions",
           "ghg_emissions_tco2e.scope2_location_based_gross"),
        _p("IFRS S2 ¶29(a)(i)", "Gross Scope 3 GHG emissions", "ghg_emissions_tco2e.scope3_gross"),
        _p("IFRS S2 ¶29(a)(ii)", "Measured per the GHG Protocol Corporate Standard",
           "methodology_statement"),
        _p("IFRS S2 ¶29(a)(iv)", "Scope 1/2 disaggregated by consolidated group vs other investees",
           "scope1_2_disaggregation_29a_iv"),
        _p("IFRS S2 ¶29(a)(v)", "Scope 2 location-based with market-based information",
           "ghg_emissions_tco2e.scope2_market_based_information"),
        _p("IFRS S2 ¶29(a)(vi)", "Scope 3 categories included",
           "ghg_emissions_tco2e.scope3_categories_included"),
        _p("IFRS S2 ¶B23", "Latest IPCC GWP values used", "ghg_emissions_tco2e.gwp_source"),
        _u("IFRS S2 ¶29(b)-(c)",
           "Assets/business activities vulnerable to transition and to physical climate risks"),
        _u("IFRS S2 ¶29(d)", "Assets/business activities aligned with climate opportunities"),
        _u("IFRS S2 ¶29(e)", "Capital deployment towards climate risks and opportunities"),
        _u("IFRS S2 ¶29(f)", "Internal carbon price, and how it is applied"),
        _u("IFRS S2 ¶29(g)", "Climate considerations in executive remuneration"),
        _u("IFRS S2 ¶32", "Industry-based metrics (SASB-derived)"),
        _u("IFRS S2 ¶33-37", "Climate-related targets and progress against them"),
    ],
    "gri": [
        _p("GRI 305-1", "Direct (Scope 1) GHG emissions", "gri_305_1_scope1.gross_tco2e"),
        _p("GRI 305-2", "Energy indirect (Scope 2) GHG emissions",
           "gri_305_2_scope2.location_based_tco2e"),
        _p("GRI 305-3", "Other indirect (Scope 3) GHG emissions", "gri_305_3_scope3.gross_tco2e"),
        _p("GRI 305-4", "GHG emissions intensity", "gri_305_4_intensity"),
        _p("GRI 305-5", "Reduction of GHG emissions vs a base year", "gri_305_5_reductions"),
        _p("GRI 302-1", "Energy consumption within the organisation", "gri_302_1_energy"),
        _p("GRI 302-3", "Energy intensity", "gri_302_3_energy_intensity"),
        _p("GRI 305-1-b", "Biogenic CO2 emissions reported separately",
           "biogenic_co2_tco2_all_scopes"),
        _p("GRI 305-1-e / 305-2-d / 305-3-e", "Source of the emission factors and GWPs used",
           "methodology"),
        _p("GRI 305-4-b", "Metrics (denominator) chosen to calculate the intensity ratio",
           "gri_305_4_intensity.denominator_unit"),
        _u("GRI 305-1-c / 305-2-e / 305-3-f", "Base year, rationale, and recalculation policy"),
        _u("GRI 305-1-f / 305-2-f", "Consolidation approach used for the inventory"),
        _u("GRI 305-3-b", "Scope 3 categories and activities included in the calculation"),
        _u("GRI 305-6", "Emissions of ozone-depleting substances (ODS) — not a GHG metric"),
        _u("GRI 305-7", "NOx, SOx and other significant air emissions"),
        _u("GRI 302-4 / 302-5", "Reduction of energy consumption and of product energy requirements"),
        _u("GRI 2-1 to 2-5", "General disclosures: entity, activities, reporting practice, assurance"),
    ],
    "cdp": [
        _p("CDP C6.1", "Scope 1 emissions", "answers.C6.1_scope1_gross_tco2e"),
        _p("CDP C6.3", "Scope 2 location-based and market-based", "answers.C6.3_scope2_location_tco2e"),
        _p("CDP C6.5", "Scope 3 emissions by category", "answers.C6.5_scope3_by_ghgp_category_tco2e"),
        _p("CDP C6.10", "GHG intensity", "answers.C6.10_intensity"),
        _p("CDP C10.1", "Verification status of reported emissions", "answers.C10.1_verification_status"),
        _u("CDP C1", "Governance: board oversight and management responsibility"),
        _u("CDP C2", "Risks and opportunities identification and assessment"),
        _u("CDP C3", "Business strategy and scenario analysis"),
        _u("CDP C4", "Targets and performance"),
        _u("CDP C12", "Value-chain engagement"),
    ],
    "sb253": [
        _p("SB 253 §38532(c)(1)", "Scope 1 emissions", "emissions_tco2e.scope1"),
        _p("SB 253 §38532(c)(1)", "Scope 2 emissions", "emissions_tco2e.scope2_location_based"),
        _p("SB 253 §38532(c)(2)", "Scope 3 emissions (from the 2027 reporting cycle)",
           "emissions_tco2e.scope3"),
        _p("SB 253 §38532(b)(2)", "Measured per the GHG Protocol Corporate Standard",
           "methodology_statement"),
        _a("SB 253 §38532(c)(3)", "Limited assurance by an independent provider (reasonable from 2030)"),
        _u("SB 253 §38532(b)(4)", "Public posting on a digital platform designated by CARB"),
    ],
    "tcfd": [
        _u("TCFD Governance (a)", "Board oversight of climate-related risks and opportunities"),
        _u("TCFD Governance (b)", "Management's role in assessing and managing them"),
        _u("TCFD Strategy (a)", "Climate risks and opportunities by time horizon"),
        _u("TCFD Strategy (b)", "Impact on business, strategy and financial planning"),
        _u("TCFD Strategy (c)", "Resilience under different climate scenarios, incl. 2°C or lower"),
        _u("TCFD Risk Mgmt (a)", "Processes for identifying and assessing climate risks"),
        _u("TCFD Risk Mgmt (b)", "Processes for managing climate risks"),
        _u("TCFD Risk Mgmt (c)", "Integration into overall risk management"),
        _u("TCFD Metrics (a)", "Metrics used to assess climate risks and opportunities"),
        _p("TCFD Metrics (b)", "Scope 1, 2 and (if appropriate) Scope 3 GHG emissions",
           "pillars.recommended_disclosures.ghg_emissions_tco2e"),
        _u("TCFD Metrics (c)", "Targets and performance against them"),
    ],

    # ------------------------------------------------------------ Inventory & assurance
    "scope3_inventory": [
        _p("GHGP Scope 3 Ch.5", "All 15 categories screened and declared",
           "scope3.completeness"),
        _p("GHGP Scope 3 Ch.6", "Emissions by category", "scope3.categories"),
        _u("GHGP Scope 3 Ch.7", "Calculation method and data sources described per category"),
        _u("GHGP Scope 3 Ch.9", "Justification for any excluded category"),
    ],
    "removals": [
        _p("GHGP LSRG Ch.6", "Removals reported separately from gross emissions",
           "inventory_removals_tco2e"),
        _u("GHGP LSRG Ch.10", "Permanence, monitoring and reversal-risk narrative"),
        _a("ISO 14064-3", "Independent verification of removal claims"),
    ],
    "neutrality": [
        _p("ISO 14068-1 §5", "Quantified GHG footprint for the subject", "gross_tco2e")
        ,_p("ISO 14068-1 §7", "Residual emissions after reductions", "residual_tco2e"),
        _p("ISO 14068-1 §7", "Residual emissions offset with RETIRED credits", "credits"),
        _u("ISO 14068-1 §6", "Carbon-reduction plan: reduce first, offset the remainder"),
        _u("ISO 14068-1 §9", "Public carbon-neutrality declaration with claim wording"),
        _a("ISO 14068-1 §10", "Independent validation/verification of the neutrality claim"),
    ],
    "assurance_readiness": [
        _p("ISO 14064-1 §9", "Inventory documented and traceable to source", "ready"),
        _u("ISAE 3410 ¶17", "Agreed level of assurance and materiality with the practitioner"),
        _a("ISAE 3410 ¶69", "Assurance report / opinion issued"),
    ],

    # -------------------------------------------------------- Compliance & carbon pricing
    "cbam": [
        _p("CBAM Reg. Art. 6(2)(a)", "Quantity of goods imported, by CN code", "lines"),
        _p("CBAM Reg. Art. 6(2)(b)", "Total embedded direct emissions", "totals"),
        _p("CBAM Reg. Art. 7", "Embedded indirect emissions (Annex II goods)", "totals"),
        _p("CBAM Reg. Art. 9", "Carbon price paid in the country of origin, deducted", "totals"),
        _u("CBAM Reg. Art. 6(2)(c)", "CBAM certificates to surrender, and the declarant's registration"),
        _a("CBAM Reg. Art. 8", "Verification of embedded emissions by an accredited verifier"),
    ],
    "ets_mrv": [
        _p("MRR Art. 24", "Annual emissions of the installation", "direct_emissions_tco2e"),
        _u("MRR Art. 12", "Approved monitoring plan and applied tier levels"),
        _u("MRR Art. 69", "Uncertainty assessment for each source stream"),
        _a("AVR Art. 27", "Verifier's opinion statement"),
        _u("ETS Dir. Art. 12", "Surrender of allowances equal to verified emissions"),
    ],
    "esos": [
        _p("ESOS reg. 26", "Total energy consumption across buildings, transport and processes",
           "total_energy_kwh"),
        _p("ESOS reg. 26", "Areas of significant energy consumption (95% of total)",
           "significant_energy_use_pct"),
        _u("ESOS reg. 28", "Energy audits covering the areas of significant consumption"),
        _u("ESOS reg. 34", "Cost-effective energy-saving recommendations"),
        _u("ESOS Phase 3", "Action plan published, and board-level director sign-off"),
        _a("ESOS reg. 29", "Review and sign-off by an approved ESOS lead assessor"),
    ],
    "eu_taxonomy": [
        _p("Disclosures DA (EU) 2021/2178 Annex II", "Turnover KPI", "turnover")
        ,_p("Disclosures DA (EU) 2021/2178 Annex II", "CapEx KPI", "capex")
        ,_p("Disclosures DA (EU) 2021/2178 Annex II", "OpEx KPI", "opex"),
        _u("Taxonomy Reg. Art. 3(a)", "Substantial-contribution assessment per activity"),
        _u("Taxonomy Reg. Art. 17", "Do No Significant Harm (DNSH) assessment"),
        _u("Taxonomy Reg. Art. 18", "Minimum safeguards (OECD/UNGP) compliance"),
        _u("Disclosures DA (EU) 2021/2178 Art. 8 / Annex II",
           "Accounting policy and the prescribed KPI reporting templates"),
    ],
    "csddd": [
        _p("CSDDD Art. 22(1)", "GHG inventory underpinning the transition plan",
           "article_22_carbon_inputs.ghg_inventory"),
        _p("CSDDD Art. 22(1)(a)", "Time-bound, 1.5°C-aligned emission reduction targets",
           "article_22_carbon_inputs.science_based_targets"),
        _u("CSDDD Art. 22(1)(b)", "Decarbonisation levers and key actions planned"),
        _u("CSDDD Art. 22(1)(c)", "Investment and funding for the transition plan"),
        _u("CSDDD Art. 22(1)(d)", "Role of administrative/management bodies in the plan"),
        _u("CSDDD Art. 5-16", "Risk-based due-diligence process over adverse impacts"),
    ],

    # ------------------------------------------------------------------------- Finance
    "pcaf": [
        _p("PCAF Part A §5.1", "Financed emissions attributed by asset class", "by_asset_class_tco2e"),
        _p("PCAF Part A §5.2", "Data-quality score (1 best – 5 proxy) disclosed", "weighted_data_quality_score"),
        _p("PCAF Part A §4", "Attribution factor per position", "positions"),
        _u("PCAF Part A §5.3", "Coverage of the portfolio and any excluded asset classes"),
        _a("PCAF / ISAE 3410", "Third-party assurance of financed-emissions figures"),
    ],
    "sfdr_pai": [
        _p("SFDR RTS Annex I, PAI 1", "GHG emissions (Scopes 1, 2, 3)", "pai_1_ghg_emissions_tco2e"),
        _p("SFDR RTS Annex I, PAI 2", "Carbon footprint", "pai_2_carbon_footprint"),
        _p("SFDR RTS Annex I, PAI 3", "GHG intensity of investee companies", "pai_3_ghg_intensity_of_investees"),
        _u("SFDR RTS Annex I Table 1",
           "The remaining mandatory adverse-impact indicators (PAI 4-14 for corporate "
           "investees, plus the sovereign and real-estate indicators where held)"),
        _u("SFDR Art. 4", "Statement on principal adverse impacts, and actions taken"),
    ],

    # --------------------------------------------------------------- Targets & projects
    "sbti": [
        _p("SBTi Criteria C1", "Base year and base-year emissions", "base"),
        _p("SBTi Criteria C5", "Target year and reduction percentage", "target"),
        _p("SBTi Criteria C7", "Scope coverage of the target", "target.scope_coverage"),
        _p("SBTi Net-Zero §4", "Ambition against the minimum annual reduction rate",
           "ambition_assessment"),
        _u("SBTi Criteria C21", "Official validation by SBTi (submitted and approved)"),
        _u("SBTi Net-Zero §7", "Annual progress disclosure against the pathway"),
    ],
    "iso_14064_2": [
        _p("ISO 14064-2 §5.4", "Baseline scenario quantification", "quantification_tco2e"),
        _p("ISO 14064-2 §5.6", "Project scenario quantification", "quantification_tco2e"),
        _p("ISO 14064-2 §5.7", "Leakage assessed and subtracted", "quantification_tco2e.leakage"),
        _p("ISO 14064-2 §5.4", "Justification of the baseline as a counterfactual",
           "project.baseline_justification"),
        _u("ISO 14064-2 §5.2", "Identification of GHG sources, sinks and reservoirs (SSRs)"),
        _u("ISO 14064-2 §5.11", "Monitoring plan and monitored data management"),
        _u("ISO 14064-2 §5.9", "Uncertainty assessment for the quantified reduction"),
        _a("ISO 14064-3", "Validation of the project plan and verification of the assertion"),
    ],

    # ---------------------------------------------------------- Products & buildings
    "lca": [
        _p("ISO 14044 §4.2", "Functional unit declared", "assessment.functional_unit"),
        _p("ISO 14044 §4.3", "Life-cycle inventory result by stage", "by_stage_kg"),
        _p("ISO 14067 §6.4.9", "Biogenic carbon reported separately", "biogenic_co2_kg_separate"),
        _u("ISO 14044 §4.2.3.3", "System boundary and cut-off criteria documented"),
        _u("ISO 14044 §4.3.4", "Allocation procedure and its justification"),
        _u("ISO 14044 §4.4", "Impact assessment beyond climate change"),
        _a("ISO 14044 §6", "Critical review (mandatory for comparative assertions disclosed publicly)"),
    ],
    "epd": [
        _p("EN 15804 §5.3", "Declared unit and reference service life", "declaration.declared_unit"),
        _p("EN 15804 §6", "GWP by information module (A1-A3, A4, A5, B, C)",
           "gwp_fossil_by_module_kg"),
        _p("EN 15804 §6.4", "Module D reported separately", "module_D_beyond_boundary_kg"),
        _p("ISO 14025 §7", "Product Category Rule (PCR) referenced", "declaration.pcr_reference"),
        _u("EN 15804+A2 §7", "The remaining core indicators (acidification, eutrophication, water, …)"),
        _u("EN 15804+A2 §7", "GWP-total, GWP-biogenic and GWP-LULUC sub-indicators"),
        _u("ISO 14025 §8", "Publication by a programme operator"),
        _a("ISO 14025 §8.1.3", "Independent third-party verification of the declaration"),
    ],
    "rics": [
        _p("RICS WLCA 2nd ed. §4", "Upfront carbon (A1-A5)", "upfront_carbon_kg"),
        _p("RICS WLCA 2nd ed. §4", "Embodied and operational carbon", "embodied_carbon_kg"),
        _p("RICS WLCA 2nd ed. §4", "Whole-life carbon, absolute and per m² GIA",
           "whole_life_carbon_kg"),
        _p("RICS WLCA 2nd ed. §5", "Module D reported separately", "module_D_beyond_boundary_kg"),
        _u("RICS WLCA 2nd ed. §3", "Scenario assumptions for the use and end-of-life stages"),
        _u("RICS WLCA 2nd ed. §6", "Benchmarking against RICS/LETI/RIBA targets"),
        _a("RICS WLCA 2nd ed. §2", "Independent review by a competent assessor"),
    ],
    "pef": [
        _p("PEF method §4", "Climate change impact category",
           "impact_categories_ef_3_1"),
        _u("PEF method §4", "The other 15 EF 3.1 impact categories"),
        _u("PEF method §6", "Normalisation and weighting to a single EF score"),
        _u("PEF method §5", "Compliance with the applicable PEFCR, incl. the Circular Footprint Formula"),
        _a("PEF method §8", "Independent verification of the PEF study"),
    ],

    # -------------------------------------------------------------------------- Nature
    "tnfd": [
        _p("TNFD LEAP", "Locate: interface with nature by site", "locate"),
        _p("TNFD LEAP", "Evaluate: dependencies and impacts", "evaluate"),
        _p("TNFD Metrics", "Core global metrics where computable", "metrics"),
        _u("TNFD Governance A/B", "Governance of nature-related dependencies and impacts"),
        _u("TNFD Strategy A-D", "Strategy, including scenario analysis"),
        _u("TNFD Risk & Impact A-C", "Risk and impact management processes"),
        _u("TNFD Strategy D", "Locations of assets/activities in sensitive areas"),
        _u("TNFD Metrics & Targets C", "Targets for nature-related dependencies and impacts"),
    ],
    "sbtn": [
        _p("SBTN Step 1", "Assess: materiality across realms", "steps"),
        _p("SBTN Step 3", "Targets by realm with validation status", "targets"),
        _u("SBTN Step 2", "Interpret and prioritise by location"),
        _u("SBTN Step 4/5", "Act and track: monitoring plan and progress"),
        _u("SBTN Validation", "Official SBTN target validation"),
    ],

    # ------------------------------------------------------------------------- Ratings
    "ecovadis": [
        _p("EcoVadis Environment", "Quantified GHG inventory as supporting evidence", "pillars"),
        _p("EcoVadis Environment", "Energy KPI and reduction vs baseline", "kpis"),
        _u("EcoVadis Policies", "Documented environmental policy and endorsements"),
        _u("EcoVadis Actions", "Measures in place (certifications, renewable procurement)"),
        _u("EcoVadis Reporting", "Published sustainability report and external verification"),
        _u("EcoVadis themes", "Labour & Human Rights, Ethics, Sustainable Procurement themes"),
    ],
}
