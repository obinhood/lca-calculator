import pytest

from app.models import CbamBenchmark, CbamDefaultValue, CbamGood, Organisation
from app.services.cbam import (
    resolve_default, resolve_benchmark, line_embedded, certificates_due,
    cbam_factor, obligation_phase_in, free_allocation_t, CbamResolutionError,
)
from app.reports.cbam import cbam_declaration


def _org(db, name="Importer"):
    o = Organisation(name=name)
    db.add(o); db.commit(); db.refresh(o)
    return o


def _default(db, prefix, category, direct, indirect, year=2026, country=None):
    d = CbamDefaultValue(cn_code_prefix=prefix, good_category=category,
                         direct_t_co2e_per_t=direct, indirect_t_co2e_per_t=indirect,
                         valid_year=year, origin_country=country)
    db.add(d); db.commit(); db.refresh(d)
    return d


def _bench(db, prefix, category, value, year=2026):
    b = CbamBenchmark(cn_code_prefix=prefix, good_category=category,
                      benchmark_t_co2e_per_t=value, valid_year=year)
    db.add(b); db.commit(); db.refresh(b)
    return b


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


def test_cbam_factor_is_the_share_of_free_allocation_retained():
    """Dir. 2003/87/EC Art. 10a(1a) names the RETAINED share the CBAM factor: 97.5% in
    2026 down to 0% in 2034. The complement — the payable share — is reported separately
    so a disclosure can never present one as the other."""
    assert cbam_factor(2025) == 1.0            # transitional: free allocation intact
    assert cbam_factor(2026) == 0.975
    assert cbam_factor(2030) == 0.515
    assert cbam_factor(2034) == 0.0
    assert cbam_factor(2040) == 0.0
    assert obligation_phase_in(2026) == 0.025
    assert obligation_phase_in(2030) == 0.485
    assert obligation_phase_in(2034) == 1.0
    for year in range(2024, 2041):
        assert cbam_factor(year) + obligation_phase_in(year) == pytest.approx(1.0)


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


def test_certificates_subtract_free_allocation_not_scale_by_factor():
    """The free allocation is SUBTRACTED. Scaling by the CBAM factor is only equivalent
    at the benchmark, and understates every dirtier importer everywhere else."""
    # 100 t of a good at 1.9 tCO2e/t against a 1.3 benchmark, in 2026 (factor 0.025).
    fa = free_allocation_t(100.0, 1.3, 2026)                 # 130 x 0.975 = 126.75
    assert fa == pytest.approx(126.75)
    assert certificates_due(190.0, None, 80.0, 2026,
                            free_allocation=fa) == pytest.approx(63.25)
    # The discarded formula would have said 4.75 — 13x lower.
    assert 190.0 * obligation_phase_in(2026) == pytest.approx(4.75)

    # Price deduction still applies pro-rata, on top of the adjusted obligation.
    assert certificates_due(190.0, 40.0, 80.0, 2026,
                            free_allocation=fa) == pytest.approx(31.625)
    assert certificates_due(190.0, 200.0, 80.0, 2026,
                            free_allocation=fa) == pytest.approx(0.0)


def test_at_benchmark_the_two_formulas_agree():
    """The equivalence that made the old formula look right: exactly at benchmark,
    embedded - benchmark x (1-f) == embedded x f, for every phase-in year."""
    for year in (2026, 2028, 2030, 2033, 2034, 2040):
        embedded = 130.0                                     # 100 t x 1.3 = benchmark
        fa = free_allocation_t(100.0, 1.3, year)
        assert certificates_due(embedded, None, 80.0, year, free_allocation=fa) \
            == pytest.approx(embedded * obligation_phase_in(year))


def test_below_benchmark_importer_owes_nothing():
    """A cleaner-than-EU producer has no advantage to equalise — floored at zero, not
    charged a proportional share as the multiplicative form did."""
    fa = free_allocation_t(100.0, 1.3, 2026)
    assert certificates_due(90.0, None, 80.0, 2026, free_allocation=fa) == 0.0


def test_certificates_rise_monotonically_through_the_phase_in():
    prev = -1.0
    for year in (2026, 2027, 2028, 2029, 2030, 2031, 2032, 2033, 2034):
        due = certificates_due(190.0, None, 80.0, year,
                               free_allocation=free_allocation_t(100.0, 1.3, year))
        assert due > prev
        prev = due
    assert prev == pytest.approx(190.0)                      # 2034: no free allocation


