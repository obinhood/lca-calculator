"""The factor/GWP layer: per-gas ingestion from a real source, and all 7 Kyoto gases.

Two defects are pinned here.

1. THE AR6 DEFAULT RETURNED A ZERO TOTAL AND CALLED IT "complete".
   ``parse_defra_flat_csv`` is the registered parser for source key "defra" and the
   documented path for the real published DESNZ file. It stored only the aggregate
   CO2e row at ``gwp_set="AR5"``, leaving the per-gas columns NULL. An aggregate
   factor's vintage is baked into its value, so ``compute_co2e`` excludes it when the
   requested set differs — and ``POST /calculate/run`` defaults to AR6. A customer
   who loaded the real DEFRA file and ran with the defaults therefore got
   ``total_co2e: 0.0`` reported as ``status: "complete"``.

2. ONLY THREE OF THE SEVEN KYOTO GASES WERE REPRESENTABLE.
   CO2/CH4/N2O had a GWP and a mass column. SF6 and NF3 had a GWP that no code path
   could reach. HFCs and PFCs had neither — so a fugitive refrigerant emission could
   not be expressed at all, and the EU/UK ETS "annual emissions of the installation"
   figure was wrong by construction for any installation with an F-gas.
"""
from pathlib import Path

import pytest
from sqlalchemy import inspect

from app.models import ActivityRecord, EmissionFactor, FactorGasMass, Organisation
from app.ef_catalog.sources import SOURCES
from app.ef_catalog.loaders.base import FactorRow, load_factors
from app.ef_catalog.loaders.defra import (DEFRA_GWP_SET, parse_defra_flat_csv,
                                          _refrigerant_gas, _rounding_tolerance)
from app.ef_catalog.loaders.generic import parse_generic_csv
from app.services.calc import compute_co2e, factor_gases
from app.services.gwp import (COLUMN_BACKED_GASES, F_GAS_FAMILIES, GAS_BLENDS, GWP_100,
                              KYOTO_FAMILIES, GasStorageError, UnknownGas,
                              co2e_from_gases, gas_family, gas_masses_for_factor,
                              gwp, is_f_gas, is_known_gas)
from app.services.resolver import auto_map_activity, propose_mapping

SAMPLES = Path("app/ef_catalog/samples")


def _defra_rows():
    return SOURCES["defra"].parse((SAMPLES / "defra_flat_sample.csv").read_bytes())


def _org(db, name="Acme Ltd"):
    org = Organisation(name=name)
    db.add(org)
    db.flush()
    return org


def _activity(db, org, category, subcategory, quantity, unit, geo="GB"):
    a = ActivityRecord(organisation_id=org.id, date="2024-06-01", category=category,
                       subcategory=subcategory, quantity=quantity, unit=unit, geo=geo)
    db.add(a)
    db.flush()
    return a


# =============================================================================
# 1. The AR6 zero-total regression
# =============================================================================

def test_ar6_default_on_the_real_defra_path_is_not_a_zero_called_complete(db):
    """THE REGRESSION. Load the DEFRA sample through the REGISTERED parser for source
    key "defra", auto-map as the upload path does, and run with the ``/calculate/run``
    default (AR6). This must never again be a zero total reported as "complete".

    Asserted on the conjunction rather than the total alone, because either half
    failing is the defect: a real figure is the fix, and a run that did exclude lines
    must not call itself complete.
    """
    src = SOURCES["defra"]
    assert src.parse is parse_defra_flat_csv, "source 'defra' must use the DEFRA parser"
    load_factors(db, src.parse((SAMPLES / "defra_flat_sample.csv").read_bytes()),
                 source=src.default_source, version="2024")
    org = _org(db)
    a = _activity(db, org, "Fuels", "Gaseous fuels / Natural gas", 10000.0, "kWh")
    assert auto_map_activity(db, a, gwp_set="AR6") == "auto", (
        "a per-gas DEFRA factor is vintage-free and must bind without review")
    db.commit()

    run = compute_co2e(db, org.id, gwp_set="AR6")
    assert not (run.total_co2e == 0.0 and run.status == "complete"), (
        "AR6 default produced a zero total and called it complete")
    assert run.total_co2e > 0.0
    assert run.status == "complete" and run.mapped == 1 and run.gwp_mismatch == 0


