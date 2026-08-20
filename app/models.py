from sqlalchemy import Column, Integer, String, Float, Date, ForeignKey, Boolean, Text, UniqueConstraint, CheckConstraint, Index
from sqlalchemy.orm import relationship
from .database import Base

class Organisation(Base):
    __tablename__ = "organisations"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    sector = Column(String, nullable=True)
    # --- Entity profile: SIZE, FOOTPRINT-OF-OPERATIONS and LISTING STATUS ---------------
    # These decide which disclosure regimes COMPEL a filing (services/applicability.py).
    # They are deliberately NOT frozen onto calculation runs, unlike `sector`: "which
    # sector was my screening challenged against" is a property of a past run, whereas
    # "what must I file" is a live question about the entity as it is today. Freezing it
    # would answer last year's question with last year's headcount.
    employees = Column(Integer, nullable=True)             # average FTE over the year
    annual_turnover = Column(Float, nullable=True)         # NET turnover / revenue
    balance_sheet_total = Column(Float, nullable=True)     # gross assets
    financials_currency = Column(String, nullable=True)    # ISO 4217 for both figures above
    financials_as_of = Column(String, nullable=True)       # ISO date the figures describe
    # JSON list of jurisdiction codes where the entity operates, is established, or is
    # listed — a group can be in scope of several regimes at once, so this is not a
    # single "home country".
    jurisdictions = Column(Text, nullable=True)
    # Where the entity's securities trade, if anywhere: several regimes key off listing
    # rather than size. JSON list; empty/absent means unlisted.
    listed_markets = Column(Text, nullable=True)
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
    date = Column(String)  # ISO date — the single point the record is attributed by
    # The CONSUMPTION WINDOW this record covers, when it is a period rather than a point
    # (a supply invoice spanning a month, a meter read between two dates). Both NULL =
    # attribute wholly by `date`, exactly as before — so every pre-existing activity and
    # every filed run is unchanged. When declared and the window straddles a reporting
    # period boundary, the quantity is PRORATED by the overlap so the emissions land in
    # the year they occurred instead of wholly in whichever year `date` happens to fall.
    coverage_start = Column(String, nullable=True)   # ISO
    coverage_end = Column(String, nullable=True)     # ISO, inclusive
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
    # GHG Protocol scope ("1"|"2"|"3"). EXPLICIT PREPARER DECLARATION ONLY — compute_co2e
    # must never write a derived scope back here (same doctrine as ghgp_category below).
    # A write-back was re-read as scope_source="explicit" on the next run, which made a
    # machine guess indistinguishable from a declaration, silently dropped the
    # assumed-scope caveat from a run whose data had not changed, and put the activity
    # permanently beyond the reach of a later SCOPE_RULES correction.
    # NULL (the normal case) = derive per run from the category; the derived value is
    # frozen on EmissionLineItem.scope with details["scope_source"] beside it.
    scope = Column(String)
    # PREPARER-DECLARED series identity for period-over-period screening. NULL is
    # the normal case and means "not enrolled" — it is NEVER written back by the
    # engine, for the same reason `scope` and `ghgp_category` are not: a derived
    # value written here would be indistinguishable from a declaration on the next
    # run, and would put the row beyond the reach of a later correction.
    #
    # Why declared and not inferred: nothing else on this row identifies a meter or
    # a site. The shipped demo separates HQ from workshop electricity by description
    # string alone, so a key inferred from category+subcategory+geo+entity would
    # merge two physical sites and report their sum as one trend — a real jump at
    # one site would vanish into a flat total, and a site opening would read as a
    # data error.
    series_key = Column(String, nullable=True, index=True)
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


