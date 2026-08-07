"""Applicability rules: who is legally compelled to file each framework.

Every threshold, citation and date here was researched against primary sources
(EUR-Lex, legislation.gov.uk, leginfo.legislature.ca.gov, standard-setter sites) and then
adversarially REFUTED by an independent pass instructed to find what was wrong, and then
reviewed again once encoded. Entries corrected as a result carry a CORRECTED IN REVIEW
marker; the corrections are noted inline where they change a
number. `confidence` reflects that verification, not how plausible the rule sounds.

`model_limitations` is load-bearing and must never be dropped. Several real tests cannot
be expressed by the evaluator — two-consecutive-financial-year ratchets, the
worldwide-versus-Union turnover distinction, activity gates measured in megawatts — and a
rule that silently ignores such a test returns a confident wrong answer. Stating the limit
lets the reader see exactly how far the automated verdict goes.

CSRD scope (Art. 19a(1)/29a(1)) is a SINGLE balance-sheet-date test — the
two-consecutive-year rule appears in Art. 40a and CSDDD Art. 2(5), but not here.

Values are as at August 2026. The EU Omnibus (Directive (EU) 2026/470, in force
18 March 2026) re-cut CSRD and CSDDD scope materially; pre-Omnibus thresholds are wrong.
"""
from .applicability import (ALL, ANY, ANY_2_OF_3, ENJOINED, IN_FORCE)

# --- methodology standards: applied, never filed ----------------------------------------
# These carry no threshold by construction. Inventing one would be a category error, so
# they are declared as data rather than given rule bodies.
_METHODOLOGY = {
    "ghg_protocol_corporate": "GHG Protocol Corporate Accounting & Reporting Standard",
    "ghg_protocol_scope2": "GHG Protocol Scope 2 Guidance",
    "ghg_protocol_scope3": "GHG Protocol Corporate Value Chain (Scope 3) Standard",
    "ghg_protocol_product": "GHG Protocol Product Life Cycle Standard",
    "iso_14064_1": "ISO 14064-1 (organisation GHG inventories)",
    "iso_14064_2": "ISO 14064-2 (project GHG reductions)",
    "iso_14064_3": "ISO 14064-3 (validation & verification)",
    "iso_14067": "ISO 14067 (product carbon footprint)",
    "iso_14040_44": "ISO 14040 / 14044 (LCA principles & requirements)",
    "iso_14025_epd": "ISO 14025 (Type III Environmental Product Declarations)",
    "iso_14068": "ISO 14068-1 (carbon neutrality)",
    "iso_14083": "ISO 14083 (transport chain GHG) / GLEC",
    "isae_3410": "ISAE 3410 (assurance on GHG statements)",
    "issa_5000": "ISSA 5000 (general sustainability assurance)",
    "pef": "EU Product Environmental Footprint",
    "en_15978_15804": "EN 15978 / EN 15804 (building & construction products)",
    "rics_wlc": "RICS Whole Life Carbon Assessment (2nd ed.)",
}

# --- voluntary schemes: chosen, or demanded by a counterparty ---------------------------
_VOLUNTARY = {
    "cdp": ("CDP Climate questionnaire",
            "no law compels a CDP response; a customer or investor request very often "
            "does, and that is a contractual expectation rather than a statutory duty"),
    "gri": ("GRI 305 Emissions / GRI 302 Energy",
            "a voluntary reporting standard; some jurisdictions and stock exchanges "
            "reference it in listing rules, which is where any compulsion comes from"),
    "sbti": ("SBTi Corporate Net-Zero Standard",
             "target validation is entirely voluntary, though CSRD and ISSB both require "
             "you to DISCLOSE targets you have set"),
    "pcaf": ("PCAF financed-emissions standard",
             "voluntary methodology, but it is the method regulators and the ISSB point "
             "to for Category 15, so a financial institution disclosing financed "
             "emissions is in practice expected to follow it"),
    "tnfd": ("TNFD nature-related disclosures", "voluntary framework"),
    "sbtn": ("SBTN Science Based Targets for Nature", "voluntary framework"),
    "ecovadis": ("EcoVadis sustainability rating",
                 "a commercial rating; compulsion comes from a customer's procurement "
                 "gate, never from statute"),
    "icvcm": ("ICVCM Core Carbon Principles",
              "a quality benchmark for carbon credits you choose to buy"),
    "vcmi": ("VCMI Claims Code of Practice",
             "a code governing voluntary claims about credits"),
    "verra_gold_standard": ("Verra VCS / Gold Standard",
                            "crediting programmes joined voluntarily by project "
                            "developers"),
    "issb_s1": ("ISSB IFRS S1 (general sustainability disclosures)",
                "IFRS S1/S2 bind only where a jurisdiction has adopted them, and "
                "adoption is asymmetric — Australia mandates S2 only, Canada's CSDS 1 is "
                "voluntary, the UK proposal applies the climate-relevant S1 provisions "
                "with broader S1 comply-or-explain. Treated as voluntary here because "
                "the platform does not model per-jurisdiction adoption"),
}