def test_ar5_run_reproduces_the_published_defra_figure_exactly(db):
    """Per-gas masses are recovered by dividing each published per-gas CO2e line by
    the GWP of that gas under DESNZ's own set, using the SAME key the calculation
    recomposes with — so a run under AR5 reproduces the published total exactly, by
    construction. If this drifts, the recovery and the recomposition have diverged."""
    load_factors(db, _defra_rows(), source="DEFRA_DESNZ", version="2024")
    org = _org(db)
    a = _activity(db, org, "Fuels", "Gaseous fuels / Natural gas", 10000.0, "kWh")
    auto_map_activity(db, a, gwp_set="AR5")
    db.commit()
    run = compute_co2e(db, org.id, gwp_set="AR5")
    # 10,000 kWh x the published 0.18293 kg CO2e/kWh.
    assert run.total_co2e == pytest.approx(1829.3, rel=1e-9)


def test_the_gwp_switch_actually_moves_a_real_defra_factor(db):
    """README's central claim — "the GWP set applied at compute time, so AR5/AR6 is a
    switch rather than a re-import" — must hold for a factor from an ACTUAL source,
    not only for hand-made rows. Natural gas is ~99.8% CO2 so the move is small; the
    refrigerant below is where the vintage matters, and both come from the same file."""
    load_factors(db, _defra_rows(), source="DEFRA_DESNZ", version="2024")
    org = _org(db)
    for cat, sub, qty, unit in [("Fuels", "Gaseous fuels / Natural gas", 10000.0, "kWh"),
                                ("Refrigerant & other", "Refrigerants / R410A", 12.0, "kg")]:
        auto_map_activity(db, _activity(db, org, cat, sub, qty, unit), gwp_set="AR6")
    db.commit()
    ar5 = compute_co2e(db, org.id, gwp_set="AR5").total_co2e
    ar6 = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
    assert ar5 != ar6, "the AR5/AR6 switch did not change the number"

    # Both lines move, and the whole difference is accounted for by re-weighting the
    # SAME stored masses — nothing is re-imported. Reconciling the delta exactly is
    # the point: a figure that moved by some OTHER amount would mean the two runs
    # read different masses, not the same masses at a different GWP set.
    gas_f = next(f for f in db.query(EmissionFactor).all() if f.category == "Fuels")
    gas_delta = 10000.0 * (co2e_from_gases(factor_gases(gas_f), "AR6")
                           - co2e_from_gases(factor_gases(gas_f), "AR5"))
    ref_delta = 12.0 * (gwp("R-410A", "AR6") - gwp("R-410A", "AR5"))
    assert ar6 - ar5 == pytest.approx(gas_delta + ref_delta, rel=1e-9)
    # The refrigerant dominates by orders of magnitude: that is why an F-gas being
    # inexpressible, or frozen at one vintage, mattered so much more than a fuel.
    assert ref_delta == pytest.approx(3984.0) and abs(gas_delta) < 1.0


def test_defra_per_gas_rows_are_ingested_as_masses_not_co2e(db):
    """DESNZ publishes per-gas lines as CO2e-WEIGHTED figures. Storing them verbatim
    would re-freeze AR5 into the per-gas columns and make the switch cosmetic. The
    stored value must be the GAS MASS."""
    gas = next(r for r in _defra_rows() if "Natural gas" in r.subcategory)
    # Published: 0.00030 kg CO2e of CH4 per kWh, weighted at DESNZ's AR5 CH4 GWP.
    assert gas.kg_ch4 == pytest.approx(0.00030 / gwp("CH4", DEFRA_GWP_SET))
    assert gas.kg_ch4 < 0.00030, "a CO2e figure was stored where a mass belongs"
    assert gas.kg_co2 == pytest.approx(0.18256)      # CO2 GWP is 1, so mass == CO2e
    assert gas.kg_n2o == pytest.approx(0.00007 / gwp("N2O", DEFRA_GWP_SET))
    # Recomposing under DESNZ's own set returns the published total.
    assert co2e_from_gases({"CO2": gas.kg_co2, "CH4": gas.kg_ch4, "N2O": gas.kg_n2o},
                           DEFRA_GWP_SET) == pytest.approx(gas.value, rel=1e-9)


