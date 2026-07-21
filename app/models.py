from sqlalchemy import Column, Integer, String, Float, Date, ForeignKey, Boolean, Text, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import relationship
from .database import Base

class Organisation(Base):
    __tablename__ = "organisations"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    sector = Column(String, nullable=True)
    # SHA-256 hash of the org's API key (the plaintext key is returned exactly
    # once at registration and never stored). Supports rotation (new hash) and
    # revocation (revoked=True disables the key without deleting the org's data).
    api_key_hash = Column(String, unique=True, nullable=True, index=True)
    api_key_revoked = Column(Boolean, nullable=False, default=False)
    key_rotated_at = Column(String, nullable=True)
    # GHG Protocol Ch.3 consolidation approach: operational_control | financial_control |
    # equity_share. Now APPLIED by the calc engine (see services/boundary.py): it decides
    # what share of each ReportingEntity's emissions enters the inventory. Validated
    # against boundary.APPROACHES in code, not a DB CHECK (organisations is an FK target,
    # and the Corporate Standard is under revision with the approaches themselves in scope).
    consolidation_approach = Column(String, nullable=True, default="operational_control")
    # GHG Protocol Ch.3 asks a company to state AND justify its chosen approach. A reason
    # cannot be defaulted or back-filled — fabricating one is the very failure this fixes —
    # so it is NULL until a human writes it.
    consolidation_approach_reason = Column(Text, nullable=True)

class ActivityRecord(Base):
    __tablename__ = "activities"
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"))
    date = Column(String)  # ISO date
    category = Column(String)  # electricity, gas, diesel, flight, train, car, waste, spend
    subcategory = Column(String)  # economy/short-haul, etc.
    description = Column(Text)
    quantity = Column(Float)
    unit = Column(String)  # kWh, L, kg, tkm, pkm
    geo = Column(String)   # country/region code
    source_file = Column(String)
    # SHA-256 of the uploaded file content; used to reject accidental re-uploads
    # of the same file (double-counted emissions on a retry/double-click).
    upload_hash = Column(String, nullable=True, index=True)
    scope = Column(String)  # 1,2,3 - set later
    mapping_confidence = Column(Float)  # 0-1
    factor_id = Column(Integer, ForeignKey("emission_factors.id"), nullable=True)
    # Human-review gate (Gap 6): coarse resolver matches are SUGGESTED, not bound.
    # factor_id is only set by an exact match (auto) or a human decision.
    suggested_factor_id = Column(Integer, ForeignKey("emission_factors.id"), nullable=True)
    mapping_status = Column(String, default="unmapped")  # unmapped | auto | needs_review | approved | overridden
    mapping_basis = Column(String, nullable=True)  # exact | category_geo | category_only | fuzzy_subcategory
    provenance = Column(String)  # process/eeio/hybrid
    # GHG Protocol Scope 3 category (1-15). EXPLICIT USER INPUT ONLY — compute_co2e
    # must never write a derived value back here: that would destroy the
    # explicit-vs-derived distinction which is what makes a map-version change
    # detectable. Meaningful only when the line's frozen scope is "3".
    # Deliberately NO DB CheckConstraint: adding one to this FK-target table would
    # need batch_alter_table under PRAGMA foreign_keys=ON, and a constraint declared
    # on the model but not in the migration would exist in tests (create_all) and
    # NOT in production (alembic). The 1..15 range is enforced in code instead.
    ghgp_category = Column(Integer, nullable=True)
    # The operation this activity belongs to (GHGP Ch.3 organisational boundary).
    # NULL = the reporting organisation ITSELF, which owns and controls itself -> share
    # 1.0 under all three approaches. Every pre-existing row is NULL, so the boundary is
    # a no-op until entities exist — that is the whole backward-compatibility mechanism.
    # Deliberately a plain Integer with NO ForeignKey, same doctrine as ghgp_category
    # above: an FK on this FK-target table would need batch_alter_table, and an FK could
    # not enforce the TENANT match anyway — which is the check that actually matters.
    # Existence + org ownership are validated at the API boundary and re-checked by the gate.
    entity_id = Column(Integer, nullable=True, index=True)

    factor = relationship("EmissionFactor", back_populates="activities",
                          foreign_keys=[factor_id])
    suggested_factor = relationship("EmissionFactor", foreign_keys=[suggested_factor_id])

class EmissionFactor(Base):
    __tablename__ = "emission_factors"
    __table_args__ = (
        # A negative factor would turn a source into a sink and silently understate
        # the total. NULL is allowed: per-gas factors carry no aggregate `value`.
        CheckConstraint("value >= 0", name="ck_factor_value_nonneg"),
    )
    id = Column(Integer, primary_key=True)
    source = Column(String)  # DEFRA2024 (demo), etc.
    version = Column(String) # 2024.1
    geography = Column(String) # GB, EU, Global
    year = Column(Integer)
    category = Column(String) # electricity, diesel, flight, etc.
    subcategory = Column(String) # tech / route
    unit = Column(String) # per kWh, per L, per tkm, per pkm, per kg — or a currency code (GBP/EUR) for spend-based EEIO factors
    gwp_set = Column(String) # GWP vintage baked into `value` (aggregate factors only)
    value = Column(Float) # kgCO2e per unit
    # GHG Protocol Scope 3 calculation-method hierarchy (Technical Guidance):
    # supplier_specific > hybrid > average_data (activity-based) > spend_based (EEIO).
    # Drives resolver preference and the primary-data-share metric.
    method_type = Column(String, nullable=True, default="average_data")
    # LCA system boundary of the factor (cradle_to_gate | cradle_to_grave |
    # gate_to_gate | well_to_tank | combustion | generation | waste_treatment ...).
    # Boundary metadata MUST live on the factor: combining e.g. a cradle-to-gate
    # material factor with a separate use-phase factor without it double counts.
    lca_boundary = Column(String, nullable=True)
    # Per-gas decomposition: kg of ACTUAL GAS emitted per activity unit. When set,
    # the calc engine applies the requested GWP set at CALCULATION time
    # (co2e = kg_co2*1 + kg_ch4*GWP(CH4) + kg_n2o*GWP(N2O)) — this is what makes
    # the AR5/AR6 switch real. When NULL, `value` is used with a gwp_set check.
    kg_co2 = Column(Float, nullable=True)
    kg_ch4 = Column(Float, nullable=True)
    kg_n2o = Column(Float, nullable=True)
    # CH4 origin routes the correct GWP variant: "fossil" (combustion sources) or
    # "biogenic" (landfill/organic). NULL falls back to the blended CH4 GWP.
    ch4_origin = Column(String, nullable=True)
    # Biogenic CO2 (kg per unit) — ISO 14067: reported SEPARATELY, never netted
    # into the fossil total. Kept outside total_co2e and surfaced on its own.
    kg_co2_biogenic = Column(Float, nullable=True)
    # Spend-based (EEIO) economics. A spend factor is priced per currency-unit of a
    # SPECIFIC base year at a SPECIFIC price basis — spend must be inflation-adjusted
    # to base_year and FX-converted at the base-year rate before applying `value`.
    base_year = Column(Integer, nullable=True)             # e.g. 2019 for EXIOBASE
    price_basis = Column(String, nullable=True)            # basic | purchaser
    supersedes_id = Column(Integer, nullable=True)

    activities = relationship("ActivityRecord", back_populates="factor",
                              foreign_keys="ActivityRecord.factor_id")

    @property
    def has_gas_breakdown(self) -> bool:
        return any(v is not None for v in (self.kg_co2, self.kg_ch4, self.kg_n2o))