RULES: dict = {}

for _k, _label in _METHODOLOGY.items():
    RULES[_k] = {"kind": "methodology", "label": _label, "confidence": "high"}

for _k, (_label, _note) in _VOLUNTARY.items():
    RULES[_k] = {"kind": "voluntary", "label": _label, "voluntary_note": _note,
                 "confidence": "high"}


RULES["tcfd"] = {
    "kind": "methodology",
    "label": "TCFD recommendations (disbanded 2023; monitoring folded into the ISSB)",
    "confidence": "high",
    "model_limitations": "The TCFD RECOMMENDATIONS are a framework you apply, but "
                         "TCFD-ALIGNED DISCLOSURE is separately compelled by law in "
                         "several jurisdictions — in the UK by SI 2022/31 and SI 2022/46. "
                         "See the 'uk_climate_disclosure' entry, which carries that duty.",
}

RULES["uk_climate_disclosure"] = {
    "kind": "conditional",
    "label": "UK climate-related financial disclosure (TCFD-aligned)",
    "jurisdiction": "UK",
    "applies_in": ["UK"],
    "summary": "UK companies and LLPs above 500 employees — and, if private, also above "
               "GBP 500m turnover — must make TCFD-aligned climate disclosures.",
    "citation": "The Companies (Strategic Report) (Climate-related Financial Disclosure) "
                "Regulations 2022 (SI 2022/31) and the Limited Liability Partnerships "
                "(Climate-related Financial Disclosure) Regulations 2022 (SI 2022/46)",
    "activity_test": "which limb catches you depends on entity type, not size alone: "
                     "public-interest entities and AIM companies above 500 employees are "
                     "caught on headcount, while private companies need above 500 "
                     "employees AND above GBP 500m turnover. Because entity type decides "
                     "which test applies, this is reported as activity-based rather than "
                     "resolved from your size.",
    "first_reporting_year": 2022,
    "phase_in": "Financial years starting on or after 6 April 2022.",
    "status": IN_FORCE,
    "confidence": "medium",
    "model_limitations": "Public-interest-entity and AIM status are not modelled, so the "
                         "applicable limb cannot be selected automatically.",
}