class ResidualMixRate(Base):
    """A PUBLISHED residual-mix rate for one market and year — global reference data.

    GHG Protocol Scope 2 Guidance: under the market-based method, consumption NOT covered
    by a contractual instrument must be priced at the RESIDUAL MIX — the grid average with
    the attributes other purchasers have already claimed removed. Residual mix is therefore
    always >= the grid average, so pricing uncovered load at the plain grid average double
    counts those attributes and UNDERSTATES the market-based figure.

    Published per market/year by AIB (Europe) and Green-e (US) — reference data of exactly
    the kind FxRate/PriceIndex already are: not per-org, admin-written, corrections
    INSERTed rather than edited. Deliberately NO organisation_id and NO unique constraint:
    the absent unique constraint IS the append-only mechanism (migration 083915258aeb
    dropped uq_fx/uq_price_index for this reason — do not reintroduce it here).

    `status='not_published'` is a first-class, ATTESTED absence: a market where no residual
    mix exists is a fact an assurer needs recorded, not an empty query result.
    """
    __tablename__ = "residual_mix_rates"
    __table_args__ = (
        CheckConstraint("status IN ('published','not_published')", name="ck_rmr_status"),
        # `status` is NOT NULL, so these comparisons are never NULL — no three-valued-logic
        # hole of the kind c8d2e4f6a1b3 had to fix.
        CheckConstraint(
            "(status = 'published' AND kg_co2e_per_kwh IS NOT NULL AND kg_co2e_per_kwh > 0) "
            "OR (status = 'not_published' AND kg_co2e_per_kwh IS NULL)",
            name="ck_rmr_rate_entailment"),
        CheckConstraint("gas_basis IN ('co2','co2e')", name="ck_rmr_gas_basis"),
        CheckConstraint("year >= 1990 AND year <= 2100", name="ck_rmr_year"),
        # An asserted absence must carry its attestation, mirroring the 20-char
        # non-boilerplate floor the Scope 3 screen already applies to exclusions.
        CheckConstraint(
            "status = 'published' OR (publication IS NOT NULL "
            "AND length(trim(publication)) >= 20)",
            name="ck_rmr_absence_attested"),
        Index("ix_residual_mix_market_year", "market", "year"),
    )
    id = Column(Integer, primary_key=True)
    # Normalised UPPER market key. NOT NULL on purpose: a market-less residual mix would be
    # a universal rate applied to every grid on earth. (MarketInstrument.market is nullable
    # because an org's own contract may genuinely omit it; published data may not.)
    market = Column(String, nullable=False)
    year = Column(Integer, nullable=False)          # the year the mix is published FOR
    gwp_set = Column(String, nullable=True)         # NULL = the source states none
    status = Column(String, nullable=False)         # published | not_published
    kg_co2e_per_kwh = Column(Float, nullable=True)  # NULL iff status='not_published'
    gas_basis = Column(String, nullable=False, default="co2e")   # co2 | co2e
    # NOT NULL: a rate without a named publisher is an assertion, not reference data —
    # that belongs in MarketInstrument, which already admits instrument_type='residual_mix'.
    publisher = Column(String, nullable=False)
    publication = Column(Text, nullable=True)       # for not_published this IS the attestation
    source_url = Column(Text, nullable=True)
    published_at = Column(String, nullable=True)    # ISO: when the SOURCE published
    recorded_at = Column(String, nullable=True)     # ISO: when WE entered it


