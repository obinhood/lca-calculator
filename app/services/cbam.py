"""EU CBAM (Carbon Border Adjustment Mechanism) embedded-emissions engine.

Definitive period (from 2026): importers of covered goods (cement, iron/steel,
aluminium, fertilisers, hydrogen, electricity) declare the embedded emissions
of each imported goods line and surrender certificates against them.

Rules encoded here, fail-closed:
  * VERIFIED actual installation values are used when present (basis "actual").
  * UNVERIFIED actuals are never used — CBAM requires accredited verification;
    the line falls back to default values and the substitution is flagged.
  * No default match and no verified actuals -> the line is an ERROR, surfaced
    in the declaration and blocking readiness (never silently skipped).
  * The certificate OBLIGATION differs from reported embedded emissions:
      - indirect emissions enter the obligation only for the goods categories
        listed in Regulation (EU) 2023/956 Annex II (cement, fertilisers,
        electricity); iron/steel, aluminium and hydrogen owe on direct only
        in the initial definitive period — both are still REPORTED.
      - the CBAM factor phases the obligation in as EU ETS free allocation
        phases out (~2.5% of embedded in 2026 rising to 100% by 2034).
      - a carbon price effectively paid in the origin country reduces the
        obligation pro-rata (simplified form, labelled), floored at zero.
Default matching is by LONGEST CN-code prefix for the import year (falling
back to the latest earlier vintage), deterministic; empty/blank prefixes are
never matched (an empty prefix would hijack every unknown CN code).
"""
from typing import Optional

from sqlalchemy.orm import Session

from ..models import CbamBenchmark, CbamDefaultValue, CbamGood

# The CBAM factor by declaration year: the share of EU ETS free allocation still
# RETAINED, which is what the statutory free-allocation adjustment is multiplied by.
# Named per Directive 2003/87/EC Art. 10a(1a) as amended by Reg (EU) 2023/956 — 97.5%
# in 2026 falling to 0% in 2034 — NOT its complement. The platform previously stored
# the complement under this name and disclosed "cbam_factor: 0.025" for 2026, which an
# auditor reading the Directive would have read as an inversion.
CBAM_FACTOR = {
    2026: 0.975, 2027: 0.95, 2028: 0.90, 2029: 0.775, 2030: 0.515,
    2031: 0.39, 2032: 0.265, 2033: 0.14,
}


# First year of the definitive period. Before this, CBAM is reporting-only and NO
# certificates are surrendered — a fact the arithmetic no longer encodes by itself
# now that free allocation is subtracted rather than multiplied in.
DEFINITIVE_PERIOD_START = 2026


def cbam_factor(year: int) -> float:
    """Share of EU ETS free allocation still retained in `year` (Dir. 2003/87/EC 10a(1a))."""
    if year < DEFINITIVE_PERIOD_START:
        return 1.0          # transitional period: free allocation wholly intact
    return CBAM_FACTOR.get(year, 0.0)   # 2034+ -> no free allocation left


def obligation_phase_in(year: int) -> float:
    """Complement of the CBAM factor: the share of the embedded-emissions gap now
    payable. Reported alongside the CBAM factor so neither number can be read as the
    other, and used only for disclosure — the certificate arithmetic subtracts."""
    return round(1.0 - cbam_factor(year), 6)


# Goods whose INDIRECT (electricity) emissions are inside the certificate
# obligation in the initial definitive period (2023/956 Annex II); the others
# owe on direct emissions only, though indirect is still reported.
INDIRECT_IN_OBLIGATION = {"cement", "fertilisers", "electricity"}

# Annual de minimis: consignments up to this total mass per importer per year
# are exempt (2026 simplification package).
DE_MINIMIS_TONNES = 50.0


class CbamResolutionError(ValueError):
    """A goods line has no usable emissions basis (fail-closed)."""


