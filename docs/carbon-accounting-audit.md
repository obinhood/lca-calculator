# Carbon-Accounting Platform — QA & Compliance Audit + Remediation Log

_Living document. Audit performed 2026-07-13; remediation in progress._

Interactive report: <https://claude.ai/code/artifact/74054e9d-dd5b-4e93-ab22-ad242e63f4b9>

## Method

A 114-agent adversarial review: 20 focused reviewers spanning software-engineering
and carbon-accounting/compliance dimensions, **each finding re-verified by an
independent skeptic agent that re-read the cited code**, then a synthesis pass and
a completeness critic. The accuracy-critical core (calc engine, GWP tables, report
freeze/lineage, spend FX, units, CBAM) was additionally read by hand.

**Result:** 88 verified findings (78 confirmed, 10 partially-confirmed), 20 high,
**0 critical, 0 cross-tenant data leaks**. 4 candidate findings were refuted on
re-check and excluded.

## Verdict

The arithmetic core is sound — units fail closed, runs and base-years are
immutable, lineage is frozen, GWP constants are correct, multi-tenancy is solid.
The risk was at the edges: the engine **validated the user's quantity obsessively
but trusted reference data blindly**, and the disclosure gates checked _mapping
status_, not _completeness_. On realistic-but-imperfect input, several reachable
paths emitted a materially wrong number while stamping the report
`disclosure_ready: true`.

## Top risks to footprint accuracy (from the audit)

| # | Risk | Status |
|---|---|---|
| 1 | A "Global" grid factor auto-binds to any country for Scope 2 (~25× overstatement) | ✅ Fixed (PR #4) |
| 2 | An unvalidated `factor.value` (NULL/inf) crashes or poisons the whole run | ✅ Fixed (PR #4) |
| 3 | Unknown activity categories silently classified Scope 3 (steam/refrigerant mis-scoped) | ✅ Fixed (PR #4) |
| 4 | Market-based Scope 2 not grid-matched; residual-mix → location-average | ⬜ Open |
| 5 | Consolidation boundary declared but never applied (a 40%-owned JV counted at 100%) | ⬜ Open |
| 6 | Scope 3 completeness invisible — 3-of-15 categories reads as "100% complete" | ✅ Fixed (PR #7) |
| 7 | Mandatory energy totals silently drop carriers; ESOS had no gate | ✅ Fixed (PR #6) |
| 8 | EN 15804/15978 Module D netted into the headline LCA total (−25%) | ✅ Fixed (PR #6) |
| 9 | A `gwp_set` typo silently zeroes the footprint (or 500s) | ✅ Fixed (PR #4) |

## Entity-level completeness gaps (from the completeness critic)

| Gap | Status |
|---|---|
| A. Scope 3 never structured into the 15 GHG-Protocol categories | ✅ Fixed (PR #7) |
| B. Financed emissions (PCAF = Cat 15) never roll into the entity total | 🚧 In progress (PR #8) |
| C. No inventory line for removals (DAC, biochar, afforestation) | ⬜ Open |
| Temporal straddle proration; float accumulation vs neutrality threshold; GLEC not truly modelled | ⬜ Open |

## Remediation log

- **PR #4** (`a9ffccf`) — _Phase 0: stop wrong numbers._ Fail-closed factor values
  (NULL/inf → `data_errors`, not a crashed/poisoned run) + DB `CHECK(value ≥ 0)`
  and the drifted `market_instruments` rate CHECK; `exact_global` grid-factor
  review gate; expanded + flagged scope classification; `gwp_set` validation +
  case-insensitive vintage compare; reporting-period date validation.
- **PR #6** (`5430c14`) — _Phase 1 gates._ ESOS completeness gate; EN 15804
  Module D excluded from the declared LCA total; staleness fixed both ways
  (period-aware via a shared `activities_in_scope`; a v3 fingerprint that hashes
  date/category/ghgp_category; a versioned "not assessable" for legacy runs;
  factor-drift detection).
- **PR #7** (`c1a63bf`) — _Scope 3 15-category dimension + completeness gate._
  Versioned append-only taxonomy, derivation map, and a completeness gate with
  five honest states and anti-gaming + forgery-by-edit rules; every run freezes
  exactly 15 declaration rows + per-line categories; `summary.inventory_coverage`
  (the value-chain axis, orthogonal to mapping coverage); ESRS/ISSB/CDP/GRI gate
  on it. **Deliberate consequence:** every existing or unscreened run is now
  `disclosure_ready: false` — correct, not a regression.
- **PR #8** (in progress) — _Category 15 = PCAF financed emissions._ Freeze
  financed emissions into the run and roll them into the disclosed entity total
  (for a bank, the majority of the footprint); fix the panel-found `pcaf` `as_of`
  exact-match bug that silently returned a zero portfolio.

## Strengths worth preserving

Fail-closed quantity/unit handling; real immutability and frozen lineage; correct
AR5/AR6 GWP constants; sound per-line pedigree data-quality; solid multi-tenancy
(no cross-tenant IDOR); the fail-closed doctrine already applied by most renderers.
These are the foundation and should not be disturbed while fixing the above.