class FxRate(Base):
    """Reference FX rate: 1 base_currency = `rate` quote_currency in `year`.

    Spend-based EEIO conversion uses the rate of the FACTOR's base year, not the
    spot rate (GHG Protocol / EEIO practice). Global reference data, not per-org.
    """
    __tablename__ = "fx_rates"
    __table_args__ = (
        # Append-only: no unique constraint — corrections INSERT a new row and
        # lookups take the latest (highest id), so the value history an assurer
        # needs is never overwritten in place.
        CheckConstraint("rate > 0", name="ck_fx_rate_pos"),
    )
    id = Column(Integer, primary_key=True)
    base_currency = Column(String, nullable=False)   # e.g. GBP
    quote_currency = Column(String, nullable=False)  # e.g. EUR
    year = Column(Integer, nullable=False)
    rate = Column(Float, nullable=False)             # quote per 1 base
    recorded_at = Column(String, nullable=True)      # ISO timestamp of entry


class PriceIndex(Base):
    """CPI-style deflator to inflation-adjust spend to a factor's base year.

    index is relative to a fixed reference (ratio of two years' index deflates
    a spend amount between years). Keyed by currency/economy.
    """
    __tablename__ = "price_indices"
    __table_args__ = (
        # Append-only, same rationale as FxRate.
        CheckConstraint("index_value > 0", name="ck_price_index_pos"),
    )
    id = Column(Integer, primary_key=True)
    currency = Column(String, nullable=False)  # economy proxy, e.g. GBP
    year = Column(Integer, nullable=False)
    index_value = Column(Float, nullable=False)
    recorded_at = Column(String, nullable=True)  # ISO timestamp of entry


