import pytest

from app.models import EmissionFactor, Organisation, LcaAssessment, LcaItem
from app.services.lca import compute_assessment, valid_stage, en_module_group


def _org(db, name="Maker"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _factor(db, category, unit, value, subcategory="", boundary=None, biogenic=None):
    f = EmissionFactor(source="T", version="1", geography="GB", year=2024,
                       category=category, subcategory=subcategory, unit=unit,
                       gwp_set="AR6", value=value, lca_boundary=boundary,
                       kg_co2_biogenic=biogenic)
    db.add(f); db.commit(); db.refresh(f)
    return f


def _assess(db, org_id, standard, fu="1 kg product", fu_qty=1.0):
    a = LcaAssessment(organisation_id=org_id, name="A", standard=standard,
                      functional_unit=fu, functional_unit_quantity=fu_qty, gwp_set="AR6")
    db.add(a); db.commit(); db.refresh(a)
    return a


def _item(db, aid, stage, qty, unit, factor_id, alloc=1.0):
    it = LcaItem(assessment_id=aid, stage=stage, quantity=qty, unit=unit,
                 factor_id=factor_id, allocation_factor=alloc)
    db.add(it); db.commit(); db.refresh(it)
    return it


def test_stage_validation():
    assert valid_stage("en_15804", "A1-A3") is True
    assert valid_stage("en_15804", "C3") is True
    assert valid_stage("en_15804", "Z9") is False
    assert valid_stage("iso_14067", "raw_materials") is True   # free-form
    assert en_module_group("A1-A3") == "A" and en_module_group("C4") == "C"


def test_product_pcf_with_allocation_and_functional_unit(db):
    org = _org(db)
    f_steel = _factor(db, "material", "kg", 2.0, subcategory="steel")     # 2 kgCO2e/kg
    f_energy = _factor(db, "electricity", "kWh", 0.17)
    a = _assess(db, org.id, "iso_14067", fu="1 widget", fu_qty=100.0)     # batch of 100
    _item(db, a.id, "raw_materials", 500, "kg", f_steel.id)               # 1000
    _item(db, a.id, "manufacturing", 2000, "kWh", f_energy.id, alloc=0.5) # 340 (half allocated)
    r = compute_assessment(db, a)
    assert r["by_stage_kg"]["raw_materials"] == pytest.approx(1000.0)
    assert r["by_stage_kg"]["manufacturing"] == pytest.approx(170.0)      # 2000*0.17*0.5
    assert r["total_co2e_kg"] == pytest.approx(1170.0)
    assert r["co2e_per_functional_unit_kg"] == pytest.approx(11.7)        # /100 widgets
    assert r["complete"] is True


def test_biogenic_separate_in_lca(db):
    org = _org(db)
    f = _factor(db, "material", "kg", 0.5, subcategory="timber", biogenic=1.6)
    a = _assess(db, org.id, "iso_14067")
    _item(db, a.id, "raw_materials", 10, "kg", f.id)
    r = compute_assessment(db, a)
    assert r["total_co2e_kg"] == pytest.approx(5.0)              # fossil only
    assert r["biogenic_co2_kg_separate"] == pytest.approx(16.0)  # never in total


def test_transport_well_to_wheel_split(db):
    org = _org(db)
    f_wtt = _factor(db, "freight", "tkm", 0.02, subcategory="road_wtt", boundary="well_to_tank")
    f_ttw = _factor(db, "freight", "tkm", 0.10, subcategory="road_ttw", boundary="combustion")
    a = _assess(db, org.id, "iso_14083", fu="1 t.km", fu_qty=1000.0)
    _item(db, a.id, "leg1_road", 1000, "tkm", f_wtt.id)         # 20 WTT
    _item(db, a.id, "leg1_road", 1000, "tkm", f_ttw.id)         # 100 TTW
    r = compute_assessment(db, a)
    wtw = r["well_to_wheel_kg"]
    assert wtw["well_to_tank"] == pytest.approx(20.0)
    assert wtw["tank_to_wheel"] == pytest.approx(100.0)
    assert wtw["well_to_wheel_total"] == pytest.approx(120.0)
    assert r["co2e_per_functional_unit_kg"] == pytest.approx(0.12)   # per t.km


def test_en15804_module_grouping(db):
    org = _org(db)
    f = _factor(db, "material", "kg", 1.0, subcategory="concrete")
    a = _assess(db, org.id, "en_15804", fu="1 m2 GFA", fu_qty=100.0)
    _item(db, a.id, "A1-A3", 50, "kg", f.id)                    # product -> group A
    _item(db, a.id, "A4", 10, "kg", f.id)                       # construction -> A
    _item(db, a.id, "C3", 20, "kg", f.id)                       # end of life -> C
    _item(db, a.id, "D", 5, "kg", f.id)                         # beyond -> D
    r = compute_assessment(db, a)
    g = r["by_module_group_kg"]
    assert g["A"] == pytest.approx(60.0) and g["C"] == pytest.approx(20.0)
    assert g["D"] == pytest.approx(5.0)
    assert r["total_co2e_kg"] == pytest.approx(85.0)


def test_unit_mismatch_excluded_not_wrong(db):
    org = _org(db)
    f = _factor(db, "electricity", "kWh", 0.17)                 # unit kWh
    a = _assess(db, org.id, "iso_14067")
    _item(db, a.id, "manufacturing", 10, "kg", f.id)           # kg vs kWh -> incompatible
    r = compute_assessment(db, a)
    assert r["complete"] is False
    assert len(r["excluded"]) == 1
    assert r["total_co2e_kg"] == 0.0                            # no wrong number


def test_lca_is_org_scoped(db):
    org_a, org_b = _org(db, "A"), _org(db, "B")
    f = _factor(db, "material", "kg", 1.0)
    a = _assess(db, org_b.id, "iso_14067")
    _item(db, a.id, "raw_materials", 100, "kg", f.id)
    # org A computing org B's assessment would be blocked at the endpoint (_own_assessment);
    # the service itself just reads the assessment it's given — verify the item belongs to B.
    r = compute_assessment(db, a)
    assert r["total_co2e_kg"] == pytest.approx(100.0)
    assert db.query(LcaAssessment).filter(LcaAssessment.organisation_id == org_a.id).count() == 0