def test_a_total_is_never_split_into_per_gas_shares(db):
    """The refusal that was always right, and must survive. Rows where DESNZ
    publishes a total and NO per-gas lines keep NULL per-gas columns: there is no way
    to know the split, and most vendors' relabelled DEFRA figures are exactly this
    guess. The vintage stays visible instead."""
    rows = _defra_rows()
    for sub in ("Diesel", "Electricity generated", "Medium car"):
        r = next(x for x in rows if sub in x.subcategory or sub in x.category)
        assert (r.kg_co2, r.kg_ch4, r.kg_n2o) == (None, None, None), (
            f"{sub}: a per-gas split was invented from an aggregate")
        assert r.gas_masses == {}
        assert r.gwp_set == "AR5"


def test_incomplete_per_gas_lines_fall_back_to_aggregate_rather_than_understate():
    """The reconciliation gate. A file missing one gas line must NOT yield a
    breakdown: it would understate the factor by that gas's share, invisibly and in
    the wrong direction. Here the N2O line is dropped from the real natural-gas row
    (0.00007 of 0.18293 — only 0.04%, far inside any percentage tolerance, which is
    why the tolerance is derived from publication precision instead)."""
    csv = (b"Scope,Level 1,Level 2,Level 3,Level 4,Column Text,UOM,GHG/Unit,"
           b"GHG Conversion Factor 2024\n"
           b"Scope 1,Fuels,Gaseous fuels,Natural gas,,,kWh,kg CO2e,0.18293\n"
           b"Scope 1,Fuels,Gaseous fuels,Natural gas,,,kWh,kg CO2e of CO2 per unit,0.18256\n"
           b"Scope 1,Fuels,Gaseous fuels,Natural gas,,,kWh,kg CO2e of CH4 per unit,0.00030\n")
    row = parse_defra_flat_csv(csv)[0]
    assert (row.kg_co2, row.kg_ch4, row.kg_n2o) == (None, None, None)
    assert row.value == pytest.approx(0.18293)   # the published total is kept intact
    assert row.gwp_set == "AR5"


def test_rounding_tolerance_separates_rounding_from_a_missing_gas():
    """The gate's precision, stated as arithmetic: on the real natural-gas row the
    rounding bound must sit BELOW a missing N2O line, or the gate cannot tell them
    apart. A flat percentage rule cannot do this."""
    tol = _rounding_tolerance([5, 5, 5, 5], 3)      # total + three gas lines at 5 d.p.
    assert tol == pytest.approx(0.5 * 1e-5 * 4)
    assert tol < 0.00007, "a missing N2O line would be absorbed as rounding"
    assert tol > 1.5e-5, "pure publication rounding would be rejected"


def test_complete_run_still_reports_complete(db):
    """The status change must not cry wolf: a run that excluded nothing is still
    "complete", or every existing caller learns to ignore the field."""
    load_factors(db, [FactorRow(category="electricity", subcategory="", unit="kWh",
                                value=0.2, geography="GB", year=2024, gwp_set="AR6")],
                 source="X", version="1")
    org = _org(db)
    a = _activity(db, org, "electricity", "", 100.0, "kWh")
    auto_map_activity(db, a, gwp_set="AR6")
    db.commit()
    run = compute_co2e(db, org.id, gwp_set="AR6")
    assert run.status == "complete" and run.total_co2e == pytest.approx(20.0)