class MarketInstrument(Base):
    """A contractual instrument for market-based Scope 2 (GHG Protocol Scope 2 Guidance).

    Hierarchy honoured by the calc engine: supplier_specific / ppa / rec first,
    then residual_mix, then grid-average fallback (= the location factor).
    ``kg_co2e_per_kwh`` is the contractual emission rate (0.0 for RECs/renewable PPAs).

    Volume matching (Scope 2 Guidance Ch. 4): ``coverage_kwh`` is the kWh the
    instrument actually covers; the calc engine allocates it cumulatively across
    the run's electricity consumption and the remainder falls through to the next
    instrument or the grid average. NULL = unbounded (only sensible for
    residual_mix). ``gwp_set`` is the vintage the contractual rate was computed
    with; an instrument is not applied to a run requesting a different set.
    """
    __tablename__ = "market_instruments"
    __table_args__ = (
        CheckConstraint("kg_co2e_per_kwh >= 0", name="ck_instrument_rate_nonneg"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    instrument_type = Column(String, nullable=False)  # supplier_specific | ppa | rec | residual_mix
    kg_co2e_per_kwh = Column(Float, nullable=False)
    coverage_kwh = Column(Float, nullable=True)  # kWh covered; NULL = unbounded
    # The grid/market the instrument belongs to (e.g. "GB", "DE"), matched against
    # the consumption's geo (Scope 2 Guidance quality criteria). NULL = unspecified:
    # the instrument still applies but the allocation is flagged market_unverified.
    market = Column(String, nullable=True)
    gwp_set = Column(String, nullable=True, default="AR6")
    start_date = Column(String, nullable=True)  # ISO; window the instrument covers
    end_date = Column(String, nullable=True)
    description = Column(Text)


class EmissionsTarget(Base):
    """A science-based / net-zero emissions target anchored to an immutable base run.

    Trajectory is assessed against the base run's frozen total, so a target's
    baseline can never drift. ``target_reduction_pct`` is the TOTAL reduction by
    ``target_year`` vs the base year (0-1); the pathway is linear between them.
    """
    __tablename__ = "emissions_targets"
    __table_args__ = (
        CheckConstraint("target_reduction_pct >= 0 AND target_reduction_pct <= 1",
                        name="ck_target_reduction_frac"),
        CheckConstraint("target_year > base_year", name="ck_target_year_after_base"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    name = Column(String, nullable=False)
    target_type = Column(String, nullable=False)   # near_term | long_term | net_zero
    scope_coverage = Column(String, nullable=False, default="1+2")  # e.g. "1+2", "1+2+3"
    base_run_id = Column(Integer, ForeignKey("calculation_runs.id"), nullable=False)
    base_year = Column(Integer, nullable=False)
    target_year = Column(Integer, nullable=False)
    target_reduction_pct = Column(Float, nullable=False)  # total reduction by target year
    ambition = Column(String, nullable=True)       # 1.5C | WB2C | custom
    sbti_validated = Column(Boolean, default=False)
    created_at = Column(String)


class CarbonCredit(Base):
    """A carbon credit holding for neutrality/offset accounting (ISO 14068).

    Only RETIRED credits applied to a specific run count toward a neutrality
    claim. Integrity metadata (ICVCM Core Carbon Principles approval, VCMI claim
    tier, removal vs avoidance) drives the claim-quality guardrails.
    """
    __tablename__ = "carbon_credits"
    __table_args__ = (
        CheckConstraint("quantity_tco2e > 0", name="ck_credit_qty_pos"),
        # A real registry serial is globally unique — the standard defence
        # against a credit being double-held/double-retired (NULL serials, i.e.
        # unserialised demo entries, are allowed to repeat under SQLite).
        UniqueConstraint("registry", "serial_number", name="uq_credit_registry_serial"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    registry = Column(String, nullable=False)      # verra | gold_standard | acr | car | puro
    project_id = Column(String, nullable=True)
    serial_number = Column(String, nullable=True)
    vintage_year = Column(Integer, nullable=True)
    quantity_tco2e = Column(Float, nullable=False)
    credit_type = Column(String, nullable=False)   # removal | reduction | avoidance
    ccp_approved = Column(Boolean, default=False)  # ICVCM Core Carbon Principles
    vcmi_claim = Column(String, nullable=True)     # none | silver | gold | platinum
    retired = Column(Boolean, default=False)
    retirement_date = Column(String, nullable=True)
    applied_to_run_id = Column(Integer, ForeignKey("calculation_runs.id"), nullable=True)
    created_at = Column(String)


class TaxonomyActivity(Base):
    """An economic activity for EU Taxonomy alignment reporting.

    Alignment requires: eligible AND substantial-contribution AND DNSH (do no
    significant harm) AND minimum safeguards. Turnover/CapEx/OpEx are the three
    KPIs reported as % aligned.
    """
    __tablename__ = "taxonomy_activities"
    __table_args__ = (
        CheckConstraint("turnover >= 0 AND capex >= 0 AND opex >= 0",
                        name="ck_taxo_nonneg"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    name = Column(String, nullable=False)
    reporting_year = Column(Integer, nullable=False)
    turnover = Column(Float, nullable=False, default=0.0)
    capex = Column(Float, nullable=False, default=0.0)
    opex = Column(Float, nullable=False, default=0.0)
    eligible = Column(Boolean, nullable=False, default=False)
    substantial_contribution = Column(Boolean, nullable=False, default=False)
    dnsh_pass = Column(Boolean, nullable=False, default=False)
    minimum_safeguards_pass = Column(Boolean, nullable=False, default=False)
    objective = Column(String, nullable=True)  # climate_mitigation | climate_adaptation | ...
    created_at = Column(String)


class LcaAssessment(Base):
    """A life-cycle / sector assessment computed from a bill of items against a
    functional unit (ISO 14067 product PCF, ISO 14083 transport chain, EN 15804
    /EN 15978 construction). Reuses the fail-closed calc engine per item and
    reports by stage/module, total, and per functional unit."""
    __tablename__ = "lca_assessments"
    __table_args__ = (
        CheckConstraint("functional_unit_quantity > 0", name="ck_lca_fu_pos"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    name = Column(String, nullable=False)
    standard = Column(String, nullable=False)   # iso_14067 | iso_14040_44 | iso_14083 | en_15804 | en_15978
    functional_unit = Column(String, nullable=False)   # e.g. "1 kg product", "1 t.km", "1 m2 GFA"
    functional_unit_quantity = Column(Float, nullable=False, default=1.0)
    gwp_set = Column(String, nullable=False, default="AR6")
    created_at = Column(String)


class LcaItem(Base):
    """One input/leg/lifecycle-module line of an assessment."""
    __tablename__ = "lca_items"
    __table_args__ = (
        CheckConstraint("allocation_factor >= 0 AND allocation_factor <= 1",
                        name="ck_lca_alloc"),
    )
    id = Column(Integer, primary_key=True)
    assessment_id = Column(Integer, ForeignKey("lca_assessments.id"), nullable=False)
    stage = Column(String, nullable=False)      # lifecycle stage / EN module (A1-A3, C3, ...) / transport leg
    description = Column(Text, nullable=True)
    quantity = Column(Float, nullable=True)
    unit = Column(String, nullable=True)
    factor_id = Column(Integer, ForeignKey("emission_factors.id"), nullable=True)
    allocation_factor = Column(Float, nullable=False, default=1.0)  # co-product allocation

    factor = relationship("EmissionFactor", foreign_keys=[factor_id])


class FinancedPosition(Base):
    """A financed position for PCAF financed-emissions accounting.

    Financed emissions = attribution factor x investee emissions, where the
    attribution factor = outstanding_amount / attribution_denominator (EVIC for
    listed equity/bonds; total equity+debt for loans; property value for real
    estate — both in the SAME currency, so the ratio is dimensionless).
    ``data_quality_score`` is the PCAF 1 (best/verified) .. 5 (proxy) score.
    """
    __tablename__ = "financed_positions"
    __table_args__ = (
        CheckConstraint("outstanding_amount >= 0", name="ck_fp_outstanding_nonneg"),
        CheckConstraint("attribution_denominator > 0", name="ck_fp_denom_pos"),
        CheckConstraint("data_quality_score >= 1 AND data_quality_score <= 5", name="ck_fp_dq"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    investee_name = Column(String, nullable=False)
    asset_class = Column(String, nullable=False)   # listed_equity | corporate_bonds | business_loans | project_finance | commercial_real_estate | mortgages | motor_vehicle_loans
    currency = Column(String, nullable=False)
    outstanding_amount = Column(Float, nullable=False)
    attribution_denominator = Column(Float, nullable=False)
    investee_scope1_tco2e = Column(Float, nullable=False, default=0.0)
    investee_scope2_tco2e = Column(Float, nullable=False, default=0.0)
    investee_scope3_tco2e = Column(Float, nullable=True)
    investee_revenue_millions = Column(Float, nullable=True)  # for SFDR PAI 3 intensity
    data_quality_score = Column(Integer, nullable=False, default=5)
    as_of_date = Column(String, nullable=True)
    created_at = Column(String)


class AssuranceEngagement(Base):
    """A third-party assurance engagement over one immutable calculation run
    (ISAE 3410 / ISO 14064-3 / ISSA 5000).

    The run's frozen lineage is the evidence base. An unqualified conclusion is
    gated on the readiness checklist passing and no open material findings — the
    conclusion cannot overstate the assurance obtained.
    """
    __tablename__ = "assurance_engagements"
    __table_args__ = (
        CheckConstraint("materiality_pct > 0 AND materiality_pct <= 100",
                        name="ck_assurance_materiality"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    run_id = Column(Integer, ForeignKey("calculation_runs.id"), nullable=False)
    standard = Column(String, nullable=False)   # ISAE_3410 | ISO_14064_3 | ISSA_5000
    level = Column(String, nullable=False)      # limited | reasonable
    assuror_name = Column(String, nullable=True)
    period_label = Column(String, nullable=True)
    materiality_pct = Column(Float, nullable=False, default=5.0)
    status = Column(String, nullable=False, default="planned")  # planned|in_progress|concluded
    opinion = Column(String, nullable=True)     # unqualified|qualified|adverse|disclaimer
    opinion_note = Column(Text, nullable=True)
    access_token_hash = Column(String, nullable=True)  # read-only assuror access
    # Readiness checklist frozen at conclusion time, so a concluded opinion is
    # judged against the run as it stood then — not a live-recomputed checklist.
    readiness_snapshot = Column(Text, nullable=True)
    created_at = Column(String)
    concluded_at = Column(String, nullable=True)


class AssuranceFinding(Base):
    """One assurance observation/finding against an engagement, optionally tied
    to a specific emission line item."""
    __tablename__ = "assurance_findings"
    id = Column(Integer, primary_key=True)
    engagement_id = Column(Integer, ForeignKey("assurance_engagements.id"), nullable=False)
    line_item_id = Column(Integer, ForeignKey("emission_line_items.id"), nullable=True)
    severity = Column(String, nullable=False)   # observation | minor | material
    description = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="open")  # open | resolved
    resolution_note = Column(Text, nullable=True)
    created_at = Column(String)


class CbamDefaultValue(Base):
    """Default embedded-emissions values for CBAM goods (tCO2e per tonne).

    Stands in for the Commission-published default values; DEMO data until the
    official tables are loaded. Matched by longest CN-code prefix. Global
    reference data (admin-gated writes), append-only like FX/CPI.
    """
    __tablename__ = "cbam_default_values"
    __table_args__ = (
        CheckConstraint("direct_t_co2e_per_t >= 0", name="ck_cbam_direct_nonneg"),
        CheckConstraint("indirect_t_co2e_per_t >= 0", name="ck_cbam_indirect_nonneg"),
    )
    id = Column(Integer, primary_key=True)
    cn_code_prefix = Column(String, nullable=False)   # e.g. "7208" (flat-rolled iron/steel)
    good_category = Column(String, nullable=False)    # iron_steel | aluminium | cement | fertilisers | hydrogen | electricity
    direct_t_co2e_per_t = Column(Float, nullable=False)
    indirect_t_co2e_per_t = Column(Float, nullable=False)
    valid_year = Column(Integer, nullable=False)
    recorded_at = Column(String, nullable=True)


class CbamGood(Base):
    """One imported goods line feeding a CBAM declaration.

    Embedded emissions use VERIFIED actual installation values when present;
    unverified actuals are never used (CBAM requires accredited verification)
    — the line falls back to default values with the substitution flagged.
    """
    __tablename__ = "cbam_goods"
    __table_args__ = (
        CheckConstraint("quantity_tonnes > 0", name="ck_cbam_qty_pos"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    cn_code = Column(String, nullable=False)
    description = Column(Text)
    quantity_tonnes = Column(Float, nullable=False)
    origin_country = Column(String, nullable=False)
    import_date = Column(String, nullable=False)      # ISO date
    installation = Column(Text, nullable=True)        # producing installation, if known
    actual_direct_t_per_t = Column(Float, nullable=True)
    actual_indirect_t_per_t = Column(Float, nullable=True)
    actual_verified = Column(Boolean, default=False)  # accredited verification done?
    carbon_price_paid_eur_per_t = Column(Float, nullable=True)  # price paid in origin country


class ReportingPeriod(Base):
    """A named reporting window for an organisation (e.g. FY2025).

    A period can be frozen once its inventory is finalised for disclosure; a
    frozen period should not accept new activities into its calculation runs.
    """
    __tablename__ = "reporting_periods"
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    label = Column(String, nullable=False)  # e.g. "FY2025"
    start_date = Column(String)  # ISO
    end_date = Column(String)    # ISO
    frozen = Column(Boolean, default=False)

class CalculationRun(Base):
    """An immutable snapshot of one calculation for one organisation.

    Every /calculate/run creates a NEW run; prior runs are never mutated or
    deleted, so any historical number is reproducible (Gap 5). The coverage
    counters are frozen at compute time, so a run's reported completeness can
    never silently contradict later re-mapping.
    """
    __tablename__ = "calculation_runs"
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    reporting_period_id = Column(Integer, ForeignKey("reporting_periods.id"), nullable=True)
    created_at = Column(String)  # ISO timestamp (UTC)
    gwp_set = Column(String)     # AR5 / AR6 applied
    status = Column(String)      # pending | complete
    # frozen coverage snapshot
    total_activities = Column(Integer, default=0)
    mapped = Column(Integer, default=0)
    unmapped = Column(Integer, default=0)
    unit_errors = Column(Integer, default=0)
    data_errors = Column(Integer, default=0)
    gwp_mismatch = Column(Integer, default=0)
    total_co2e = Column(Float, default=0.0)          # location-based total (headline)
    # GHG Protocol dual reporting: same total with Scope 2 swapped to market-based.
    total_co2e_market = Column(Float, default=0.0)
    # ISO 14067: biogenic CO2 reported separately, never netted into the totals above.
    total_biogenic_co2e = Column(Float, default=0.0)
    # Emissions-weighted pedigree data-quality score (1 best .. 5 worst).
    data_quality_score = Column(Float, default=0.0)
    notes = Column(Text)  # JSON: per-activity exclusion reasons
    # Fingerprint of the org's activity set at compute time (id/factor/quantity/unit).
    # Lets a reader detect that a run is stale even when the activity COUNT is unchanged
    # (e.g. an activity was re-mapped to a different factor).
    activities_fingerprint = Column(String)
    # --- GHGP Scope 3 15-category dimension (frozen onto the run) ---
    # NULL ghgp_standard_version is the LEGACY-RUN sentinel: such a run has no
    # completeness statement and must never be rendered as a clean 15x0.0 table.
    ghgp_standard_version = Column(String, nullable=True)
    ghgp_map_version = Column(String, nullable=True)
    # Which factor-boundary ACCEPTANCE VOCABULARY (Table 5.4 token policy) produced this
    # run's per-line minimum-boundary verdicts. Versioned apart from the GHGP standard
    # because the token set is OUR interpretation, not Protocol content. NULL = computed
    # before the policy was versioned; boundary_policy_for_run() reports that as
    # "s3bnd-v1 (inferred)" at render time and never back-fills it into history.
    ghgp_boundary_policy_version = Column(String, nullable=True)
    # Hash of the declaration set frozen onto this run — detects an exclusion
    # statement being edited AFTER the run that filed it.
    scope3_declaration_fingerprint = Column(String, nullable=True)
    # --- Scope 3 Category 15 = PCAF financed emissions, frozen onto the run ---
    # KG. NULL = financed emissions were NOT evaluated for this run; 0.0 = evaluated
    # and genuinely zero. NEVER added to total_co2e (which is activity-derived and is
    # the invariant an assurer walks); the DISCLOSED total in the renderers adds it.
    financed_co2e = Column(Float, nullable=True)
    financed_as_of = Column(String, nullable=True)
    financed_include_scope3 = Column(Boolean, nullable=True)
    # Hash of the position set frozen onto this run — detects the live loan/investment
    # ledger being edited after the run that filed it.
    financed_fingerprint = Column(String, nullable=True)
    # --- Frozen GHG Protocol Ch.3 organisational boundary ---
    # NULL boundary_version is the LEGACY-RUN sentinel (mirrors ghgp_standard_version):
    # such a run has no boundary statement and must NEVER render as a clean
    # "operational_control, 100%" claim it never made.
    boundary_version = Column(String, nullable=True)
    consolidation_approach = Column(String, nullable=True)
    consolidation_reason = Column(Text, nullable=True)
    # activities_fingerprint hashes ACTIVITIES and is structurally blind to an
    # equity_share_pct 40->100 edit or an approach flip — either changes every number
    # while every run still reports FRESH. This closes that.
    consolidation_fingerprint = Column(String, nullable=True)
    # --- Inventory REMOVALS (GHG Protocol Land Sector & Removals) ---
    # KG of the org's OWN gross removals within its boundary (DAC, biochar,
    # afforestation, ...). Reported SEPARATELY — the fourth disjoint pool alongside
    # total_biogenic_co2e and financed_co2e. NEVER in total_co2e; "net" is derived at
    # render time (there is deliberately no net column, so netting is impossible).
    # NULL = not evaluated (legacy/false-zero); 0.0 = evaluated and genuinely zero.
    total_removals_co2e = Column(Float, nullable=True)
    removals_reversed_co2e = Column(Float, nullable=True)   # KG reversals booked this period
    removals_as_of = Column(String, nullable=True)
    removals_fingerprint = Column(String, nullable=True)    # detects the live ledger edited after filing
    removals_lsrg_version = Column(String, nullable=True)   # legacy sentinel (NULL = dimension not evaluated)
    # KG of GROSS emissions EXCLUDED by the boundary: sum of (1 - share) * gross.
    # NEVER in total_co2e (which stays exactly the sum of location line items — the
    # assurer's invariant), and never added to the disclosed total either: unlike
    # financed_co2e this is a DIFFERENT measure, not a missing addend (adding an
    # equity-excluded associate's gross back is the double count Cat 15 exists to
    # avoid). NULL = not evaluated (legacy); 0.0 = evaluated, nothing excluded.
    total_co2e_non_consolidated = Column(Float, nullable=True)


class RemovalRecord(Base):
    """A CO2 REMOVAL within the org's boundary (GHG Protocol Land Sector & Removals).

    The org's OWN sequestration — technological (DAC+storage, BECCS, enhanced
    weathering) or land-based (afforestation, soil carbon, biochar) — NOT a purchased
    offset credit (that is CarbonCredit, a market instrument) and NOT biogenic-CO2
    flux. Reported separately from gross emissions, never netted into total_co2e.

    A REVERSAL (a stored removal later re-emitted — a forest burns) is a first-class
    record: record_kind='reversal', positive quantity, reverses_record_id -> original.
    It reduces the CURRENT period's net removals; a prior filed run is never restated.
    """
    __tablename__ = "removal_records"
    __table_args__ = (
        CheckConstraint("quantity_tco2e > 0", name="ck_removal_qty_pos"),
        CheckConstraint("removal_category IN ('technological','land_based')",
                        name="ck_removal_category"),
        CheckConstraint("record_kind IN ('removal','reversal')", name="ck_removal_record_kind"),
        CheckConstraint("scope IN ('1','3')", name="ck_removal_scope"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    # Bare Integer, NO FK (mirrors ActivityRecord.entity_id): a cross-tenant/dangling
    # id must resolve to fail-open at compute time, which an FK would forbid.
    entity_id = Column(Integer, nullable=True, index=True)
    reporting_period_id = Column(Integer, ForeignKey("reporting_periods.id"), nullable=True)
    record_kind = Column(String, nullable=False, default="removal")   # removal | reversal
    reverses_record_id = Column(Integer, ForeignKey("removal_records.id"), nullable=True)
    removal_category = Column(String, nullable=False)      # technological | land_based
    method = Column(String, nullable=False)                # dac | beccs | biochar | afforestation | ...
    scope = Column(String, nullable=False)                 # 1 (own ops) | 3 (value chain)
    quantity_tco2e = Column(Float, nullable=False)         # > 0 always; sign is carried by record_kind
    quantification_method = Column(String, nullable=False)  # stock_difference | gain_loss | metered
    storage_medium = Column(String, nullable=True)
    expected_durability_years = Column(Integer, nullable=True)
    monitoring_method = Column(Text, nullable=True)
    monitoring_period_years = Column(Integer, nullable=True)
    reversal_accounting = Column(Text, nullable=True)
    # Removed carbon must not ALSO be sold as a credit (that is a double claim).
    attribute_retained = Column(Boolean, nullable=False, default=True)
    credit_registry = Column(String, nullable=True)
    credit_serial_if_sold = Column(String, nullable=True)  # cross-check vs carbon_credits.serial_number
    uncertainty_pct = Column(Float, nullable=True)
    buffer_pct = Column(Float, nullable=True)
    vintage_year = Column(Integer, nullable=True)
    as_of_date = Column(String, nullable=True)
    created_at = Column(String, nullable=True)


class RunRemovalLine(Base):
    """A removal frozen against an immutable run (the RunFinancedLine analogue).

    NOT an EmissionLineItem: a removal is not an activity, and would pollute
    total_activities / mapped / coverage / fingerprint / DQ. co2e is stored POSITIVE
    (kg), and a CHECK forbids negatives — a removal can never be smuggled into a total
    as a negative reduction; it lives in its own positive-signed pool.
    """
    __tablename__ = "run_removal_lines"
    __table_args__ = (
        UniqueConstraint("run_id", "removal_record_id", name="uq_run_removal_line"),
        CheckConstraint("co2e >= 0", name="ck_rrl_co2e_nonneg"),
        CheckConstraint("record_kind IN ('removal','reversal')", name="ck_rrl_record_kind"),
    )
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("calculation_runs.id"), nullable=False)
    removal_record_id = Column(Integer, ForeignKey("removal_records.id"), nullable=False)
    removal_category = Column(String, nullable=False)      # frozen copy (never joins the live table)
    scope = Column(String, nullable=False)
    record_kind = Column(String, nullable=False)
    co2e = Column(Float, nullable=False)                   # KG, positive (tCO2e x1000 x entity share)
    details = Column(Text, nullable=False)                 # frozen full lineage


class ReportingEntity(Base):
    """One operation/investee inside a tenant's organisational boundary (GHGP Ch.3).

    NOT a tenant: organisation_id remains the security boundary; an entity is a
    sub-dimension inside one org.

    FLAT by construction — deliberately NO parent_entity_id. Indirect chains (80% of a
    sub holding 50% of a JV) are NOT multiplied: the GHG Protocol specifies no
    multiplication rule, so computing one would be an uncitable platform policy that
    silently changes the number. The preparer asserts the EFFECTIVE economic interest
    and justifies it in equity_share_basis.

    The control facts are INDEPENDENT of accounting_category and of ownership %:
    operational control is an asserted judgement, not a function of equity (IFRS S2
    educational material Ex. 2A vs 2B — the same 20% associate, opposite outcomes).
    accounting_category drives DISCLOSURE only, never the weight.
    """
    __tablename__ = "reporting_entities"
    __table_args__ = (
        UniqueConstraint("organisation_id", "name", name="uq_entity_org_name"),
        CheckConstraint("equity_share_pct IS NULL OR "
                        "(equity_share_pct >= 0 AND equity_share_pct <= 100)",
                        name="ck_entity_equity_pct_range"),
        CheckConstraint("accounting_category IN ('subsidiary','joint_venture_incorporated',"
                        "'joint_operation','associate','fixed_asset_investment',"
                        "'franchise','lease_finance','lease_operating')",
                        name="ck_entity_acct_category"),
        # Joint financial control is the one place a control approach falls back to a
        # percentage; it is not compatible with sole financial control.
        CheckConstraint("NOT (financial_control = 1 AND joint_financial_control = 1)",
                        name="ck_entity_joint_vs_sole_fc"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    name = Column(String, nullable=False)
    entity_ref = Column(String, nullable=True)          # the client's own group/ERP code
    accounting_category = Column(String, nullable=False)
    # Economic interest — an ASSERTED, evidenced input, never read from a share
    # register ("economic substance overrides legal ownership"). NULL = not asserted.
    equity_share_pct = Column(Float, nullable=True)
    equity_share_basis = Column(Text, nullable=True)
    # Control judgements, each independent of ownership %. NULL = NOT ASSERTED.
    financial_control = Column(Boolean, nullable=True)
    joint_financial_control = Column(Boolean, nullable=True)
    operational_control = Column(Boolean, nullable=True)
    control_rationale = Column(Text, nullable=True)
    # Financial-statement group membership — INDEPENDENT of the GHGP approach and of
    # accounting_category. Without it the IFRS S2 29(a)(iv) disaggregation is not
    # derivable: that clause splits on the consolidated ACCOUNTING group.
    in_consolidated_accounting_group = Column(Boolean, nullable=True)
    effective_from = Column(String, nullable=True)      # ISO; NULL = unbounded
    effective_to = Column(String, nullable=True)
    created_at = Column(String, nullable=True)


class RunEntityBoundary(Base):
    """The IMMUTABLE per-run boundary — the gross -> share -> consolidated walk.

    Complete by construction (the RunScope3Declaration doctrine): one row per entity
    the org holds INCLUDING entities weighted 0.0 (those rows ARE the "other investees
    excluded" list the disclosure clauses ask for), plus always exactly one 'self' row.
    """
    __tablename__ = "run_entity_boundary"
    __table_args__ = (
        # entity_key, not entity_id: SQLite treats NULLs as DISTINCT in a unique index,
        # so a nullable entity_id could not stop two 'self' rows.
        UniqueConstraint("run_id", "entity_key", name="uq_run_entity_boundary"),
        CheckConstraint("share_factor >= 0 AND share_factor <= 1", name="ck_reb_share_range"),
        CheckConstraint("group_class IN ('consolidated_accounting_group','other_investee',"
                        "'unclassified')", name="ck_reb_group_class"),
    )
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("calculation_runs.id"), nullable=False)
    entity_key = Column(String, nullable=False)        # 'self' | 'e:<id>'
    entity_id = Column(Integer, nullable=True)         # provenance only, never joined back
    entity_name = Column(String, nullable=False)
    entity_ref = Column(String, nullable=True)
    accounting_category = Column(String, nullable=False)   # 'reporting_org' for the self row
    # --- frozen INPUTS (the weight is re-derivable from the run alone) ---
    equity_share_pct = Column(Float, nullable=True)
    equity_share_basis = Column(Text, nullable=True)
    financial_control = Column(Boolean, nullable=True)
    joint_financial_control = Column(Boolean, nullable=True)
    operational_control = Column(Boolean, nullable=True)
    control_rationale = Column(Text, nullable=True)
    in_consolidated_accounting_group = Column(Boolean, nullable=True)
    effective_from = Column(String, nullable=True)
    effective_to = Column(String, nullable=True)
    # --- frozen VERDICT (freeze the verdict, not just the inputs — a later fix to the
    #     share function must be DETECTABLE, never retroactively applied to a filed run) ---
    approach = Column(String, nullable=False)
    share_factor = Column(Float, nullable=False)       # 0.0..1.0, UNROUNDED
    share_basis = Column(String, nullable=False)
    resolved = Column(Boolean, nullable=False)         # False => a disclosure blocker
    group_class = Column(String, nullable=False)
    gross_co2e = Column(Float, nullable=False)         # KG, before the share
    consolidated_co2e = Column(Float, nullable=False)  # KG, after the share
    # IFRS S2 ¶29(a)(iv): the Scope 1 / Scope 2 (location-based) split, per entity,
    # so the disaggregation between the consolidated group and other investees can be
    # reported PER SCOPE (the clause asks for Scope 1 and Scope 2, not all-scope).
    # NULL only on runs frozen before this dimension existed — the summary falls back
    # to the all-scope figure and flags the scope split unavailable (reproduction
    # contract: a legacy run renders exactly what it froze, never a back-filled claim).
    scope1_consolidated_co2e = Column(Float, nullable=True)   # KG, after the share
    scope2_consolidated_co2e = Column(Float, nullable=True)   # KG, after the share, location-based
    line_count = Column(Integer, nullable=False, default=0)
    boundary_version = Column(String, nullable=False)
    frozen_at = Column(String, nullable=False)


class Scope3CategoryDeclaration(Base):
    """The LIVE, editable Scope 3 screen: one row per (org, period, category).

    This is the org's assertion about a category. It is copied verbatim onto every
    run (RunScope3Declaration) so a filed statement can never be edited after the
    fact. reporting_period_id is NOT NULL: a completeness assertion is inherently
    period-bound, so an org-wide run can never be disclosure_ready.
    """
    __tablename__ = "scope3_category_declarations"
    __table_args__ = (
        UniqueConstraint("organisation_id", "reporting_period_id", "category",
                         name="uq_s3decl_org_period_cat"),
        CheckConstraint("category >= 1 AND category <= 15", name="ck_s3decl_cat"),
        CheckConstraint(
            "status IN ('included','not_applicable','not_material','not_measured')",
            name="ck_s3decl_status"),
        CheckConstraint("screening_estimate_tco2e IS NULL OR screening_estimate_tco2e >= 0",
                        name="ck_s3decl_est_nonneg"),
        CheckConstraint("materiality_threshold_pct IS NULL OR "
                        "(materiality_threshold_pct >= 0 AND materiality_threshold_pct <= 100)",
                        name="ck_s3decl_thresh"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    reporting_period_id = Column(Integer, ForeignKey("reporting_periods.id"), nullable=False)
    category = Column(Integer, nullable=False)          # 1..15
    status = Column(String, nullable=False)             # the 4 storable states
    justification = Column(Text, nullable=True)         # required to exclude
    screening_estimate_tco2e = Column(Float, nullable=True)   # required: not_material
    screening_method = Column(Text, nullable=True)
    materiality_threshold_pct = Column(Float, nullable=True)  # required: not_material
    criteria = Column(Text, nullable=True)              # JSON: all seven relevance criteria
    minimum_boundary_met = Column(Boolean, nullable=True)     # org assertion; cross-checked
    method_description = Column(Text, nullable=True)    # required: included
    calculation_tools = Column(Text, nullable=True)
    primary_data_pct = Column(Float, nullable=True)
    # IFRS S2 ¶B58-B63 (Cat 15 only): the financial institution's gross exposure, so
    # the % of exposure covered by the reported financed emissions can be disclosed.
    gross_exposure_total = Column(Float, nullable=True)
    gross_exposure_currency = Column(String, nullable=True)
    screened_at = Column(String, nullable=False)        # ISO date — drives the 3-year clock
    declared_by = Column(String, nullable=True)
    standard_version = Column(String, nullable=False, default="ghgp-scope3-2011")
    created_at = Column(String, nullable=True)
    updated_at = Column(String, nullable=True)


class RunScope3Declaration(Base):
    """The IMMUTABLE per-run copy of the Scope 3 screen — the completeness artifact.

    compute_co2e writes EXACTLY 15 rows on every run; a category the org never
    screened is frozen as status='undeclared'. The run's statement is therefore
    complete BY CONSTRUCTION: an assurer opening a run sees fifteen statements,
    not an absence they have to notice.
    """
    __tablename__ = "run_scope3_declarations"
    __table_args__ = (
        UniqueConstraint("run_id", "category", name="uq_run_s3decl"),
        CheckConstraint("category >= 1 AND category <= 15", name="ck_run_s3decl_cat"),
        CheckConstraint(
            "status IN ('included','not_applicable','not_material','not_measured','undeclared')",
            name="ck_run_s3decl_status"),
    )
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("calculation_runs.id"), nullable=False)
    category = Column(Integer, nullable=False)
    status = Column(String, nullable=False)            # + the 5th state: 'undeclared'
    declaration_id = Column(Integer, nullable=True)    # provenance only; never read back
    justification = Column(Text, nullable=True)
    screening_estimate_tco2e = Column(Float, nullable=True)
    screening_method = Column(Text, nullable=True)
    materiality_threshold_pct = Column(Float, nullable=True)
    criteria = Column(Text, nullable=True)
    minimum_boundary_met = Column(Boolean, nullable=True)
    method_description = Column(Text, nullable=True)
    calculation_tools = Column(Text, nullable=True)
    primary_data_pct = Column(Float, nullable=True)
    gross_exposure_total = Column(Float, nullable=True)
    gross_exposure_currency = Column(String, nullable=True)
    screened_at = Column(String, nullable=True)
    ghgp_standard_version = Column(String, nullable=False)
    frozen_at = Column(String, nullable=False)


class RunFinancedLine(Base):
    """PCAF financed emissions frozen against an immutable run = GHGP Scope 3 Cat 15.

    NOT an EmissionLineItem: that table requires a non-null activity_id and is keyed
    (run_id, activity_id, method); financed positions are not activities, and
    synthesising one per position would pollute total_activities / mapped /
    coverage_pct / the fingerprint / the resolver / the pedigree DQ score. This is a
    parallel frozen line so a filed run reproduces its Cat 15 even after the live
    loan/investment ledger changes.
    """
    __tablename__ = "run_financed_lines"
    __table_args__ = (
        UniqueConstraint("run_id", "position_id", name="uq_run_financed_line"),
        CheckConstraint("ghgp_category = 15", name="ck_rfl_cat15"),
        CheckConstraint("co2e >= 0", name="ck_rfl_co2e_nonneg"),
    )
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("calculation_runs.id"), nullable=False)
    position_id = Column(Integer, ForeignKey("financed_positions.id"), nullable=False)
    ghgp_category = Column(Integer, nullable=False, default=15)
    co2e = Column(Float, nullable=False)      # KG (PCAF tCO2e x 1000)
    details = Column(Text, nullable=False)    # frozen position_financed() lineage


class EmissionLineItem(Base):
    """One computed emission line, tied to an immutable run (replaces Result).

    Carries the lineage an assurer needs: which run, which activity, scope,
    method (location vs market-based for Scope 2), and a JSON detail blob tracing
    factor id/version, unit conversion and quantity.
    """
    __tablename__ = "emission_line_items"
    __table_args__ = (
        # One line per (run, activity, method) — guards against accumulation and is
        # required once Scope 2 adds a second "market" method per activity (Phase 2c).
        UniqueConstraint("run_id", "activity_id", "method", name="uq_lineitem_run_activity_method"),
    )
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("calculation_runs.id"), nullable=False)
    activity_id = Column(Integer, ForeignKey("activities.id"), nullable=False)
    scope = Column(String)
    method = Column(String, default="location")  # location | market (Scope 2 dual reporting)
    co2e = Column(Float)
    details = Column(Text)  # JSON string of calculation context


# --- Nature (TNFD / SBTN): a separate data model from carbon -----------------
# Nature disclosure is spatial and qualitative, not a single CO2e figure. Sites
# have a location and sensitivity flags (Locate); each carries impacts on and
# dependencies upon nature (Evaluate); the report screens priority interfaces
# (Assess) and reports TNFD core metrics (Prepare). SBTN targets are tracked by
# realm. Deliberately NOT folded into the carbon inventory or its runs.

class NatureSite(Base):
    """A physical location assessed for nature-related issues (TNFD 'Locate').

    Sensitivity is the union of three flags: inside a protected area, inside a
    Key Biodiversity Area (KBA), or in a water-stressed basin (high/extreme).
    """
    __tablename__ = "nature_sites"
    __table_args__ = (
        CheckConstraint("area_hectares >= 0", name="ck_nature_area_nonneg"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    name = Column(String, nullable=False)
    country = Column(String, nullable=True)
    biome = Column(String, nullable=True)         # descriptive: tropical_forest, freshwater, marine, ...
    latitude = Column(Float, nullable=True)
    longitude = Column(Float, nullable=True)
    area_hectares = Column(Float, nullable=False, default=0.0)
    in_protected_area = Column(Boolean, nullable=False, default=False)
    in_kba = Column(Boolean, nullable=False, default=False)
    # unknown | none | low | medium | high | extreme (WRI Aqueduct-style bands)
    water_stress = Column(String, nullable=False, default="unknown")
    created_at = Column(String)


class NatureImpactDependency(Base):
    """One impact on, or dependency upon, nature at a site (TNFD 'Evaluate').

    kind='impact'    -> driver is an IPBES direct driver of nature change.
    kind='dependency'-> driver is an ecosystem service the site relies on.
    materiality is the qualitative screen; metric_value/unit are optional
    quantitative evidence (e.g. m3 of water withdrawn).
    """
    __tablename__ = "nature_impacts_dependencies"
    id = Column(Integer, primary_key=True)
    site_id = Column(Integer, ForeignKey("nature_sites.id"), nullable=False)
    kind = Column(String, nullable=False)          # impact | dependency
    driver = Column(String, nullable=False)        # driver (impact) or ecosystem service (dependency)
    description = Column(Text, nullable=True)
    materiality = Column(String, nullable=False, default="low")   # low | medium | high
    metric_value = Column(Float, nullable=True)
    metric_unit = Column(String, nullable=True)

    site = relationship("NatureSite")


class NatureTarget(Base):
    """A science-based target for nature (SBTN), tracked by realm.

    Direction is not assumed: a freshwater/land target is usually a reduction,
    a restoration target an increase, so the delta is reported signed.
    """
    __tablename__ = "nature_targets"
    __table_args__ = (
        CheckConstraint("target_year >= 2000 AND target_year <= 2100",
                        name="ck_nature_target_year"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    realm = Column(String, nullable=False)         # freshwater | land | ocean | biodiversity
    name = Column(String, nullable=False)
    baseline_value = Column(Float, nullable=False, default=0.0)
    baseline_unit = Column(String, nullable=False)
    baseline_year = Column(Integer, nullable=True)
    target_value = Column(Float, nullable=False, default=0.0)
    target_year = Column(Integer, nullable=False)
    validated = Column(Boolean, nullable=False, default=False)   # SBTN-validated target
    created_at = Column(String)
