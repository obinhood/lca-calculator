"""Emission-factor loader framework.

Source adapters parse a published EF file into normalised ``FactorRow``s; the
loader inserts them under a pinned ``source`` + ``version`` and — because the
inventory is immutable and version-pinned — marks the prior version of each
matching factor as superseded, so the resolver (which excludes superseded rows)
picks the newest automatically without ever mutating a factor in place.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from ...models import EmissionFactor, FactorGasMass
from ...services.gwp import COLUMN_BACKED_GASES, is_known_gas

# Key that identifies "the same factor across versions" for supersession.
_KEY = ("category", "subcategory", "geography", "unit", "gwp_set")


def _gas_masses_ok(gas_masses: Optional[Dict[str, float]]) -> bool:
    """True if every entry is a loadable (gas, mass) pair.

    Fail-closed, for the same reason the aggregate ``value`` is checked: a factor
    carrying an unknown gas identity would raise at CALCULATION time — after the
    catalog was accepted — and a negative or non-finite mass would either net a
    removal into the gross total or poison every line using the factor. Rejecting
    the row at load time puts the failure where an operator can see and fix it.
    """
    import math
    for gas, kg in (gas_masses or {}).items():
        if not gas or gas in COLUMN_BACKED_GASES or not is_known_gas(gas):
            return False
        try:
            m = float(kg)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(m) or m < 0:
            return False
    return True


@dataclass
class FactorRow:
    category: str
    subcategory: str
    unit: str
    value: float
    geography: str = "Global"
    year: int = 0
    gwp_set: str = "AR6"
    kg_co2: Optional[float] = None
    kg_ch4: Optional[float] = None
    kg_n2o: Optional[float] = None
    ch4_origin: Optional[str] = None
    method_type: str = "average_data"
    lca_boundary: Optional[str] = None
    base_year: Optional[int] = None
    price_basis: Optional[str] = None
    # Masses (kg of ACTUAL GAS per activity unit) for every gas WITHOUT a dedicated
    # column: the HFCs, PFCs, SF6 and NF3 that complete the Kyoto basket, plus
    # refrigerant blends by name. Keys are services/gwp.py gas identities. An EMPTY
    # dict means "this source published no other gases"; it is not interchangeable
    # with a gas mapped to 0.0, which means "published, and zero".
    gas_masses: Dict[str, float] = field(default_factory=dict)


def load_factors(session: Session, rows: List[FactorRow], source: str, version: str,
                 supersede: bool = True) -> dict:
    """Insert ``rows`` under (source, version); supersede matching older versions.

    Returns a summary. Fail-closed on obviously bad values (non-finite/negative
    factor values are skipped and counted) rather than poisoning the catalog.

    A row's ``gas_masses`` become ``FactorGasMass`` association rows, which is how
    every gas without a dedicated column (HFCs, PFCs, SF6, NF3) reaches the engine.
    Those are validated on the SAME fail-closed principle as ``value``: an unknown
    gas identity, or a non-finite/negative mass, skips the whole row rather than
    loading a factor whose F-gas component would silently evaluate to zero or net a
    removal into the gross total.
    """
    import math
    added, skipped = [], 0
    for r in rows:
        if r.value is None or not math.isfinite(r.value) or r.value < 0:
            skipped += 1
            continue
        if not _gas_masses_ok(r.gas_masses):
            skipped += 1
            continue
        ef = EmissionFactor(
            source=source, version=version, geography=r.geography, year=r.year,
            category=r.category, subcategory=r.subcategory, unit=r.unit,
            gwp_set=r.gwp_set, value=r.value, kg_co2=r.kg_co2, kg_ch4=r.kg_ch4,
            kg_n2o=r.kg_n2o, ch4_origin=r.ch4_origin, method_type=r.method_type,
            lca_boundary=r.lca_boundary, base_year=r.base_year, price_basis=r.price_basis)
        for gas, kg in (r.gas_masses or {}).items():
            ef.gas_masses.append(FactorGasMass(gas=gas, kg_per_unit=float(kg)))
        session.add(ef)
        added.append((ef, r))
    session.flush()

    n_superseded = 0
    if supersede:
        for ef, r in added:
            prior = session.query(EmissionFactor).filter(
                EmissionFactor.source == source,
                EmissionFactor.version != version,
                EmissionFactor.category == r.category,
                EmissionFactor.subcategory == r.subcategory,
                EmissionFactor.geography == r.geography,
                EmissionFactor.unit == r.unit,
                EmissionFactor.gwp_set == r.gwp_set,
            ).order_by(EmissionFactor.id.desc()).first()
            if prior is not None and prior.id != ef.id:
                ef.supersedes_id = prior.id
                n_superseded += 1

    session.commit()
    return {"source": source, "version": version,
            "added": len(added), "skipped": skipped, "superseded": n_superseded}