def test_an_excluded_line_makes_the_run_say_so_with_a_reason(db):
    """Where a source genuinely publishes only an aggregate, the vintage mismatch
    must stay VISIBLE. A reviewer may bind the AR5 aggregate anyway; the run then
    refuses that line WITH A REASON and does not call itself complete — a dropped
    line reported as a complete zero is the worse outcome."""
    load_factors(db, [FactorRow(category="steam", subcategory="", unit="kWh",
                                value=0.2, geography="GB", year=2024, gwp_set="AR5")],
                 source="X", version="1")
    org = _org(db)
    a = _activity(db, org, "steam", "", 100.0, "kWh")
    a.factor_id = db.query(EmissionFactor).one().id       # reviewer-approved binding
    db.commit()
    run = compute_co2e(db, org.id, gwp_set="AR6")
    assert run.status == "complete_with_exclusions"
    assert run.gwp_mismatch == 1 and run.total_co2e == 0.0
    assert "AR5" in run.notes and "AR6" in run.notes, (
        "the exclusion must carry its reason, not just a count")


def test_aggregate_only_factor_still_routes_to_review_under_a_different_vintage(db):
    """DECISION, recorded: the resolver's vintage fallback is KEPT for aggregate-only
    factors and REMOVED for per-gas ones.

    An aggregate-only AR5 factor cannot produce an AR6 figure, so auto-binding it
    would guarantee a gwp_mismatch exclusion — i.e. auto-binding would manufacture a
    silently zero line. Review is correct there. A per-gas factor has no vintage to
    mismatch, so demoting it was pure loss: it pushed every line of a real DEFRA
    import into the review queue."""
    load_factors(db, [FactorRow(category="steam", subcategory="", unit="kWh",
                                value=0.2, geography="GB", year=2024, gwp_set="AR5")],
                 source="X", version="1")
    hit = propose_mapping(db, "steam", "", None, "GB", gwp_set="AR6")
    assert hit is not None and "vintage_fallback" in hit[1] and hit[2] < 0.95
    org = _org(db)
    a = _activity(db, org, "steam", "", 100.0, "kWh")
    assert auto_map_activity(db, a, gwp_set="AR6") == "needs_review"


def test_per_gas_factor_binds_under_any_vintage(db):
    """The other half of that decision, including the case with NO column-backed mass
    at all: an SF6 factor's only representation is an association row, so a
    columns-only breakdown test would vintage-check a factor that has no vintage."""
    load_factors(db, [FactorRow(category="switchgear", subcategory="", unit="kg",
                                value=24300.0, geography="GB", year=2024, gwp_set="AR6",
                                gas_masses={"SF6": 1.0})],
                 source="X", version="1")
    for requested in ("AR5", "AR6"):
        hit = propose_mapping(db, "switchgear", "", None, "GB", gwp_set=requested)
        assert hit is not None, f"a per-gas factor was filtered out under {requested}"
        assert hit[1] == "exact" and hit[2] == 1.0


def test_sql_breakdown_clause_matches_the_orm_property(db):
    """The resolver expresses has_gas_breakdown in SQL because an ORM property cannot
    be used in a WHERE clause. Two expressions of one rule drift; this pins them
    against each other over every shape of factor, including the negative control."""
    from app.services.resolver import _has_gas_breakdown_clause
    rows = [
        FactorRow(category="agg", subcategory="", unit="kWh", value=1.0),
        FactorRow(category="co2only", subcategory="", unit="kWh", value=1.0, kg_co2=0.5),
        FactorRow(category="ch4only", subcategory="", unit="kWh", value=1.0, kg_ch4=0.5),
        FactorRow(category="n2oonly", subcategory="", unit="kWh", value=1.0, kg_n2o=0.5),
        FactorRow(category="sf6only", subcategory="", unit="kg", value=1.0,
                  gas_masses={"SF6": 1.0}),
        FactorRow(category="mixed", subcategory="", unit="kg", value=1.0, kg_co2=0.1,
                  gas_masses={"HFC-134a": 0.2}),
    ]
    load_factors(db, rows, source="X", version="1")
    sql_ids = {f.id for f in db.query(EmissionFactor)
               .filter(_has_gas_breakdown_clause()).all()}
    orm_ids = {f.id for f in db.query(EmissionFactor).all() if f.has_gas_breakdown}
    assert sql_ids == orm_ids
    # Negative control: the aggregate-only row must be in NEITHER set, or this test
    # would pass just as well over a clause that matched everything.
    agg = db.query(EmissionFactor).filter(EmissionFactor.category == "agg").one()
    assert agg.id not in sql_ids and agg.id not in orm_ids
    assert len(sql_ids) == 5


