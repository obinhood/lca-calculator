import pytest

from app.models import CbamDefaultValue, CbamGood, Organisation
from app.services.cbam import (
    resolve_default, line_embedded, certificates_due, CbamResolutionError,
)
from app.reports.cbam import cbam_declaration


def _org(db, name="Importer"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _default(db, prefix, category, direct, indirect, year=2026):
    d = CbamDefaultValue(cn_code_prefix=prefix, good_category=category,
                         direct_t_co2e_per_t=direct, indirect_t_co2e_per_t=indirect,
                         valid_year=year)
    db.add(d); db.commit(); db.refresh(d)
    return d


def _good(db, org_id, cn="72081000", qty=100.0, **kw):
    g = CbamGood(organisation_id=org_id, cn_code=cn, quantity_tonnes=qty,
                 origin_country="CN", import_date="2026-03-15", **kw)
    db.add(g); db.commit(); db.refresh(g)
    return g


def test_default_resolution_longest_prefix_wins(db):
    _default(db, "72", "iron_steel", 2.5, 0.5)
    specific = _default(db, "7208", "iron_steel", 1.9, 0.3)
    assert resolve_default(db, "72081000", 2026).id == specific.id
    # no match at all
    assert resolve_default(db, "9999", 2026) is None
    # vintage: only defaults valid <= import year apply
    assert resolve_default(db, "72081000", 2025) is None


def test_line_embedded_default_basis(db):
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)
    g = _good(db, org.id, qty=100.0)
    line = line_embedded(db, g)
    assert line["basis"] == "default"
    assert line["embedded_direct_t"] == pytest.approx(190.0)
    assert line["embedded_indirect_t"] == pytest.approx(30.0)
    assert line["embedded_total_t"] == pytest.approx(220.0)
    assert line["default_value_id"] is not None


def test_verified_actuals_take_precedence(db):
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)
    g = _good(db, org.id, qty=100.0, actual_direct_t_per_t=1.2,
              actual_indirect_t_per_t=0.1, actual_verified=True)
    line = line_embedded(db, g)
    assert line["basis"] == "actual_verified"
    assert line["embedded_total_t"] == pytest.approx(130.0)


def test_unverified_actuals_fall_back_to_default_flagged(db):
    """CBAM requires accredited verification — unverified actuals never count."""
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)
    g = _good(db, org.id, qty=100.0, actual_direct_t_per_t=0.01,
              actual_indirect_t_per_t=0.0, actual_verified=False)
    line = line_embedded(db, g)
    assert line["basis"] == "default"
    assert line["embedded_total_t"] == pytest.approx(220.0)   # NOT the tiny actuals
    assert "NOT verified" in line["note"]


def test_no_basis_fails_closed(db):
    org = _org(db)
    g = _good(db, org.id, cn="99999999")                      # no default seeded
    with pytest.raises(CbamResolutionError):
        line_embedded(db, g)


def test_certificate_deduction_pro_rata_floored():
    assert certificates_due(220.0, None, 80.0) == pytest.approx(220.0)
    assert certificates_due(220.0, 40.0, 80.0) == pytest.approx(110.0)   # half paid
    assert certificates_due(220.0, 200.0, 80.0) == pytest.approx(0.0)    # overpaid -> 0
    assert certificates_due(220.0, 80.0, 80.0) == pytest.approx(0.0)


def test_declaration_totals_and_gates(db):
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)
    _default(db, "7601", "aluminium", 1.5, 5.5)
    _good(db, org.id, cn="72081000", qty=100.0)                          # 220 t
    _good(db, org.id, cn="76011000", qty=10.0,
          carbon_price_paid_eur_per_t=40.0)                              # 70 t, half deducted
    d = cbam_declaration(db, org.id, 2026, ets_price_eur_per_t=80.0)
    t = d["totals"]
    assert t["embedded_total_t"] == pytest.approx(290.0)
    assert t["by_good_category_t"]["iron_steel"] == pytest.approx(220.0)
    assert t["by_good_category_t"]["aluminium"] == pytest.approx(70.0)
    assert t["certificates_due_t"] == pytest.approx(220.0 + 35.0)
    assert d["declaration_ready"] is True


def test_declaration_blocks_on_unresolvable_line_and_missing_price(db):
    org = _org(db)
    _good(db, org.id, cn="99999999")
    d = cbam_declaration(db, org.id, 2026)
    assert d["declaration_ready"] is False
    assert len(d["line_errors"]) == 1
    assert any("no usable emissions basis" in b for b in d["blockers"])
    assert any("ets_price" in b for b in d["blockers"])


def test_declaration_is_year_and_org_scoped(db):
    org_a, org_b = _org(db, "A"), _org(db, "B")
    _default(db, "7208", "iron_steel", 1.9, 0.3)
    _good(db, org_a.id, qty=100.0)                             # 2026
    g_2027 = _good(db, org_a.id, qty=50.0); g_2027.import_date = "2027-01-10"
    _good(db, org_b.id, qty=999.0)                             # other tenant
    db.commit()
    d = cbam_declaration(db, org_a.id, 2026, ets_price_eur_per_t=80.0)
    assert d["totals"]["goods_lines"] == 1
    assert d["totals"]["embedded_total_t"] == pytest.approx(220.0)