# --- CSRD / ESRS E1 ---------------------------------------------------------------------
# CORRECTED IN REVIEW: the original research said first_reporting_year 2024 and an
# any-2-of-3 test. Both wrong post-Omnibus. Directive (EU) 2026/470 Art. 2(4)(a) replaces
# Art. 19a(1) with a CONJUNCTIVE test — turnover AND employees, no balance-sheet limb —
# and no entity has ever owed a report for FY2024 on that basis. FY2024 belonged to a
# different, legacy criterion, modelled separately below.
RULES["esrs_e1"] = {
    "kind": "mandatory",
    "label": "CSRD — ESRS E1 (Climate change)",
    "jurisdiction": "EU",
    "applies_in": ["EU"],
    "summary": "EU undertakings exceeding EUR 450m net turnover AND 1,000 average "
               "employees must report under the ESRS in their management report.",
    "citation": "Directive 2013/34/EU Art. 19a(1) (and Art. 29a(1) consolidated) as "
                "replaced by Directive (EU) 2026/470 Art. 2(4)(a) and Art. 2(5)(a); "
                "application dates in Directive (EU) 2022/2464 Art. 5(2) as amended",
    "size_tests": [
        {"metric": "annual_turnover", "operator": ">", "value": 450_000_000, "unit": "EUR",
         "note": "net turnover; 'exceed' is strictly greater than"},
        {"metric": "employees", "operator": ">", "value": 1000, "unit": "headcount",
         "note": "average number of employees during the financial year"},
    ],
    "size_logic": ALL,          # conjunctive — NOT the any-2-of-3 company-size test
    "first_reporting_year": 2027,
    "phase_in": "FY2027 for the 450m/1,000 test (first reports published 2028) — that "
                "date was set by the 'stop-the-clock' Directive (EU) 2025/794, which the "
                "Omnibus then left in place while replacing the scope criteria. "
                "Transposition 19 March 2027. The reasonable-assurance step was removed; "
                "1 July 2027 is the Commission's deadline to ADOPT limited-assurance "
                "standards (Dir. 2006/43/EC Art. 26a(3)), not an application date.",
    "status": IN_FORCE,
    "confidence": "high",
    "model_limitations": "Third-country groups are caught by a separate Art. 40a test "
                         "(EUR 450m net turnover IN THE UNION for two consecutive years, "
                         "plus a EUR 200m EU subsidiary or branch) that this rule does "
                         "not evaluate. Member States may exempt some legacy filers for "
                         "FY2025-2026.",
}

RULES["esrs_e1_legacy_pie"] = {
    # CONDITIONAL, not mandatory: public-interest-entity status is a NECESSARY statutory
    # condition and is not an entity-size field, so a "required" here would tell a
    # 600-employee non-PIE it must file when it need not.
    "kind": "conditional",
    "label": "CSRD — ESRS E1, legacy large-PIE cohort (FY2024-2026)",
    "jurisdiction": "EU",
    "applies_in": ["EU"],
    "summary": "Large public-interest entities above 500 average employees were in scope "
               "for financial years starting 2024-2026, before the Omnibus test applies.",
    "activity_test": "you were in this cohort only if you are a PUBLIC-INTEREST ENTITY "
                     "(listed, a credit institution or an insurer) above 500 average "
                     "employees. Member States may exempt entities in this cohort that "
                     "do not exceed EUR 450m turnover or 1,000 employees for financial "
                     "years starting 2025-2026, so FY2024 may be the last mandatory year.",
    "citation": "Directive (EU) 2022/2464 Art. 5(2) first subparagraph point (a), as "
                "amended by Directive (EU) 2026/470 Art. 3(1)",
    "size_tests": [
        {"metric": "employees", "operator": ">", "value": 500, "unit": "headcount",
         "note": "average number of employees; ALSO requires public-interest-entity "
                 "status, which this platform does not model"},
    ],
    "size_logic": ALL,
    "first_reporting_year": 2024,
    "phase_in": "Financial years starting 1 Jan 2024 to 31 Dec 2026 only. Member States "
                "may exempt entities in this cohort that do not exceed EUR 450m turnover "
                "or 1,000 employees for FYs starting 2025-2026, so FY2024 may be the "
                "last mandatory year for many.",
    "status": IN_FORCE,
    "confidence": "medium",
    "model_limitations": "Public-interest-entity status (listed, credit institution or "
                         "insurer) is a necessary condition this rule cannot test, so a "
                         "'required' here means 'required IF you are a PIE'.",
}