def resolve_default(db: Session, cn_code: str, year: int,
                    origin_country: Optional[str] = None) -> Optional[CbamDefaultValue]:
    """Longest-prefix default for a CN code; latest vintage <= year, else None.

    The Commission publishes defaults per CN code AND country of origin, so a country
    match is preferred — but only as a TIE-BREAK behind CN-code specificity and vintage,
    in that order. A default for a different good is never substituted because the
    country matched, and a stale country-specific row never beats a current one.
    """
    code = (cn_code or "").strip()
    if not code:
        return None
    want = (origin_country or "").strip().upper()
    candidates = db.query(CbamDefaultValue)\
        .filter(CbamDefaultValue.valid_year <= year).all()
    matches = []
    for d in candidates:
        prefix = (d.cn_code_prefix or "").strip()
        if not prefix or not code.startswith(prefix):
            continue
        row_country = (d.origin_country or "").strip().upper()
        if row_country and row_country != want:
            continue          # a default for a DIFFERENT country never applies
        matches.append(d)
    if not matches:
        return None
    matches.sort(key=lambda d: (len((d.cn_code_prefix or "").strip()), d.valid_year,
                                1 if (d.origin_country or "").strip() else 0,
                                d.id), reverse=True)
    return matches[0]


def resolve_benchmark(db: Session, cn_code: str, good_category: Optional[str],
                      year: int) -> Optional[CbamBenchmark]:
    """Longest-prefix EU production benchmark for a CN code; latest vintage <= year."""
    code = (cn_code or "").strip()
    if not code:
        return None
    rows = db.query(CbamBenchmark).filter(CbamBenchmark.valid_year <= year).all()
    # CN-code prefix ONLY. A category-wide fallback would hand a good with no benchmark
    # of its own the benchmark of a different good in the same family — and because a
    # benchmark that is too high zeroes the certificate count outright, that would both
    # understate the obligation and stop the missing-benchmark blocker from ever firing.
    # Missing means missing: return None and let the declaration fail closed.
    matches = [b for b in rows
               if b.cn_code_prefix and b.cn_code_prefix.strip()
               and code.startswith(b.cn_code_prefix.strip())
               and (not good_category or b.good_category == good_category)]
    if not matches:
        return None
    matches.sort(key=lambda b: (len((b.cn_code_prefix or "").strip()),
                                b.valid_year, b.id), reverse=True)
    return matches[0]


def free_allocation_t(quantity_tonnes: float, benchmark_t_co2e_per_t: float,
                      year: int) -> float:
    """Free allocation the equivalent EU producer would still receive, in tCO2e.

    quantity x benchmark x CBAM factor — an EU installation receives benchmark x
    production for free, and the CBAM factor is the share of that still granted in
    `year`. This is what the importer's obligation is reduced BY; see `certificates_due`
    for why that is a subtraction and not a multiplication.
    """
    return quantity_tonnes * benchmark_t_co2e_per_t * cbam_factor(year)


