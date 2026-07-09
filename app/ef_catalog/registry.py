"""Registry of LCA / emission-factor databases the platform can integrate.

Research-verified landscape (2025-2026). Two hard rules encoded here:

1. LICENSE COMPLIANCE FIRST — ecoinvent / Sphera / full-Agribalyse LCI are NOT
   redistributable (results only); EXIOBASE's free tier is non-commercial only.
   ``redistributable`` gates which sources may ship inside the product.
2. NEVER blend pre-aggregated CO2e across sources with different GWP vintages
   (DEFRA=AR5, USEEIO v1.4=AR6): re-derive from per-gas splits at a common GWP
   set — which is exactly why the calc engine stores per-gas masses and applies
   GWP at calculation time.

Recommended audit-grade MVP stack (free, redistributable, direct-download):
  activity-based  -> DEFRA/DESNZ + US EPA GHG Factors Hub
  spend-based     -> USEEIO Supply-Chain factors (US) + Open CEDA (global)
  market Scope 2  -> AIB European Residual Mix + EPA eGRID
Aggregators (Climatiq) are a later phase, to reach commercially-licensed data.
"""

EF_DATABASES = {
    "DEFRA_DESNZ": {
        "name": "UK Government GHG Conversion Factors (DEFRA/DESNZ)",
        "method_type": "average_data", "unit_basis": "physical",
        "gwp_set": "AR5", "per_gas": True,
        "boundaries": ["combustion", "well_to_tank", "generation", "waste_treatment"],
        "license": "UK Open Government Licence", "redistributable": True, "cost": "free",
        "access": "annual XLSX/CSV download",
        "vintage_rule": "match factor year to the reporting period, not the build date",
    },
    "EPA_GHG_HUB": {
        "name": "US EPA GHG Emission Factors Hub",
        "method_type": "average_data", "unit_basis": "physical",
        "gwp_set": "AR5/AR6 by edition", "per_gas": True,
        "boundaries": ["combustion", "well_to_tank", "generation"],
        "license": "public domain", "redistributable": True, "cost": "free",
        "access": "annual XLSX download (incl. eGRID subregions)",
    },
    "USEEIO_SUPPLY_CHAIN": {
        "name": "US EPA / Cornerstone USEEIO Supply-Chain GHG factors",
        "method_type": "spend_based", "unit_basis": "currency:USD",
        "gwp_set": "AR6", "per_gas": False,
        "boundaries": ["cradle_to_gate"],
        "license": "public domain", "redistributable": True, "cost": "free",
        "access": "GitHub-versioned CSV; NAICS-6 categories",
        "note": "basic-price USD of a stated base year; adjust spend for inflation/price basis",
    },
    "OPEN_CEDA": {
        "name": "Open CEDA (Watershed)",
        "method_type": "spend_based", "unit_basis": "currency",
        "gwp_set": "verify per release", "per_gas": False,
        "boundaries": ["cradle_to_gate"],
        "license": "CC BY-SA", "redistributable": True, "cost": "free",
        "access": "openceda.org / AWS Open Data; 148 countries x 400 industries",
    },
    "EXIOBASE": {
        "name": "EXIOBASE 3 (EEIO MRIO)",
        "method_type": "spend_based", "unit_basis": "currency:EUR(basic price)",
        "gwp_set": "per-gas accounts", "per_gas": True,
        "boundaries": ["cradle_to_gate"],
        "license": "CC BY-SA-NC (free tier)", "redistributable": False,
        "cost": "free non-commercial; commercial license required",
        "access": "Zenodo download / pymrio; 49 regions x 163 industries",
    },
    "ECOINVENT": {
        "name": "ecoinvent (process LCI)",
        "method_type": "average_data", "unit_basis": "physical",
        "gwp_set": "per-gas elementary flows", "per_gas": True,
        "boundaries": ["cradle_to_gate", "cradle_to_grave"],
        "license": "commercial", "redistributable": False, "cost": "subscription",
        "access": "ecospold2 files via SimaPro/openLCA/Brightway; no public API",
        "note": "uncertainty via pedigree matrix -> lognormal",
    },
    "ADEME_BASE_EMPREINTE": {
        "name": "ADEME Base Empreinte (incl. Base Carbone + Agribalyse CO2e)",
        "method_type": "average_data", "unit_basis": "mixed",
        "gwp_set": "documented per entry", "per_gas": "many entries",
        "boundaries": ["documented per entry"],
        "license": "Etalab Licence Ouverte", "redistributable": True, "cost": "free",
        "access": "REST API + bulk CSV; French statutory reference",
    },
    "AIB_RESIDUAL_MIX": {
        "name": "AIB European Residual Mix",
        "method_type": "average_data", "unit_basis": "physical:kWh",
        "gwp_set": "CO2-focused", "per_gas": False,
        "boundaries": ["generation"],
        "license": "free download", "redistributable": True, "cost": "free",
        "access": "annual tables; THE market-based Scope 2 residual-mix source for Europe",
    },
    "CLIMATIQ": {
        "name": "Climatiq (aggregator API)",
        "method_type": "aggregator", "unit_basis": "normalized per source",
        "gwp_set": "per source", "per_gas": "per source",
        "boundaries": ["per source"],
        "license": "API terms; premium sources pass-through", "redistributable": False,
        "cost": "free starter tier; paid for full metadata + ecoinvent/EXIOBASE",
        "access": "REST API — later phase, for commercially-licensed data",
    },
}

# Correspondence-table reality for spend-based factors: EXIOBASE keys to NACE,
# USEEIO to NAICS-6/BEA, CEDA to its own taxonomy. Every crosswalk step
# (chart-of-accounts -> UNSPSC -> NAICS/NACE) is itself an uncertainty source,
# often larger than the factor's own uncertainty — version the crosswalks.
SPEND_CLASSIFICATIONS = ("NAICS", "NACE", "ISIC", "UNSPSC")