# --- CSDDD ------------------------------------------------------------------------------
# SURVIVED REFUTATION. Omnibus raised the thresholds from 450m/1,000 to 1.5bn/5,000.
RULES["csddd"] = {
    "kind": "mandatory",
    "label": "EU CSDDD (Corporate Sustainability Due Diligence)",
    "jurisdiction": "EU",
    "applies_in": ["EU"],
    "summary": "EU companies with more than 5,000 employees AND more than EUR 1.5bn net "
               "worldwide turnover must run risk-based human-rights and environmental "
               "due diligence across their chains of activities.",
    "citation": "Directive (EU) 2024/1760 Art. 2(1)(a) as replaced by Directive (EU) "
                "2026/470 Art. 4(2)(a)(i); application dates in Art. 37(1) as replaced "
                "by Art. 4(22)",
    "size_tests": [
        {"metric": "employees", "operator": ">", "value": 5000, "unit": "headcount",
         "note": "average; part-timers on an FTE basis, agency workers included "
                 "(Art. 2(4)). Raised from 1,000 by the Omnibus."},
        {"metric": "annual_turnover", "operator": ">", "value": 1_500_000_000,
         "unit": "EUR", "note": "net WORLDWIDE turnover. Raised from EUR 450m."},
    ],
    "size_logic": ALL,
    "first_reporting_year": 2030,
    "phase_in": "Substantive due-diligence obligations from 26 July 2029; the Art. 16 "
                "statement is required for financial years starting on or after "
                "1 January 2030, so the first statement is published in 2031.",
    "status": IN_FORCE,
    "confidence": "high",
    "model_limitations": "Three tests are not evaluated here: (a) Art. 2(5) requires the "
                         "conditions to be met in TWO CONSECUTIVE financial years before "
                         "the directive applies — this rule tests a single year, so it "
                         "can flag an entity a year early; (b) the third-country route "
                         "(Art. 2(2)) tests EUR 1.5bn of UNION turnover in the year "
                         "preceding the last, which is a different metric and period "
                         "from the worldwide test above; (c) the franchising route "
                         "(Art. 2(1)(c): Union royalties over EUR 75m together with "
                         "worldwide turnover over EUR 275m) is an alternative way in "
                         "that this rule does not test.",
}

# --- SECR -------------------------------------------------------------------------------
RULES["secr"] = {
    "kind": "mandatory",
    "label": "UK Streamlined Energy & Carbon Reporting (SECR)",
    "jurisdiction": "UK",
    "applies_in": ["UK"],
    "summary": "Quoted companies of any size, and large unquoted companies and LLPs, must "
               "report energy use and greenhouse gas emissions in the directors' report.",
    "citation": "SI 2008/410 Sch. 7 Pt. 7 (quoted companies, inserted by SI 2013/1970) "
                "and Pt. 7A (large unquoted companies, inserted by SI 2018/1155)",
    "size_tests": [
        # Sch. 7 paras. 20B(2)/20C(2) state these as EXEMPTION conditions ("not more
        # than 250"), so being in scope needs strictly MORE — 250 employees exactly is
        # exempt. ESOS uses the same numeral with the opposite operator ("at least 250").
        {"metric": "employees", "operator": ">", "value": 250, "unit": "headcount",
         "note": "para. 20B(2)/20C(2) exempts 'not more than 250' employees, so 250 "
                 "exactly is OUT of scope"},
        {"metric": "annual_turnover", "operator": ">", "value": 36_000_000, "unit": "GBP",
         "note": "turnover limit hard-coded in Sch. 7 para. 20B(2)/20C(2) — not a "
                 "cross-reference to the Companies Act size limits"},
        {"metric": "balance_sheet_total", "operator": ">", "value": 18_000_000,
         "unit": "GBP", "note": "balance-sheet-total limit in Sch. 7 para. 20B(2)/20C(2)"},
    ],
    "size_logic": ANY_2_OF_3,
    # Companies Act 2006 s.385(2) verbatim: the UK official list, official listing in an
    # EEA State, or admission to dealing on the NYSE or Nasdaq. AIM is deliberately absent
    # — an AIM company is not "quoted" for Companies Act purposes.
    "listed_regardless_of_size": ["UK-OFFICIAL", "EEA-OFFICIAL", "NYSE", "NASDAQ"],
    "first_reporting_year": 2013,
    "phase_in": "Quoted-company GHG reporting from financial years ending on or after "
                "30 September 2013; extended to energy and to large unquoted companies "
                "for financial years starting on or after 1 April 2019.",
    "status": IN_FORCE,
    "confidence": "high",
    "model_limitations": "Two tests are not evaluated: (a) a low-energy user (40,000 kWh "
                         "or less over the period) may state that instead of reporting; "
                         "(b) Sch. 7 paras. 20B(1)(b)/20C(1)(b) carry status forward from "
                         "the preceding year, so this single-year test can flag an "
                         "undertaking a year early. Note the SECR limits are hard-coded "
                         "in Sch. 7 and were NOT changed by SI 2024/1303, which raised "
                         "the Companies Act size limits only — the two regimes have "
                         "diverged.",
}

