import pytest

from app.models import CbamDefaultValue, CbamGood, Organisation
from app.services.cbam import (
    resolve_default, line_embedded, certificates_due, cbam_factor,
    CbamResolutionError,
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
    assert resolve_default(db, "9999", 2026) is None
    # vintage: only defaults valid <= import year apply
    assert resolve_default(db, "72081000", 2025) is None


def test_empty_prefix_never_hijacks(db):
    """A blank prefix would startswith-match every CN code — must be ignored."""
    _default(db, "", "hijack", 99.0, 99.0)
    _default(db, "   ", "hijack2", 99.0, 99.0)
    assert resolve_default(db, "12345678", 2026) is None


def test_cbam_factor_phase_in():
    assert cbam_factor(2025) == 0.0            # transitional: reporting only
    assert cbam_factor(2026) == 0.025
    assert cbam_factor(2030) == 0.485
    assert cbam_factor(2034) == 1.0
    assert cbam_factor(2040) == 1.0


def test_line_embedded_default_basis(db):
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)
    g = _good(db, org.id, qty=100.0)
    line = line_embedded(db, g)
    assert line["basis"] == "default"
    assert line["embedded_direct_t"] == pytest.approx(190.0)
    assert line["embedded_indirect_t"] == pytest.approx(30.0)
    assert line["embedded_total_t"] == pytest.approx(220.0)
    # Iron/steel: indirect REPORTED but not in the certificate obligation.
    assert line["indirect_in_obligation"] is False
    assert line["obligation_basis_t"] == pytest.approx(190.0)


def test_annex2_goods_owe_on_indirect_too(db):
    org = _org(db)
    _default(db, "2523", "cement", 0.55, 0.05)
    g = _good(db, org.id, cn="25231000", qty=100.0)
    line = line_embedded(db, g)
    assert line["indirect_in_obligation"] is True
    assert line["obligation_basis_t"] == pytest.approx(60.0)   # (0.55+0.05)*100


def test_verified_actuals_take_precedence(db):
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)
    g = _good(db, org.id, qty=100.0, actual_direct_t_per_t=1.2,
              actual_indirect_t_per_t=0.1, actual_verified=True)
    line = line_embedded(db, g)
    assert line["basis"] == "actual_verified"
    assert line["embedded_total_t"] == pytest.approx(130.0)
    assert line["good_category"] == "iron_steel"               # category still attributed
    assert line["obligation_basis_t"] == pytest.approx(120.0)  # direct only


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


def test_certificates_apply_factor_and_price_deduction():
    # 2026: 190 t obligation x 0.025 = 4.75 certificates before deduction.
    assert certificates_due(190.0, None, 80.0, 2026) == pytest.approx(4.75)
    assert certificates_due(190.0, 40.0, 80.0, 2026) == pytest.approx(2.375)
    assert certificates_due(190.0, 200.0, 80.0, 2026) == pytest.approx(0.0)
    # 2034+: full obligation.
    assert certificates_due(190.0, None, 80.0, 2034) == pytest.approx(190.0)
    # Transitional period: no certificates at all.
    assert certificates_due(190.0, None, 80.0, 2025) == pytest.approx(0.0)


def test_declaration_totals_and_gates(db):
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)
    _default(db, "2523", "cement", 0.55, 0.05)
    _good(db, org.id, cn="72081000", qty=100.0)                # obligation 190
    _good(db, org.id, cn="25231000", qty=100.0,
          carbon_price_paid_eur_per_t=40.0)                    # obligation 60, half deducted
    d = cbam_declaration(db, org.id, 2026, ets_price_eur_per_t=80.0)
    t = d["totals"]
    assert t["embedded_total_t"] == pytest.approx(280.0)       # 220 + 60
    assert t["obligation_basis_t"] == pytest.approx(250.0)     # 190 + 60
    # certificates: 190*0.025 + 60*0.025*0.5 = 4.75 + 0.75 = 5.5
    assert t["certificates_due_t"] == pytest.approx(5.5)
    assert d["cbam_factor"] == 0.025
    assert d["declaration_ready"] is True
    # 200 t total mass -> de minimis note must NOT appear.
    assert not any("de minimis" in n for n in d["notes"])


def test_declaration_blocks_on_unresolvable_line_and_missing_price(db):
    org = _org(db)
    _good(db, org.id, cn="99999999")
    d = cbam_declaration(db, org.id, 2026)
    assert d["declaration_ready"] is False
    assert len(d["line_errors"]) == 1
    assert any("unresolvable" in b for b in d["blockers"])
    assert any("ets_price" in b for b in d["blockers"])


def test_malformed_import_date_surfaces_as_error(db):
    """A bad date must not silently drop the line from every year's declaration."""
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)
    g = _good(db, org.id, qty=10.0)
    g.import_date = "15/03/2026"; db.commit()                 # non-ISO
    d = cbam_declaration(db, org.id, 2026, ets_price_eur_per_t=80.0)
    assert d["declaration_ready"] is False
    assert any("unparseable import_date" in e["error"] for e in d["line_errors"])


def test_de_minimis_note_for_small_importers(db):
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)
    _good(db, org.id, qty=20.0)                                # <= 50 t/year
    d = cbam_declaration(db, org.id, 2026, ets_price_eur_per_t=80.0)
    assert any("de minimis" in n for n in d["notes"])


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