class RunResidualMixStatement(Base):
    """The IMMUTABLE per-run Scope 2 residual-mix statement: one row per (market, year)
    the run's electricity touched.

    Complete by construction (the RunEntityBoundary / RunScope3Declaration doctrine):
    markets fully covered by contractual instruments get a row too (status
    'fully_contractual', zeros), so an assurer sees the whole market population rather
    than having to notice an absence.
    """
    __tablename__ = "run_residual_mix_statements"
    __table_args__ = (
        # SQLite treats NULLs as DISTINCT in a unique index, so market_key/year_key use
        # sentinels ('__unknown__' / 0) rather than NULL — a nullable pair would silently
        # admit duplicate statement rows for the same run.
        UniqueConstraint("run_id", "market_key", "year_key", name="uq_run_rm_statement"),
        CheckConstraint(
            "status IN ('fully_contractual','org_instrument','reference_rate',"
            "'not_published','unresolved_no_reference_data','market_unknown',"
            "'year_unknown','unpriceable')",
            name="ck_rms_status"),
        # NULL-SAFE two-branch form: a bare `status = 'x'` yields NULL when status is NULL
        # and SQLite PASSES a NULL CHECK (the c8d2e4f6a1b3 lesson). status is NOT NULL here,
        # and the form is written so it stays definite regardless.
        CheckConstraint(
            "(status IN ('org_instrument','reference_rate') "
            "AND rate_kg_co2e_per_kwh IS NOT NULL) "
            "OR (status NOT IN ('org_instrument','reference_rate') "
            "AND rate_kg_co2e_per_kwh IS NULL)",
            name="ck_rms_rate_entailment"),
    )
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("calculation_runs.id"), nullable=False)
    market_key = Column(String, nullable=False)     # '__unknown__' when the geo is absent
    year_key = Column(Integer, nullable=False)      # 0 when the date is unparseable
    status = Column(String, nullable=False)
    rate_kg_co2e_per_kwh = Column(Float, nullable=True)     # the rate ACTUALLY applied
    # Provenance stamps: plain Integer, NO ForeignKey (the ActivityRecord.entity_id /
    # RunScope3Declaration.declaration_id precedent) — they must survive whatever later
    # happens to the source row, and are what RM-B5 re-checks against live data.
    reference_rate_id = Column(Integer, nullable=True)
    # Frozen EVEN WHEN NOT APPLIED, so an org rate that undercuts the published one is
    # detectable frozen-vs-frozen without a live read at render time.
    reference_rate_kg_co2e_per_kwh = Column(Float, nullable=True)
    instrument_id = Column(Integer, nullable=True)
    gwp_match = Column(String, nullable=True)       # matched | matched_gwp_unstated | unverified
    gas_basis = Column(String, nullable=True)
    publisher = Column(String, nullable=True)
    publication = Column(Text, nullable=True)
    kwh_contractual = Column(Float, nullable=False, default=0.0)
    kwh_priced_at_residual = Column(Float, nullable=False, default=0.0)
    kwh_priced_at_grid = Column(Float, nullable=False, default=0.0)
    grid_rate_avg_kg_per_kwh = Column(Float, nullable=True)
    # Spread of the location grid rates across this market's electricity. When an
    # instrument covers only part of the load and the rest falls back to each line's own
    # grid rate, WHICH line was covered changes the total; the spread bounds that swing.
    grid_rate_min_kg_per_kwh = Column(Float, nullable=True)
    grid_rate_max_kg_per_kwh = Column(Float, nullable=True)
    co2e_at_residual_kg = Column(Float, nullable=False, default=0.0)
    co2e_at_grid_kg = Column(Float, nullable=False, default=0.0)
    # The CONSOLIDATED (boundary-share-weighted) understatement this market still carries.
    # Weighted deliberately: every EmissionLineItem.co2e is share-weighted, so an unweighted
    # gap beside a weighted total is the like-for-like error that bit the Cat 11 check.
    gap_consolidated_co2e_kg = Column(Float, nullable=False, default=0.0)
    # The ORG's own residual rate, unblended. RM-B4 must judge what the org asserted,
    # not a bucket average diluted by reference-priced load sharing the same market.
    org_rate_kg_co2e_per_kwh = Column(Float, nullable=True)
    # A rate exists for this market/year but only under ANOTHER GWP vintage. Without this
    # the preparer is told "no residual mix is on file" and sent to load one that is.
    gwp_vintage_mismatch = Column(Boolean, nullable=False, default=False)
    # Electricity lines whose unit would not convert to kWh: they contribute no quantity,
    # so a zeroed bucket must not read as a clean 'fully_contractual' market.
    unpriceable_lines = Column(Integer, nullable=False, default=0)
    residual_mix_version = Column(String, nullable=False)
    frozen_at = Column(String, nullable=False)


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
    # Where an org-supplied residual_mix rate came from (a supplier letter, a national
    # publication). Free text, no CHECK — its absence is a warning, never a blocker.
    rate_source = Column(String, nullable=True)
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
    # The Commission's default tables are published per CN code AND country of origin — the
    # same good from two countries carries different defaults. NULL = a country-agnostic
    # fallback row, used only when no country-specific row matches (and flagged as such).
    origin_country = Column(String, nullable=True)
    good_category = Column(String, nullable=False)    # iron_steel | aluminium | cement | fertilisers | hydrogen | electricity
    direct_t_co2e_per_t = Column(Float, nullable=False)
    indirect_t_co2e_per_t = Column(Float, nullable=False)
    valid_year = Column(Integer, nullable=False)
    recorded_at = Column(String, nullable=True)