# --- ESOS -------------------------------------------------------------------------------
# CORRECTED IN REVIEW: the test is NESTED — headcount alone qualifies, or BOTH financial
# limbs together. It is emphatically not any-2-of-3, which is a different regime's shape.
RULES["esos"] = {
    "kind": "mandatory",
    "label": "UK ESOS (Energy Savings Opportunity Scheme)",
    "jurisdiction": "UK",
    "applies_in": ["UK"],
    "summary": "UK undertakings employing at least 250 people, or exceeding both the "
               "turnover and balance-sheet limits, must carry out ESOS energy audits.",
    "citation": "SI 2014/1643 reg. 15 (relevant undertakings) and reg. 16 (excluded "
                "undertakings) read with Sch. 1 (large undertaking; amounts A and B; "
                "para. 11 two-period status rule) and reg. 4 (compliance periods)",
    # employees >= 250  OR  (turnover > GBP 44m  AND  balance sheet > GBP 38m)
    "size_expr": {
        "op": ANY,
        "of": [
            {"metric": "employees", "operator": ">=", "value": 250, "unit": "headcount",
             "note": "'employs at least 250 persons' — a standalone limb. Monthly "
                     "headcount averaged over the accounting period, including "
                     "owner-managers and partners."},
            {"op": ALL, "of": [
                {"metric": "annual_turnover", "operator": ">", "value": 44_000_000,
                 "unit": "GBP", "note": "'amount A'; strictly exceeding. Bites only "
                                        "together with the balance-sheet limb."},
                {"metric": "balance_sheet_total", "operator": ">", "value": 38_000_000,
                 "unit": "GBP", "note": "'amount B'; strictly exceeding. Neither "
                                        "financial limb qualifies an undertaking alone."},
            ]},
        ],
    },
    "first_reporting_year": 2015,
    "phase_in": "Phase 3 compliance date 5 June 2024; Phase 4 qualification date "
                "31 December 2026, compliance date 5 December 2027.",
    "status": IN_FORCE,
    "confidence": "high",
    "model_limitations": "Two tests are not evaluated: (a) Sch. 1 para. 11 makes status a "
                         "TWO-CONSECUTIVE-ACCOUNTING-PERIOD ratchet — an undertaking "
                         "keeps large status until it falls below for two periods running "
                         "— whereas this rule reads a single period; (b) reg. 16 excludes "
                         "public bodies and undertakings in an insolvency procedure "
                         "entirely. A group SME is also caught by reg. 15(1)(b) if any "
                         "group undertaking qualifies, which this rule cannot see. "
                         "SI 2026/701 (in force 22 July 2026) rewrote reg. 16, adding a "
                         "limb for a group SME whose large group undertaking is "
                         "insolvent.",
}