# =============================================================================
# 2. All seven Kyoto gas families
# =============================================================================

def test_all_seven_kyoto_families_are_representable(db):
    """THE REGRESSION. Each of the seven Kyoto-basket families must have a GWP in
    both sets AND somewhere to put a mass, and must survive a round trip through the
    calculation. Before this, four of the seven failed: SF6/NF3 had a GWP no path
    could reach, HFCs/PFCs had neither."""
    representative = {"CO2": "CO2", "CH4": "CH4_fossil", "N2O": "N2O",
                      "HFC": "HFC-134a", "PFC": "PFC-14", "SF6": "SF6", "NF3": "NF3"}
    assert set(representative) == set(KYOTO_FAMILIES)
    org = _org(db)
    for family, gas in representative.items():
        assert gas_family(gas) == family
        for s in ("AR5", "AR6"):
            assert gwp(gas, s) > 0
        in_column = gas in COLUMN_BACKED_GASES
        row = FactorRow(category=f"src-{family}", subcategory="", unit="kg", value=1.0,
                        geography="GB", year=2024, gwp_set="AR6",
                        ch4_origin="fossil" if gas == "CH4_fossil" else None,
                        kg_co2=1.0 if gas == "CO2" else None,
                        kg_ch4=1.0 if gas == "CH4_fossil" else None,
                        kg_n2o=1.0 if gas == "N2O" else None,
                        gas_masses={} if in_column else {gas: 1.0})
        load_factors(db, [row], source="K", version=family)
        f = db.query(EmissionFactor).filter(EmissionFactor.category == f"src-{family}").one()
        assert f.has_gas_breakdown, f"{family} factor has no reachable gas mass"
        assert factor_gases(f) == {gas: 1.0}
        a = _activity(db, org, f"src-{family}", "", 1.0, "kg")
        a.factor_id = f.id
        db.commit()
        # 1 kg of the gas must yield its GWP, under both sets.
        for s in ("AR5", "AR6"):
            run = compute_co2e(db, org.id, gwp_set=s)
            assert run.total_co2e == pytest.approx(gwp(gas, s)), (family, s)
        a.factor_id = None
        db.commit()


def test_a_refrigerant_leak_is_expressible_and_moves_with_the_vintage(db):
    """The material Scope 1 source that could not previously be expressed at all.
    12 kg of R-410A lost from a chiller is ~23 tonnes CO2e — not zero, and not the
    same number under AR5 and AR6."""
    load_factors(db, _defra_rows(), source="DEFRA_DESNZ", version="2024")
    org = _org(db)
    a = _activity(db, org, "Refrigerant & other", "Refrigerants / R410A", 12.0, "kg")
    assert auto_map_activity(db, a, gwp_set="AR6") == "auto"
    db.commit()
    ar5 = compute_co2e(db, org.id, gwp_set="AR5").total_co2e
    ar6 = compute_co2e(db, org.id, gwp_set="AR6").total_co2e
    assert ar5 == pytest.approx(12 * 1923.5)
    assert ar6 == pytest.approx(12 * 2255.5)
    assert ar6 > ar5 > 20000.0


def test_defra_refrigerant_row_is_stored_as_gas_mass_not_a_frozen_aggregate(db):
    """A DESNZ refrigerant line is "kg CO2e per kg of refrigerant" — which IS that
    refrigerant's GWP under AR5. Stored as an aggregate it would be excluded from
    every AR6 run, which is how fugitive F-gases went missing. Stored as 1 kg of the
    blend, the GWP applies at calculation time."""
    row = next(r for r in _defra_rows() if r.category == "Refrigerant & other")
    assert row.gas_masses == {"R-410A": 1.0}
    assert (row.kg_co2, row.kg_ch4, row.kg_n2o) == (None, None, None)
    load_factors(db, [row], source="DEFRA_DESNZ", version="2024")
    stored = db.query(FactorGasMass).one()
    assert (stored.gas, stored.kg_per_unit) == ("R-410A", 1.0)
    # And it reconciles with the figure DESNZ published for that refrigerant.
    assert gwp("R-410A", DEFRA_GWP_SET) == pytest.approx(row.value, abs=1.0)


