from sqlalchemy import Column, Integer, String, Float, Date, ForeignKey, Boolean, Text
from sqlalchemy.orm import relationship
from .database import Base

class Organisation(Base):
    __tablename__ = "organisations"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)
    sector = Column(String, nullable=True)

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
    gwp_set = Column(String) # AR5 or AR6
    value = Column(Float) # kgCO2e per unit
    supersedes_id = Column(Integer, nullable=True)

    activities = relationship("ActivityRecord", back_populates="factor")

class Result(Base):
    __tablename__ = "results"
    id = Column(Integer, primary_key=True)
    activity_id = Column(Integer, ForeignKey("activities.id"))
    co2e = Column(Float)
    details = Column(Text)  # JSON string of calculation context