# --- California SB 253 / SB 261 ---------------------------------------------------------
RULES["sb253"] = {
    "kind": "mandatory",
    # Naming only "US" cannot rule this out — an entity that does business nationally may
    # or may not do business in California, and that is a question, not an exemption.
    "territory_parent": "US",
    "label": "California SB 253 (Climate Corporate Data Accountability Act)",
    "jurisdiction": "US-CA",
    "applies_in": ["US-CA"],
    "summary": "Entities doing business in California with total annual revenues over "
               "USD 1bn must report Scope 1 and 2 (and later Scope 3) emissions.",
    "citation": "California Health & Safety Code § 38532(b)(2) and (c)(1); penalty cap "
                "at § 38532(f)(2)(A). Neither § 38532 nor § 38533 cross-references the "
                "Revenue & Taxation Code — CARB's rulemaking glosses 'doing business in "
                "California' by reference to Rev. & Tax. Code § 23101(a) with (b)(1)-(2), "
                "excluding the (b)(3)-(4) property and payroll prongs.",
    "size_tests": [
        {"metric": "annual_turnover", "operator": ">", "value": 1_000_000_000,
         "unit": "USD",
         "note": "TOTAL ANNUAL REVENUE — global, gross, not California-only and not net. "
                 "CARB measures it as the lesser of the two previous fiscal years."},
    ],
    "size_logic": ALL,
    "first_reporting_year": 2026,
    "phase_in": "Scope 1/2 from 2026, Scope 3 from 2027. The ADOPTED regulation's "
                "first-year Scope 1/2 deadline is 10 AUGUST 2026. CARB announced an "
                "intent to defer it to 10 November 2026, but that package was still in "
                "comment and awaiting OAL approval — treat 10 August as operative and "
                "10 November as proposed until the amendment is approved. Limited "
                "assurance on Scope 1/2 from 2026 and reasonable from 2030; Scope 3 "
                "limited assurance from 2030.",
    "status": IN_FORCE,
    "status_note": "Not enjoined — the Ninth Circuit's November 2025 order granted an "
                   "injunction pending appeal as to SB 261 only and denied it as to "
                   "SB 253. No merits opinion had issued as of August 2026.",
    "confidence": "high",
    "model_limitations": "'Doing business in California' is a legal test on nexus, "
                         "sales, property and payroll that this rule approximates by "
                         "asking whether you operate in California at all.",
}

RULES["sb261"] = {
    "kind": "mandatory",
    # Naming only "US" cannot rule this out — an entity that does business nationally may
    # or may not do business in California, and that is a question, not an exemption.
    "territory_parent": "US",
    "label": "California SB 261 (climate-related financial risk)",
    "jurisdiction": "US-CA",
    "applies_in": ["US-CA"],
    "summary": "Entities doing business in California with total annual revenues over "
               "USD 500m must publish a climate-related financial risk report.",
    "citation": "California Health & Safety Code § 38533",
    "size_tests": [
        {"metric": "annual_turnover", "operator": ">", "value": 500_000_000,
         "unit": "USD", "note": "total annual revenue, global and gross"},
    ],
    "size_logic": ALL,
    "first_reporting_year": 2026,
    # The lesson that made `status` a first-class field: a statutory date read on its own
    # would present this as a live duty when a court has suspended it.
    "status": ENJOINED,
    "status_note": "The Ninth Circuit granted an injunction pending appeal in November "
                   "2025, so the provision is NOT presently enforceable. CARB opened a "
                   "voluntary-submission docket. Scope is stated as-if-in-force because "
                   "an injunction can be lifted on appeal.",
    "confidence": "medium",
    "model_limitations": "Two tests are not evaluated: (a) 'doing business in California' "
                         "is a legal test on nexus, sales, property and payroll, which "
                         "this rule approximates by asking whether you operate there at "
                         "all; (b) § 38533 carves out entities regulated by the "
                         "California Department of Insurance.",
}

# --- EU Taxonomy ------------------------------------------------------------------------
# CORRECTED IN REVIEW: conditional, not mandatory — the duty attaches to being an
# undertaking already subject to the sustainability-reporting requirement, not to size.
RULES["eu_taxonomy"] = {
    "kind": "conditional",
    "label": "EU Taxonomy (Art. 8 disclosures)",
    "jurisdiction": "EU",
    "applies_in": ["EU"],
    "summary": "Undertakings already required to publish sustainability information must "
               "disclose the taxonomy-aligned share of turnover, CapEx and OpEx.",
    "citation": "Regulation (EU) 2020/852 Art. 8(1)-(2) (dates in Art. 27(2)); content "
                "specified by Commission Delegated Regulation (EU) 2021/2178",
    "activity_test": "attaches to any undertaking that is subject to the CSRD "
                     "sustainability-reporting requirement — so your Taxonomy duty "
                     "follows your CSRD status rather than a threshold of its own",
    "first_reporting_year": 2021,
    "status": IN_FORCE,
    "confidence": "high",
    "model_limitations": "Because the duty follows CSRD status rather than a threshold of "
                         "its own, your Taxonomy position is exactly as certain as your "
                         "CSRD position — resolve that entry first. Art. 8(2) is limited "
                         "to non-financial undertakings.",
}