def test_transitional_period_and_missing_benchmark():
    # Before the definitive period nothing is surrendered — the subtraction does not
    # encode that on its own (free allocation is 100% but obligation may exceed it).
    assert certificates_due(190.0, None, 80.0, 2025,
                            free_allocation=free_allocation_t(100.0, 1.3, 2025)) == 0.0
    # No benchmark -> abstain, never guess low.
    assert certificates_due(190.0, None, 80.0, 2026, free_allocation=None) is None


def test_benchmark_resolution_is_cn_prefix_only_never_category_wide(db):
    """A benchmark that is too high zeroes the certificate count outright, so a
    category-wide fallback would both understate the obligation and stop the
    missing-benchmark blocker from ever firing. Missing must mean missing."""
    _bench(db, "72", "iron_steel", 2.0)
    narrow = _bench(db, "7208", "iron_steel", 1.3)
    assert resolve_benchmark(db, "72081000", "iron_steel", 2026).id == narrow.id
    assert resolve_benchmark(db, "72081000", "iron_steel", 2025) is None   # vintage
    # A good in the same category but matching no prefix gets NOTHING, not a neighbour's.
    assert resolve_benchmark(db, "99999999", "iron_steel", 2026) is None
    assert resolve_benchmark(db, "99999999", "cement", 2026) is None
    assert resolve_benchmark(db, "", "iron_steel", 2026) is None
    # A prefix match for a DIFFERENT category never applies either.
    assert resolve_benchmark(db, "72081000", "cement", 2026) is None


def test_country_specific_default_preferred_but_never_across_goods(db):
    """Country is a tie-break BEHIND CN-code specificity: a Chinese default for a
    different good must not displace a country-agnostic default for the right one."""
    agnostic_right_good = _default(db, "7208", "iron_steel", 1.9, 0.3)
    cn_specific = _default(db, "7208", "iron_steel", 2.4, 0.4, country="CN")
    _default(db, "72", "iron_steel", 9.9, 9.9, country="CN")     # wrong good, right country
    assert resolve_default(db, "72081000", 2026, "CN").id == cn_specific.id
    # An importer from a country with no specific row still gets the agnostic fallback.
    assert resolve_default(db, "72081000", 2026, "IN").id == agnostic_right_good.id
    # A default published for ANOTHER country never applies.
    _default(db, "2523", "cement", 0.55, 0.05, country="CN")
    assert resolve_default(db, "25231000", 2026, "IN") is None


def test_declaration_totals_and_gates(db):
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)
    _default(db, "2523", "cement", 0.55, 0.05)
    _bench(db, "7208", "iron_steel", 1.3)
    _bench(db, "2523", "cement", 0.5)
    _good(db, org.id, cn="72081000", qty=100.0)                # obligation 190
    _good(db, org.id, cn="25231000", qty=100.0,
          carbon_price_paid_eur_per_t=40.0)                    # obligation 60, half deducted
    d = cbam_declaration(db, org.id, 2026, ets_price_eur_per_t=80.0)
    t = d["totals"]
    assert t["embedded_total_t"] == pytest.approx(280.0)       # 220 + 60
    assert t["obligation_basis_t"] == pytest.approx(250.0)     # 190 + 60
    # free allocation: 100x1.3x0.975 = 126.75, 100x0.5x0.975 = 48.75
    assert t["free_allocation_t"] == pytest.approx(175.5)
    # certificates: (190-126.75) + (60-48.75)x0.5 = 63.25 + 5.625 = 68.875
    assert t["certificates_due_t"] == pytest.approx(68.875)
    assert t["cbam_factor"] == 0.975
    assert t["obligation_phase_in_share"] == 0.025
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


def test_declaration_blocks_when_no_benchmark_loaded(db):
    """Fail CLOSED: with no benchmark the free-allocation adjustment is unknown, and the
    only fallback (obligation x factor) understates every above-benchmark importer. The
    declaration must abstain on the certificate count, not guess low."""
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)              # default but NO benchmark
    _good(db, org.id, cn="72081000", qty=100.0)
    d = cbam_declaration(db, org.id, 2026, ets_price_eur_per_t=80.0)
    assert d["declaration_ready"] is False
    assert any("benchmark" in b for b in d["blockers"])
    assert d["totals"]["certificates_due_t"] is None
    assert d["totals"]["lines_without_benchmark"] == 1
    assert d["totals"]["free_allocation_t"] is None
    assert d["lines"][0]["certificates_due_t"] is None
    # Emissions are still fully reported — only the certificate count abstains.
    assert d["totals"]["embedded_total_t"] == pytest.approx(220.0)