class CbamBenchmark(Base):
    """EU ETS production benchmark for a CBAM good (tCO2e per tonne of product).

    This is the basis of the FREE-ALLOCATION ADJUSTMENT: CBAM equalises an importer with an
    EU producer, who receives free allocation of benchmark x production. The certificate
    obligation is therefore embedded emissions MINUS that adjustment, not embedded emissions
    scaled by the CBAM factor — the two coincide only when embedded emissions happen to equal
    the benchmark exactly. Global reference data (admin-gated writes), append-only.
    """
    __tablename__ = "cbam_benchmarks"
    __table_args__ = (
        CheckConstraint("benchmark_t_co2e_per_t >= 0", name="ck_cbam_benchmark_nonneg"),
    )
    id = Column(Integer, primary_key=True)
    cn_code_prefix = Column(String, nullable=False)
    good_category = Column(String, nullable=False)
    benchmark_t_co2e_per_t = Column(Float, nullable=False)
    # Column A (process-related) vs Column B (including precursors) in the Commission tables.
    basis = Column(String, nullable=True)
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
    # The reporting entity's sector AS AT this run, frozen like every other basis. The
    # sector routes the Scope 3 relevance challenge (services/sectors.py), so reading the
    # organisation's CURRENT sector would let a later profile edit silently change a
    # frozen run's completeness statement. NULL = no sector stated, or a run predating
    # the column; either way no sector challenge ran, and the payload says so.
    organisation_sector = Column(String, nullable=True)
    ghgp_map_version = Column(String, nullable=True)
    # Which factor-boundary ACCEPTANCE VOCABULARY (Table 5.4 token policy) produced this
    # run's per-line minimum-boundary verdicts. Versioned apart from the GHGP standard
    # because the token set is OUR interpretation, not Protocol content. NULL = computed
    # before the policy was versioned; boundary_policy_for_run() reports that as
    # "s3bnd-v1 (inferred)" at render time and never back-fills it into history.
    ghgp_boundary_policy_version = Column(String, nullable=True)
    # Which temporal-basis requirement this run was computed under. NULL = the run
    # PREDATES the requirement, and the gate then only warns (never blocks) — that NULL
    # is the entire anti-cliff mechanism, so it must NEVER be back-filled.
    scope3_temporal_basis_version = Column(String, nullable=True)
    # Which pre-calculation screening policy was in force. NULL = the run PREDATES
    # screening and is never retroactively blocked by it — the same anti-cliff
    # sentinel as the columns around it, and it must NEVER be back-filled.
    screening_version = Column(String, nullable=True)
    # Which Scope 2 residual-mix policy priced this run's uncovered market-based load.
    # NULL = the run PREDATES the requirement and the gate only warns — the anti-cliff
    # mechanism, so it must NEVER be back-filled.
    scope2_residual_mix_version = Column(String, nullable=True)
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
        CheckConstraint(
            "(temporal_basis IS NOT NULL AND temporal_basis = 'sold_units_full_lifetime') "
            "OR (basis_units_sold IS NULL "
            "AND basis_lifetime_years IS NULL AND basis_per_unit_annual_co2e_kg IS NULL)",
            name="ck_s3decl_basis_entailment"),
        CheckConstraint(
            "(basis_units_sold IS NULL OR basis_units_sold > 0) AND "
            "(basis_lifetime_years IS NULL OR basis_lifetime_years > 0) AND "
            "(basis_per_unit_annual_co2e_kg IS NULL OR basis_per_unit_annual_co2e_kg > 0)",
            name="ck_s3decl_basis_positive"),
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
    # --- Temporal basis (GHGP Cats 2/11/12): what the reported figure DENOMINATES ---
    # NULL is the UNSTATED state — deliberately no 'not_stated' sentinel, which would make
    # an absence look like an answer. The three entailed numbers are demanded by exactly
    # one token (`sold_units_full_lifetime`), mirrored by ck_s3decl_basis_entailment.
    # basis_lifetime_years is stored, frozen, rendered and DIVIDED — never multiplied into
    # any emissions figure. No declaration field ever enters the inventory arithmetic.
    temporal_basis = Column(String, nullable=True)
    basis_units_sold = Column(Float, nullable=True)
    basis_lifetime_years = Column(Float, nullable=True)
    basis_per_unit_annual_co2e_kg = Column(Float, nullable=True)
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
        CheckConstraint(
            "(temporal_basis IS NOT NULL AND temporal_basis = 'sold_units_full_lifetime') "
            "OR (basis_units_sold IS NULL "
            "AND basis_lifetime_years IS NULL AND basis_per_unit_annual_co2e_kg IS NULL)",
            name="ck_runs3decl_basis_entailment"),
        CheckConstraint(
            "(basis_units_sold IS NULL OR basis_units_sold > 0) AND "
            "(basis_lifetime_years IS NULL OR basis_lifetime_years > 0) AND "
            "(basis_per_unit_annual_co2e_kg IS NULL OR basis_per_unit_annual_co2e_kg > 0)",
            name="ck_runs3decl_basis_positive"),
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
    # --- Temporal basis, FROZEN copy (see Scope3CategoryDeclaration for the doctrine) ---
    temporal_basis = Column(String, nullable=True)
    basis_units_sold = Column(Float, nullable=True)
    basis_lifetime_years = Column(Float, nullable=True)
    basis_per_unit_annual_co2e_kg = Column(Float, nullable=True)
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