# --- SFDR -------------------------------------------------------------------------------
# CORRECTED IN REVIEW: the original claim said "no size test". Art. 17(1) excludes small
# advisers entirely, and Art. 4(3) is a stringency escalator rather than an entry gate.
RULES["sfdr"] = {
    "kind": "conditional",
    "label": "EU SFDR (Sustainable Finance Disclosure Regulation)",
    "jurisdiction": "EU",
    "applies_in": ["EU"],
    "summary": "Applies by ENTITY TYPE — financial market participants and financial "
               "advisers — not by size; above 500 employees the principal-adverse-impact "
               "statement becomes mandatory rather than comply-or-explain.",
    "citation": "Regulation (EU) 2019/2088 Art. 2(1) and 2(11) (entity types), Art. 17(1) "
                "(exclusion below three persons, with the Member State opt-in at "
                "Art. 17(2) and notification at 17(3)), Art. 4(3)-(4) (500-employee PAI "
                "trigger); consolidated text 02019R2088-20260702",
    "activity_test": "you are in scope if you are a financial market participant "
                     "(Art. 2(1)(a)-(j)) or a financial adviser (Art. 2(11)(a)-(f)). "
                     "Art. 17(1) excludes insurance intermediaries and investment firms "
                     "employing fewer than three persons unless the Member State has "
                     "opted them in. Above 500 average employees the entity-level PAI "
                     "statement becomes mandatory (Art. 4(3)).",
    "first_reporting_year": 2021,
    "status": IN_FORCE,
    "confidence": "high",
    "model_limitations": "Entity type is not modelled, so this cannot tell you whether "
                         "you are a financial market participant — only that size alone "
                         "does not decide it.",
}

# --- CBAM -------------------------------------------------------------------------------
# CORRECTED IN REVIEW: the original citation ('Art. 2(3a)') was misnumbered. The de
# minimis lives in Art. 2a inserted by Reg (EU) 2025/2083 Art. 1(2), with Annex VII
# point 1 setting the 50-tonne mass.
RULES["cbam"] = {
    "kind": "conditional",
    "label": "EU CBAM (Carbon Border Adjustment Mechanism)",
    "jurisdiction": "EU",
    "applies_in": ["EU"],
    "summary": "Applies to importers of covered goods — iron and steel, aluminium, "
               "cement, fertilisers, hydrogen and electricity — regardless of size.",
    "citation": "Regulation (EU) 2023/956; de minimis at Art. 2a as inserted by "
                "Regulation (EU) 2025/2083 Art. 1(2), read with Annex VII point 1 "
                "(50 tonnes net mass per year); hydrogen and electricity carve-out at "
                "Art. 2a(4)",
    "activity_test": "you are in scope if you import goods under a covered CN code into "
                     "the EU. Importers under 50 tonnes net mass a year are exempt, "
                     "except for hydrogen and electricity. Size is irrelevant.",
    "first_reporting_year": 2026,
    "phase_in": "Transitional reporting-only period 2023-2025; definitive period with "
                "certificate surrender from 2026.",
    "status": IN_FORCE,
    "confidence": "high",
    "model_limitations": "Whether you import covered CN codes is not an entity-profile "
                         "field, so this rule cannot resolve it. Note the 50-tonne de "
                         "minimis is a CLIFF, not an allowance: exceed it and the "
                         "obligation attaches to ALL goods imported that calendar year "
                         "(Art. 2a(2)) — do not subtract the first 50 tonnes.",
}