def test_unrecognised_refrigerant_stays_aggregate_rather_than_guessing():
    """A designation this platform does not know must NOT be mapped to a
    near-neighbour: refrigerant GWPs differ by thousands, so a wrong match is a
    silent order-of-magnitude error. It stays aggregate-only, which is visible."""
    assert _refrigerant_gas("R-600a") is None          # isobutane: deliberately absent
    assert _refrigerant_gas("R-1234yf") is None        # HFO: not shipped
    assert _refrigerant_gas("") is None
    # ...while the ones it does know resolve, across DESNZ's spelling variants.
    assert _refrigerant_gas("R410A") == "R-410A"
    assert _refrigerant_gas("R-404A") == "R-404A"
    assert _refrigerant_gas("HFC-134a") == "HFC-134a"


def test_blend_gwps_reproduce_published_values():
    """A blend's GWP is DERIVED from its composition at the requested set, so it is
    never frozen at one vintage. Composing the cited component values must reproduce
    the published blend figures, or the compositions are wrong."""
    published_ar5 = {"R-410A": 1923.5, "R-404A": 3942.8, "R-407A": 1923.4,
                     "R-407C": 1624.21, "R-407F": 1674.1, "R-427A": 2024.05,
                     "R-507A": 3985.0, "R-508B": 11698.0}
    for blend, expected in published_ar5.items():
        assert gwp(blend, "AR5") == pytest.approx(expected, abs=0.02), blend
        assert gwp(blend, "AR6") != gwp(blend, "AR5"), blend


def test_every_shipped_blend_is_well_formed():
    """Fractions summing to anything but 1 would mis-scale every line using the
    blend, by a factor nothing downstream can detect."""
    for blend, comp in GAS_BLENDS.items():
        assert sum(comp.values()) == pytest.approx(1.0, abs=1e-9), blend
        assert blend not in GWP_100["AR6"], f"{blend} shadows a pure species"
        for gas in comp:
            assert is_known_gas(gas)
        assert gas_family(blend) in KYOTO_FAMILIES


def test_gwp_tables_are_symmetric_and_f_gases_are_classified():
    """Both sets must define the SAME gases: a gas present in one only would compute
    under one vintage and raise under the other, mid-inventory."""
    assert set(GWP_100["AR5"]) == set(GWP_100["AR6"])
    f_gases = [g for g in GWP_100["AR6"] if is_f_gas(g)]
    assert {gas_family(g) for g in f_gases} == F_GAS_FAMILIES
    # The four families that were previously unrepresentable are all present.
    for fam in ("HFC", "PFC", "SF6", "NF3"):
        assert any(gas_family(g) == fam for g in GWP_100["AR6"]), fam