# --- Hourly Scope 2 (GHG Protocol Scope 2 revision: temporal matching) -------
# A PARALLEL method beside annual location- and market-based accounting, never a
# replacement: no existing figure moves. The revision under consultation would
# require energy attribute certificates to be matched to consumption HOURLY and
# within physically deliverable boundaries, so the three tables below carry the
# hour dimension the annual model has no place for.

class GranularCertificate(Base):
    """An hourly-timestamped energy attribute certificate (EnergyTag-shaped).

    Retirement is the anti-double-counting mechanism and it is structural, not a
    check: a certificate carries the ONE period it was retired against, and the
    matcher will not consider a certificate retired against a different period.
    (issuer, certificate_ref) is globally unique so the same certificate cannot be
    loaded twice under two ids and matched twice.

    `kg_co2e_per_kwh` is usually 0 for renewables but is stored rather than
    assumed — a carbon-free-energy claim covering nuclear or biomass is not
    automatically zero-carbon, and hard-coding zero would quietly convert an
    attribute claim into an emissions claim.
    """
    __tablename__ = "granular_certificates"
    __table_args__ = (
        UniqueConstraint("issuer", "certificate_ref", name="uq_gc_issuer_ref"),
        CheckConstraint("kwh > 0", name="ck_gc_kwh_pos"),
        CheckConstraint("kg_co2e_per_kwh >= 0", name="ck_gc_intensity_nonneg"),
        CheckConstraint("production_end > production_start", name="ck_gc_window"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    issuer = Column(String, nullable=False)
    certificate_ref = Column(String, nullable=False)
    # ISO-8601 UTC instants bounding the production window. End is EXCLUSIVE, so an
    # hourly certificate is [13:00, 14:00) and two consecutive hours cannot overlap.
    production_start = Column(String, nullable=False)
    production_end = Column(String, nullable=False)
    kwh = Column(Float, nullable=False)
    technology = Column(String, nullable=True)        # solar | wind | hydro | nuclear | ...
    grid_region = Column(String, nullable=False)      # bidding zone / market the device sits in
    production_device_id = Column(String, nullable=True)
    kg_co2e_per_kwh = Column(Float, nullable=False, default=0.0)
    retired_at = Column(String, nullable=True)
    retired_for_period_id = Column(Integer, ForeignKey("reporting_periods.id"), nullable=True)
    created_at = Column(String)


class HourlyLoad(Base):
    """Metered electricity consumption for one hour, one entity, one grid region.

    A MISSING hour is missing — never a zero. The matcher reports hour coverage and
    refuses to present a partial period's CFE score as a whole-period figure,
    because an unmetered hour silently scored as fully matched (load 0, matched 0)
    would inflate every score toward 100%.
    """
    __tablename__ = "hourly_loads"
    __table_args__ = (
        UniqueConstraint("organisation_id", "entity_id", "metering_point", "hour_start",
                         name="uq_hourly_load_point_hour"),
        CheckConstraint("kwh >= 0", name="ck_hourly_load_nonneg"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    entity_id = Column(Integer, ForeignKey("reporting_entities.id"), nullable=True)
    metering_point = Column(String, nullable=False, default="default")
    hour_start = Column(String, nullable=False)       # ISO-8601 UTC, start of the hour
    kwh = Column(Float, nullable=False)
    grid_region = Column(String, nullable=False)
    source_file = Column(String, nullable=True)
    created_at = Column(String)


class HourlyGridIntensity(Base):
    """Average and residual grid intensity for one region-hour.

    Both are stored because they answer different questions and must never be
    substituted for one another: the average prices a location-based hour, the
    RESIDUAL prices unmatched market-based load once other purchasers' attributes
    are stripped out. Residual is always >= average; the annual engine already
    encodes that rule and it holds hour by hour too.
    """
    __tablename__ = "hourly_grid_intensities"
    __table_args__ = (
        UniqueConstraint("grid_region", "hour_start", "source", "version",
                         name="uq_hourly_intensity_region_hour_source"),
        CheckConstraint("kg_co2e_per_kwh_average >= 0", name="ck_hgi_avg_nonneg"),
    )
    id = Column(Integer, primary_key=True)
    grid_region = Column(String, nullable=False)
    hour_start = Column(String, nullable=False)       # ISO-8601 UTC
    kg_co2e_per_kwh_average = Column(Float, nullable=False)
    kg_co2e_per_kwh_residual = Column(Float, nullable=True)
    source = Column(String, nullable=False)
    version = Column(String, nullable=False, default="1")
    created_at = Column(String)


class DeliverabilityLink(Base):
    """A declared physical-deliverability relationship between two grid regions.

    The proposed Scope 2 revision confines certificates to load they could actually
    reach. Same-region is always deliverable and needs no row; this table records
    the DECLARED exceptions (an interconnector, a combined bidding zone), so every
    cross-region match rests on a stated policy rather than on the matcher's
    goodwill. Direction matters: a link is from the certificate's region TO the
    load's region.
    """
    __tablename__ = "deliverability_links"
    __table_args__ = (
        UniqueConstraint("organisation_id", "from_region", "to_region",
                         name="uq_deliverability_pair"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    from_region = Column(String, nullable=False)      # where the certificate was produced
    to_region = Column(String, nullable=False)        # where the load sits
    basis = Column(String, nullable=False)            # interconnector | single_bidding_zone | ...
    rationale = Column(Text, nullable=True)
    created_at = Column(String)


# --- PACT Pathfinder (WBCSD) product carbon footprint exchange ---------------

class ProductFootprint(Base):
    """A PACT v3 ProductFootprint held by this organisation.

    `direction` separates the two roles a platform plays on the network:
    `received` is a supplier's PCF we consumed and may use as primary data;
    `published` is one of ours, offered to customers. They share a table because
    they share a schema and a lifecycle, and mixing them up is prevented by the
    column rather than by convention.

    `document` is the RECEIVED BYTES, stored verbatim. The denormalised columns
    beside it exist for querying and are derived from it — never the other way
    round. An assuror asking what the supplier actually sent gets the document,
    not our reconstruction of it, and a later spec revision cannot retroactively
    change what we were given.

    v3 versioning is IMMUTABLE: a corrected PCF arrives as a new `pf_id` listing
    the old one in `preceding_pf_ids`, and the old row is marked Deprecated. There
    is no in-place update and deliberately no `version` column — that was v2.
    """
    __tablename__ = "product_footprints"
    __table_args__ = (
        UniqueConstraint("organisation_id", "pf_id", name="uq_pf_org_pfid"),
        CheckConstraint("direction IN ('received','published')", name="ck_pf_direction"),
        CheckConstraint("status IN ('Active','Deprecated')", name="ck_pf_status"),
        CheckConstraint("declared_unit_amount > 0", name="ck_pf_declared_amount_pos"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    direction = Column(String, nullable=False)          # received | published
    pf_id = Column(String, nullable=False)              # the PACT UUID
    spec_version = Column(String, nullable=False)
    status = Column(String, nullable=False, default="Active")
    created = Column(String, nullable=True)             # the sender's timestamp
    preceding_pf_ids = Column(Text, nullable=True)      # JSON array of UUIDs

    company_name = Column(String, nullable=True)
    company_ids = Column(Text, nullable=True)           # JSON array of URNs
    product_name = Column(String, nullable=True)
    product_description = Column(Text, nullable=True)
    product_ids = Column(Text, nullable=True)           # JSON array of URNs

    declared_unit = Column(String, nullable=True)
    declared_unit_amount = Column(Float, nullable=True)
    # Emissions per ONE declared unit, pre-divided at import. The spec quotes the
    # footprint against declaredUnitAmount, not against one unit, and using the
    # undivided figure would overstate by that factor.
    kg_co2e_per_unit_excl_biogenic = Column(Float, nullable=True)
    kg_co2e_per_unit_incl_biogenic = Column(Float, nullable=True)

    reference_period_start = Column(String, nullable=True)
    reference_period_end = Column(String, nullable=True)
    validity_period_start = Column(String, nullable=True)
    validity_period_end = Column(String, nullable=True)
    primary_data_share = Column(Float, nullable=True)
    geography_level = Column(String, nullable=True)     # global | region | country | subdivision
    geography_value = Column(String, nullable=True)
    dqi_technological = Column(Float, nullable=True)
    dqi_geographical = Column(Float, nullable=True)
    dqi_temporal = Column(Float, nullable=True)

    document = Column(Text, nullable=False)             # verbatim, as received
    source_url = Column(String, nullable=True)
    validation_warnings = Column(Text, nullable=True)   # JSON array, frozen at import
    received_at = Column(String, nullable=True)
    created_at = Column(String)


# --- Pre-calculation screening: the assurance exception register -------------
# Built as the MISSTATEMENT LEDGER that ISAE 3410 (50-56) and ISSA 5000 (153-161)
# require a practitioner to assemble by hand, not as an "anomaly detector". Each
# finding carries a stated expectation, the threshold in force, the observation,
# a quantified effect, and an auditable disposition — because PCAOB SAPA 11 is
# explicit that a sign-off alone is not evidence of a control.

class ActivityFinding(Base):
    """One screening exception against an organisation's activity data.

    Identity is `finding_key` — a hash of the check and the activities it
    concerns, never of the detection time or a row id — so re-screening UPDATES a
    finding rather than duplicating it, and a disposition made last month still
    attaches to the same defect today.

    Findings are never deleted. A defect that disappears is marked `superseded`
    and retained: ISAE 3410 para 69 forbids discarding engagement documentation,
    and a register that silently drops cleared items cannot answer "what did you
    know on the day you signed".
    """
    __tablename__ = "activity_findings"
    __table_args__ = (
        UniqueConstraint("organisation_id", "finding_key", name="uq_finding_org_key"),
        CheckConstraint("severity IN ('blocking','high','medium','informational')",
                        name="ck_finding_severity"),
        CheckConstraint("status IN ('open','corrected','accepted','superseded')",
                        name="ck_finding_status"),
        # The vocabulary is closed at the DB, not only at the endpoint — the
        # evidence pack names "no constraints on the severity/status vocabularies"
        # as a gap in the older AssuranceFinding table, and this does not repeat it.
        CheckConstraint(
            "disposition_reason_code IS NULL OR disposition_reason_code IN ("
            "'genuine_operational_change','corrected_at_source','restated_prior_period',"
            "'unit_error_fixed','boundary_change','benchmark_not_applicable',"
            "'accepted_immaterial')",
            name="ck_finding_reason_code"),
        # A disposed finding must carry its reason and its note. A status change
        # with no recorded investigation is exactly the worthless sign-off.
        CheckConstraint(
            "status IN ('open','superseded') OR "
            "(disposition_reason_code IS NOT NULL AND disposition_note IS NOT NULL)",
            name="ck_finding_disposition_complete"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    activity_id = Column(Integer, ForeignKey("activities.id"), nullable=True)
    related_activity_ids = Column(Text, nullable=True)   # JSON array — duplicates/pairs
    finding_key = Column(String, nullable=False)
    check_code = Column(String, nullable=False)
    severity = Column(String, nullable=False)
    status = Column(String, nullable=False, default="open")
    # The three attributes that make a finding audit evidence rather than a score.
    expectation = Column(Text, nullable=False)
    threshold = Column(Text, nullable=False)
    observed = Column(Text, nullable=False)
    # kgCO2e at risk. NULL is NOT zero: it means the effect could not be
    # determined, which is tracked separately and never folded into the total.
    estimated_effect_kg = Column(Float, nullable=True)
    effect_quantifiable = Column(Boolean, nullable=False, default=True)
    disposition_reason_code = Column(String, nullable=True)
    disposition_note = Column(Text, nullable=True)
    dispositioned_at = Column(String, nullable=True)
    screening_version = Column(String, nullable=False)
    detected_at = Column(String, nullable=True)
    created_at = Column(String)


class RunScreeningStatement(Base):
    """The screening state frozen onto one immutable run.

    Advisory by construction: the run is always produced. The engine's contract is
    that every activity lands in a visible bucket and nothing is silently dropped,
    so a gate that refused to produce a run would be the first mechanism here to
    leave NO evidence artifact at all. The blockers are reported at disclosure
    time instead, where a reader sees the figure and the reason to doubt it
    together.
    """
    __tablename__ = "run_screening_statements"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_run_screening_statement"),
        CheckConstraint("findings_total >= 0", name="ck_rss_total_nonneg"),
        CheckConstraint("materiality_pct > 0 AND materiality_pct <= 100",
                        name="ck_rss_materiality_pct"),
    )
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("calculation_runs.id"), nullable=False)
    screening_version = Column(String, nullable=False)
    screened_at = Column(String, nullable=True)     # when the register was last run
    findings_total = Column(Integer, nullable=False, default=0)
    findings_open = Column(Integer, nullable=False, default=0)
    findings_corrected = Column(Integer, nullable=False, default=0)
    findings_accepted = Column(Integer, nullable=False, default=0)
    open_blocking = Column(Integer, nullable=False, default=0)
    accumulated_uncorrected_effect_kg = Column(Float, nullable=False, default=0.0)
    uncorrected_unquantifiable = Column(Integer, nullable=False, default=0)
    materiality_kg = Column(Float, nullable=False, default=0.0)
    materiality_pct = Column(Float, nullable=False, default=5.0)
    exceeds_materiality = Column(Boolean, nullable=False, default=False)
    frozen_at = Column(String, nullable=False)


# --- PACT host side: OAuth2 client credentials and issued tokens -------------

class PactClient(Base):
    """A partner permitted to read this organisation's published footprints.

    Separate from the X-API-Key tenant credential on purpose: the PACT network
    identifies a DATA RECIPIENT, and a partner must be able to be revoked without
    touching the owner's own access.
    """
    __tablename__ = "pact_clients"
    __table_args__ = (
        UniqueConstraint("client_id", name="uq_pact_client_id"),
    )
    id = Column(Integer, primary_key=True)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    client_id = Column(String, nullable=False)
    client_secret_hash = Column(String, nullable=False)
    partner_name = Column(String, nullable=True)
    revoked = Column(Boolean, nullable=False, default=False)
    created_at = Column(String)


class PactToken(Base):
    """A bearer token issued to a PACT client.

    Stored by hash and given an explicit expiry, so a leaked token has a bounded
    life and revocation is a delete rather than a hope.
    """
    __tablename__ = "pact_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_pact_token_hash"),
    )
    id = Column(Integer, primary_key=True)
    client_id = Column(String, nullable=False)
    organisation_id = Column(Integer, ForeignKey("organisations.id"), nullable=False)
    token_hash = Column(String, nullable=False)
    expires_at = Column(String, nullable=False)     # ISO-8601 UTC
    created_at = Column(String)


# --- Versioned classification crosswalks -------------------------------------

class Crosswalk(Base):
    """A versioned concordance table between two classification schemes.

    Version-pinned because a frozen report must freeze its crosswalks exactly as
    it freezes FX rates: NACE Rev.2.1 became mandatory for EU statistics in 2025,
    ISIC Rev.5 and CPC 3.0 exist with thin correspondence coverage, and UNSPSC
    governance moved from GS1 US to UNDP on 1 January 2025. A concordance revision
    would otherwise silently move a filed figure.

    `uncitable` marks a hop for which no authoritative table exists — a direct
    UNSPSC-to-industry mapping is the case: UNSPSC classifies the PRODUCT bought
    while NAICS/NACE/ISIC classify the ESTABLISHMENT that produced it, and every
    table on offer is commercial or machine-generated.
    """
    __tablename__ = "crosswalks"
    __table_args__ = (
        UniqueConstraint("from_scheme", "to_scheme", "table_version",
                         name="uq_crosswalk_scheme_version"),
    )
    id = Column(Integer, primary_key=True)
    from_scheme = Column(String, nullable=False)
    to_scheme = Column(String, nullable=False)
    source = Column(String, nullable=False)          # publisher
    table_version = Column(String, nullable=False)
    licence = Column(String, nullable=True)
    url = Column(String, nullable=True)
    uncitable = Column(Boolean, nullable=False, default=False)
    created_at = Column(String)


class CrosswalkMapping(Base):
    """One row of a concordance.

    `partial` records the asterisk or free-text qualifier the source table carried:
    93.7% of ISIC Rev.4 to NAICS 2017 rows are flagged partial and 56.3% carry a
    note. Such a row is NOT resolvable by lookup alone, and recording it as a clean
    correspondence is how crosswalk error becomes invisible.
    """
    __tablename__ = "crosswalk_mappings"
    __table_args__ = (
        UniqueConstraint("crosswalk_id", "from_code", "to_code",
                         name="uq_crosswalk_mapping"),
    )
    id = Column(Integer, primary_key=True)
    crosswalk_id = Column(Integer, ForeignKey("crosswalks.id"), nullable=False)
    from_code = Column(String, nullable=False, index=True)
    to_code = Column(String, nullable=False)
    partial = Column(Boolean, nullable=False, default=False)
    note = Column(Text, nullable=True)