# --- EU ETS / UK ETS --------------------------------------------------------------------
# CORRECTED IN REVIEW: both are activity-gated with NO entity-size test of any kind, and
# the EU exclusion thresholds are measured across three preceding years, not per year.
RULES["eu_ets"] = {
    "kind": "conditional",
    "label": "EU ETS — monitoring, reporting & verification",
    "jurisdiction": "EU",
    "applies_in": ["EU"],
    "summary": "Applies to operators of installations carrying out an Annex I activity "
               "above its capacity threshold, and to aircraft and ship operators.",
    "citation": "Directive 2003/87/EC Annex I (activities and capacity thresholds); "
                "small-emitter opt-outs at Art. 27(1) and Art. 27a(1)",
    "activity_test": "you are in scope if you operate an installation carrying out an "
                     "Annex I activity above its threshold (for combustion, total rated "
                     "thermal input above 20 MW). Member States may exclude installations "
                     "below 25,000 tCO2e (and, for combustion, below 35 MW rated thermal "
                     "input) in EACH of the three preceding years under Art. 27(1), or "
                     "below 2,500 tCO2e on the same three-year basis under Art. 27a(1) — "
                     "which carries NO thermal-input condition.",
    "first_reporting_year": 2005,
    "status": IN_FORCE,
    "confidence": "high",
    "model_limitations": "Capacity and emissions gates are measured in MW and tCO2e, "
                         "which are not entity-profile fields — this rule can only tell "
                         "you that the test is activity-based, not whether you pass it.",
}

RULES["uk_ets"] = {
    "kind": "conditional",
    "label": "UK ETS — monitoring, reporting & verification",
    "jurisdiction": "UK",
    "applies_in": ["UK"],
    "summary": "Applies to operators of regulated installations, to aviation on covered "
               "routes, and from July 2026 to ships of 5,000 gross tonnage or more.",
    "citation": "SI 2020/1265 Sch. 2 Table C (installations; combustion above 20 MW "
                "total rated thermal input), Sch. 1 paras. 1-2 (aviation), Sch. 2A "
                "(maritime, inserted by SI 2026/392), Sch. 7 and Sch. 8 (small and "
                "ultra-small emitters)",
    "activity_test": "you are in scope if you operate a regulated installation, fly a "
                     "covered route, or operate a ship of 5,000 GT or more. Aviation "
                     "covers UK departures to the UK, EEA, Gibraltar or Switzerland and "
                     "Gibraltar-to-UK arrivals — inbound flights from the EEA or third "
                     "countries are NOT in scope. Size is irrelevant.",
    "first_reporting_year": 2021,
    "status": IN_FORCE,
    "confidence": "high",
    "model_limitations": "Gates are in MW, kg maximum take-off mass and gross tonnage, "
                         "none of which are entity-profile fields.",
}

# --- ISSB IFRS S2 -----------------------------------------------------------------------
# CORRECTED IN REVIEW: applicability is by JURISDICTIONAL ADOPTION, not entity size, and
# adoption is asymmetric between S1 and S2.
RULES["issb_s2"] = {
    "kind": "conditional",
    "label": "ISSB IFRS S2 (Climate-related Disclosures)",
    "jurisdiction": "global baseline",
    "summary": "Binds only where a jurisdiction has adopted it; the entity test is then "
               "whatever that jurisdiction's adopting instrument says.",
    "citation": "IFRS S2; adoption instruments vary by jurisdiction (e.g. Australia "
                "Corporations Act s.292A with AASB S2)",
    "activity_test": "IFRS S2 is a global baseline, not a directly-binding law. You are "
                     "compelled only where your jurisdiction has adopted it — Australia "
                     "mandates AASB S2 on a two-of-three size test phased from FY2025, "
                     "the UK and others are at various stages. This platform does not "
                     "model per-jurisdiction adoption, so it cannot answer for you.",
    "first_reporting_year": 2024,
    "phase_in": "Amendments to the greenhouse-gas disclosure requirements were issued in "
                "December 2025 and are effective for periods beginning on or after "
                "1 January 2027.",
    "status": IN_FORCE,
    "confidence": "medium",
    "model_limitations": "Per-jurisdiction adoption is the whole test and is not "
                         "modelled. Treat this entry as a pointer, not a verdict.",
}