def test_cited_gwp_values_match_the_ghg_protocol_table():
    """Spot-check against the GHG Protocol "IPCC Global Warming Potential Values"
    v2.0 (7 Aug 2024) table that services/gwp.py cites. A wrong GWP is a silent,
    pervasive, unverifiable error, so the citation is pinned, not just commented.

    SF6 under AR6 is 24,300 here. AR6 Ch.7 is internally inconsistent on that gas
    (main-chapter Table 7.15 prints 25,200; supplementary Table 7.SM.7 prints
    24,300); 24,300 is the GHG Protocol's published figure and the one on EU F-gas
    Regulation 2024/573 labelling. The choice is deliberate and asserted so it
    cannot be changed silently in either direction.
    """
    ar5, ar6 = GWP_100["AR5"], GWP_100["AR6"]
    expected = {                     # gas: (AR5, AR6)
        "CO2": (1.0, 1.0), "N2O": (265.0, 273.0),
        "CH4_fossil": (30.0, 29.8), "CH4_biogenic": (28.0, 27.0),
        "SF6": (23500.0, 24300.0), "NF3": (16100.0, 17400.0),
        "HFC-23": (12400.0, 14600.0), "HFC-32": (677.0, 771.0),
        "HFC-125": (3170.0, 3740.0), "HFC-134a": (1300.0, 1530.0),
        "HFC-143a": (4800.0, 5810.0), "HFC-152a": (138.0, 164.0),
        "HFC-227ea": (3350.0, 3600.0), "HFC-236fa": (8060.0, 8690.0),
        "HFC-245fa": (858.0, 962.0), "HFC-365mfc": (804.0, 914.0),
        "HFC-43-10mee": (1650.0, 1600.0),
        "PFC-14": (6630.0, 7380.0), "PFC-116": (11100.0, 12400.0),
        "PFC-218": (8900.0, 9290.0), "PFC-318": (9540.0, 10200.0),
        "PFC-31-10": (9200.0, 10000.0), "PFC-51-14": (7910.0, 8620.0),
    }
    for gas, (v5, v6) in expected.items():
        assert ar5[gas] == v5, f"{gas} AR5"
        assert ar6[gas] == v6, f"{gas} AR6"
    # HFC-43-10mee is the one halocarbon here revised DOWN from AR5 to AR6; if a
    # transcription slip had flipped a column, this would catch it.
    assert ar6["HFC-43-10mee"] < ar5["HFC-43-10mee"]


# =============================================================================
# Storage invariants
# =============================================================================

def test_gas_masses_table_exists_with_its_constraints(db):
    """The schema the migration creates, asserted rather than assumed."""
    insp = inspect(db.get_bind())
    assert "factor_gas_masses" in insp.get_table_names()
    cols = {c["name"]: c for c in insp.get_columns("factor_gas_masses")}
    assert {"id", "factor_id", "gas", "kg_per_unit"} <= set(cols)
    assert cols["gas"]["nullable"] is False
    assert cols["kg_per_unit"]["nullable"] is False


def test_absent_gas_and_measured_zero_stay_distinguishable(db):
    """NULL is never a no. A gas with NO row was not measured; a gas with a row of
    0.0 was measured and is zero. A column-per-species schema cannot tell these
    apart, which is a main reason this is a table."""
    load_factors(db, [
        FactorRow(category="not-measured", subcategory="", unit="kg", value=1.0,
                  gas_masses={}),
        FactorRow(category="measured-zero", subcategory="", unit="kg", value=1.0,
                  gas_masses={"SF6": 0.0}),
    ], source="X", version="1")
    absent = db.query(EmissionFactor).filter(
        EmissionFactor.category == "not-measured").one()
    zero = db.query(EmissionFactor).filter(
        EmissionFactor.category == "measured-zero").one()
    assert gas_masses_for_factor(absent) == {}
    assert gas_masses_for_factor(zero) == {"SF6": 0.0}
    assert absent.has_gas_breakdown is False
    assert zero.has_gas_breakdown is True, (
        "a measured zero IS a breakdown; treating it as absent would send a factor "
        "that genuinely has no F-gas down the vintage-check path")


def test_one_gas_one_store(db):
    """The column store and the association table must stay disjoint. A gas in both
    would be double counted or dropped depending on read order, and nothing
    downstream could detect either — so the assembler raises instead."""
    f = EmissionFactor(source="X", version="1", category="c", subcategory="", unit="kg",
                       geography="GB", year=2024, gwp_set="AR6", value=1.0, kg_co2=1.0)
    f.gas_masses.append(FactorGasMass(gas="CO2", kg_per_unit=1.0))
    db.add(f)
    db.commit()
    with pytest.raises(GasStorageError):
        gas_masses_for_factor(f)