def line_embedded(db: Session, good: CbamGood) -> dict:
    """Embedded emissions + certificate-obligation basis for one goods line."""
    year = int(good.import_date[:4]) if good.import_date else 0
    has_actuals = (good.actual_direct_t_per_t is not None
                   and good.actual_indirect_t_per_t is not None)

    # Category attribution always comes from the CN-code default table, even
    # when verified actuals supply the numbers (the category drives the
    # indirect-in-obligation rule).
    default = resolve_default(db, good.cn_code, year, good.origin_country)
    category = default.good_category if default else None
    # The Commission tables are per CN code AND country; a country-agnostic row is a
    # fallback, not an equivalent, so which one was used is disclosed on the line.
    default_country_basis = None
    if default is not None:
        default_country_basis = ("country_specific" if (default.origin_country or "").strip()
                                 else "country_agnostic_fallback")

    if has_actuals and good.actual_verified:
        direct, indirect = good.actual_direct_t_per_t, good.actual_indirect_t_per_t
        basis = "actual_verified"
        default_used = None
        note = None
        if category is None:
            note = ("no CN-code default match — category unattributed; indirect "
                    "emissions conservatively INCLUDED in the obligation")
    else:
        if default is None:
            raise CbamResolutionError(
                f"no default value for CN {good.cn_code} (year {year}) and no "
                f"verified actual values — line cannot be declared")
        direct, indirect = default.direct_t_co2e_per_t, default.indirect_t_co2e_per_t
        basis = "default"
        default_used = default.id
        note = ("actual values present but NOT verified — defaults substituted "
                "(CBAM requires accredited verification)") if has_actuals else None

    bench = resolve_benchmark(db, good.cn_code, category, year)

    embedded_direct = good.quantity_tonnes * direct
    embedded_indirect = good.quantity_tonnes * indirect
    # Unknown category (verified actuals, no default row) -> conservative: include.
    indirect_in_scope = category in INDIRECT_IN_OBLIGATION if category else True
    obligation = embedded_direct + (embedded_indirect if indirect_in_scope else 0.0)

    return {
        "good_id": good.id,
        "cn_code": good.cn_code,
        "origin_country": good.origin_country,
        "quantity_tonnes": good.quantity_tonnes,
        "basis": basis,
        "good_category": category or "unattributed",
        "default_value_id": default_used,
        # Set whenever a default row was consulted — on a verified-actual line it still
        # supplies the category, which drives the indirect-in-obligation rule.
        "default_country_basis": default_country_basis,
        "direct_t_per_t": direct,
        "indirect_t_per_t": indirect,
        "embedded_direct_t": embedded_direct,
        "embedded_indirect_t": embedded_indirect,
        "embedded_total_t": embedded_direct + embedded_indirect,
        "indirect_in_obligation": indirect_in_scope,
        "obligation_basis_t": obligation,
        "eu_benchmark_t_co2e_per_t": bench.benchmark_t_co2e_per_t if bench else None,
        "eu_benchmark_id": bench.id if bench else None,
        # Column A (process-related) and Column B (including precursors) benchmarks differ
        # by roughly a factor of two, and nothing forces the loaded row to match the
        # obligation basis — so which one was applied is disclosed per line.
        "eu_benchmark_basis": bench.basis if bench else None,
        "free_allocation_t": (free_allocation_t(good.quantity_tonnes,
                                                bench.benchmark_t_co2e_per_t, year)
                              if bench else None),
        "cbam_factor": cbam_factor(year),
        "note": note,
    }


def certificates_due(obligation_t: float, carbon_price_paid_eur_per_t: Optional[float],
                     ets_price_eur_per_t: float, year: int,
                     *, free_allocation: Optional[float]) -> Optional[float]:
    """Certificates due for one line's obligation basis, or None if incomputable.

    certificates = max(0, obligation - free allocation) x (1 - origin carbon price)

    The free-allocation adjustment is SUBTRACTED, not applied as a scaling factor. The
    two are equal only when embedded emissions happen to equal the EU benchmark exactly;
    away from that point they diverge, and in the direction that matters:

        embedded 2.2, benchmark 1.3, 2026 (CBAM factor 0.975 -> 2.5% payable)
          subtract: 2.2 - 1.3 x 0.975 = 0.93 certificates
          multiply: 2.2 x 0.025       = 0.055 certificates   (17x understated)

    A multiplication also erases the policy signal entirely — it makes the bill strictly
    proportional to embedded emissions, so an importer at half the benchmark and one at
    twice it are charged in the same 1:2 ratio instead of zero-versus-substantial.

    Before `DEFINITIVE_PERIOD_START` the result is zero regardless: under the old
    multiplicative form the zero CBAM factor made that fall out of the arithmetic, but a
    subtraction does not, so the transitional period is now gated explicitly.

    `free_allocation` is keyword-only and REQUIRED: passing None means no EU benchmark
    could be resolved for the good, and the answer is None (the caller must block the
    declaration) rather than a silently understated number.
    """
    if year < DEFINITIVE_PERIOD_START:
        return 0.0          # transitional period: reporting only, nothing surrendered
    if free_allocation is None:
        return None
    due = max(0.0, obligation_t - free_allocation)
    if carbon_price_paid_eur_per_t and ets_price_eur_per_t > 0:
        # Simplified pro-rata form of the Art. 9 deduction for a carbon price already
        # effectively paid in the country of origin, labelled as such in the report.
        reduction = min(1.0, carbon_price_paid_eur_per_t / ets_price_eur_per_t)
        due *= (1.0 - reduction)
    return max(0.0, due)
