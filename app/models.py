from sqlalchemy import Column, Integer, String, Float, Date, ForeignKey, Boolean, Text, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import relationship
from .database import Base

class Organisation(Base):
    __tablename__ = "organisations"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    sector = Column(String, nullable=True)
    # SHA-256 hash of the org's API key (the plaintext key is returned exactly
    # once at registration and never stored).
    api_key_hash = Column(String, unique=True, nullable=True, index=True)
    # GHG Protocol consolidation approach: operational_control | financial_control | equity_share.
    # NOTE: stored for provenance but NOT yet wired into the calc engine — every run currently
    # includes 100% of the org's own activities. Multi-entity roll-up / equity-share weighting
    # (which needs a parent/child org hierarchy + ownership %) is future work; do not assume live.
    consolidation_approach = Column(String, nullable=True, default="operational_control")

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

    factor = relationship("EmissionFactor", back_populates="activities",
                          foreign_keys=[factor_id])
    suggested_factor = relationship("EmissionFactor", foreign_keys=[suggested_factor_id])

class EmissionFactor(Base):
    __tablename__ = "emission_factors"
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