def test_loader_rejects_rows_whose_gas_masses_are_unloadable(db):
    """Fail-closed at LOAD time, on the same principle as the aggregate `value`: an
    unknown gas identity would otherwise raise mid-calculation, after the catalog had
    been accepted, and a negative mass would net a removal into the gross total."""
    rows = [
        FactorRow(category="ok", subcategory="", unit="kg", value=1.0,
                  gas_masses={"SF6": 1.0}),
        FactorRow(category="unknown-gas", subcategory="", unit="kg", value=1.0,
                  gas_masses={"HFC-999z": 1.0}),
        FactorRow(category="negative", subcategory="", unit="kg", value=1.0,
                  gas_masses={"SF6": -1.0}),
        FactorRow(category="non-finite", subcategory="", unit="kg", value=1.0,
                  gas_masses={"SF6": float("inf")}),
        FactorRow(category="wrong-store", subcategory="", unit="kg", value=1.0,
                  gas_masses={"CO2": 1.0}),
    ]
    res = load_factors(db, rows, source="X", version="1")
    assert res["added"] == 1 and res["skipped"] == 4
    assert db.query(EmissionFactor).one().category == "ok"


def test_unknown_gas_and_set_still_fail_closed():
    with pytest.raises(UnknownGas):
        gwp("XeF6", "AR6")
    with pytest.raises(UnknownGas):
        gas_family("XeF6")
    assert is_known_gas("HFC-134a") and not is_known_gas("HFC-999z")


def test_factors_endpoint_says_whether_the_gwp_stamp_binds():
    """`GET /factors` feeds the mapping-override dropdown. Without `per_gas` a
    reviewer cannot tell an aggregate-only factor (whose vintage WILL exclude it from
    a run on another set) from a per-gas one (which computes under any set) — both
    just show "AR5". A pure F-gas factor is the sharpest case: it looks vintage-locked
    and is not."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.database import Base
    from app import main as main_mod

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    s = Session()

    def override():
        sess = Session()
        try:
            yield sess
        finally:
            sess.close()

    main_mod.app.dependency_overrides[main_mod.get_db] = override
    try:
        s.add(Organisation(name="Factors API Co",
                           api_key_hash=main_mod._hash_key("k-factors")))
        s.commit()
        load_factors(s, [
            FactorRow(category="api-aggregate", subcategory="", unit="kWh", value=0.2,
                      geography="GB", year=2024, gwp_set="AR5"),
            FactorRow(category="api-pergas", subcategory="", unit="kWh", value=0.2,
                      geography="GB", year=2024, gwp_set="AR5", kg_co2=0.2),
            FactorRow(category="api-fgas", subcategory="", unit="kg", value=24300.0,
                      geography="GB", year=2024, gwp_set="AR6",
                      gas_masses={"SF6": 1.0}),
        ], source="APITEST", version="1")
        rows = {f["cat"]: f for f in TestClient(main_mod.app).get(
            "/factors", headers={"X-API-Key": "k-factors"},
            params={"q": "api-", "limit": 50}).json()["factors"]}
    finally:
        main_mod.app.dependency_overrides.clear()
        s.close()

    assert rows["api-aggregate"]["per_gas"] is False
    assert rows["api-aggregate"]["gases"] == []
    assert rows["api-pergas"]["per_gas"] is True
    assert rows["api-pergas"]["gases"] == ["CO2"]
    assert rows["api-fgas"]["per_gas"] is True, (
        "an F-gas factor has no column-backed mass; it must still read as per-gas")
    assert rows["api-fgas"]["gases"] == ["SF6"]


def test_generic_csv_carries_f_gas_masses(db):
    """The operator-facing route to the four families that had no route at all."""
    csv = (b"category,subcategory,unit,value,gwp_set,gas:SF6,gas:HFC-134a\n"
           b"switchgear,,kg,24300,AR6,1.0,\n"
           b"mobile-ac,,kg,1530,AR6,,1.0\n")
    rows = parse_generic_csv(csv)
    assert rows[0].gas_masses == {"SF6": 1.0}
    assert rows[1].gas_masses == {"HFC-134a": 1.0}, "an empty cell must not store 0"
    load_factors(db, rows, source="OP", version="1")
    org = _org(db)
    f = db.query(EmissionFactor).filter(EmissionFactor.category == "switchgear").one()
    a = _activity(db, org, "switchgear", "", 2.5, "kg")
    a.factor_id = f.id
    db.commit()
    run = compute_co2e(db, org.id, gwp_set="AR6")
    assert run.total_co2e == pytest.approx(2.5 * 24300.0)