def test_partial_benchmark_coverage_does_not_report_a_short_total(db):
    """One benchmarked line + one not must not yield a total that looks complete."""
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)
    _default(db, "2523", "cement", 0.55, 0.05)
    _bench(db, "7208", "iron_steel", 1.3)                     # cement benchmark missing
    _good(db, org.id, cn="72081000", qty=100.0)
    _good(db, org.id, cn="25231000", qty=100.0)
    d = cbam_declaration(db, org.id, 2026, ets_price_eur_per_t=80.0)
    assert d["declaration_ready"] is False
    assert d["totals"]["certificates_due_t"] is None
    assert d["totals"]["lines_without_benchmark"] == 1


def test_country_agnostic_fallback_is_disclosed_on_the_declaration(db):
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)              # no origin_country
    _bench(db, "7208", "iron_steel", 1.3)
    _good(db, org.id, cn="72081000", qty=100.0)               # origin CN
    d = cbam_declaration(db, org.id, 2026, ets_price_eur_per_t=80.0)
    assert d["lines"][0]["default_country_basis"] == "country_agnostic_fallback"
    assert any("country-agnostic" in n for n in d["notes"])
    # An approximation in the defaults is disclosed, not blocking.
    assert d["declaration_ready"] is True


def test_export_builder_passes_the_ets_price_through(db):
    """The certificate count is the headline CBAM figure; a CSV/PDF export that dropped
    the reference price silently emitted a declaration with no certificates at all."""
    from app.reports.export import build_report
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)
    _bench(db, "7208", "iron_steel", 1.3)
    _good(db, org.id, cn="72081000", qty=100.0)
    payload = build_report(db, org.id, "cbam",
                           {"year": 2026, "ets_price_eur_per_t": "80"})
    assert payload["totals"]["certificates_due_t"] == pytest.approx(63.25)
    assert payload["declaration_ready"] is True


def test_transitional_year_discloses_a_real_zero_not_an_abstention(db):
    """Before 2026 nothing is surrendered by law, so no benchmark is needed to say so.
    A None here would read as 'we could not compute it' when the answer is known."""
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3, year=2025)      # no benchmark at all
    g = _good(db, org.id, cn="72081000", qty=100.0)
    g.import_date = "2025-06-01"; db.commit()
    d = cbam_declaration(db, org.id, 2025, ets_price_eur_per_t=80.0)
    assert d["declaration_ready"] is True
    assert d["blockers"] == []
    assert d["totals"]["certificates_due_t"] == 0.0
    assert d["totals"]["cbam_factor"] == 1.0                     # free allocation intact
    assert d["totals"]["obligation_phase_in_share"] == 0.0
    assert any("TRANSITIONAL" in n for n in d["notes"])
    # And the note must NOT recite a subtraction formula it did not run.
    assert not any("SUBTRACTED" in n for n in d["notes"])
    assert d["totals"]["embedded_total_t"] == pytest.approx(220.0)


def test_benchmark_basis_is_disclosed_per_line(db):
    """Column A (process-related) and Column B (incl. precursors) benchmarks differ by
    roughly 2x and nothing forces the loaded row to match the obligation basis."""
    org = _org(db)
    _default(db, "7208", "iron_steel", 1.9, 0.3)
    b = _bench(db, "7208", "iron_steel", 1.3)
    b.basis = "process-related"; db.commit()
    _good(db, org.id, cn="72081000", qty=100.0)
    d = cbam_declaration(db, org.id, 2026, ets_price_eur_per_t=80.0)
    assert d["lines"][0]["eu_benchmark_basis"] == "process-related"
    assert d["lines"][0]["eu_benchmark_id"] == b.id


def test_default_vintage_outranks_the_country_tie_break(db):
    """A stale country-specific row must never beat a current country-agnostic one —
    the docstring promises latest-vintage, and the country is only a tie-break."""
    _default(db, "7208", "iron_steel", 2.40, 0.4, year=2026, country="CN")
    current = _default(db, "7208", "iron_steel", 3.00, 0.5, year=2030)
    assert resolve_default(db, "72081000", 2030, "CN").id == current.id
    # Same vintage: the country-specific row wins.
    cn_current = _default(db, "7208", "iron_steel", 3.30, 0.6, year=2030, country="CN")
    assert resolve_default(db, "72081000", 2030, "CN").id == cn_current.id
