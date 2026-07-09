from sqlalchemy import Column, Integer, String, Float, Date, ForeignKey, Boolean, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from .database import Base

class Organisation(Base):
    __tablename__ = "organisations"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    sector = Column(String, nullable=True)
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
    scope = Column(String)  # 1,2,3 - set later
    mapping_confidence = Column(Float)  # 0-1
    factor_id = Column(Integer, ForeignKey("emission_factors.id"), nullable=True)
    provenance = Column(String)  # process/eeio/hybrid

    factor = relationship("EmissionFactor", back_populates="activities")

class EmissionFactor(Base):
    __tablename__ = "emission_factors"
    id = Column(Integer, primary_key=True)
    source = Column(String)  # DEFRA2024 (demo), etc.
    version = Column(String) # 2024.1
    geography = Column(String) # GB, EU, Global
    year = Column(Integer)
    category = Column(String) # electricity, diesel, flight, etc.
    subcategory = Column(String) # tech / route
    unit = Column(String) # per kWh, per L, per tkm, per pkm, per kg
    gwp_set = Column(String) # GWP vintage baked into `value` (aggregate factors only)
    value = Column(Float) # kgCO2e per unit (pre-aggregated fallback)
    # Per-gas decomposition: kg of ACTUAL GAS emitted per activity unit. When set,
    # the calc engine applies the requested GWP set at CALCULATION time
    # (co2e = kg_co2*1 + kg_ch4*GWP(CH4) + kg_n2o*GWP(N2O)) — this is what makes
    # the AR5/AR6 switch real. When NULL, `value` is used with a gwp_set check.
    kg_co2 = Column(Float, nullable=True)
    kg_ch4 = Column(Float, nullable=True)
    kg_n2o = Column(Float, nullable=True)
    supersedes_id = Column(Integer, nullable=True)

    activities = relationship("ActivityRecord", back_populates="factor")

    @property
    def has_gas_breakdown(self) -> bool:
        return any(v is not None for v in (self.kg_co2, self.kg_ch4, self.kg_n2o))

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
    total_co2e = Column(Float, default=0.0)
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
