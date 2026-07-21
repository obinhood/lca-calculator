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
| 4 | Market-based Scope 2 not grid-matched; residual-mix → location-average | ✅ Fixed (PR #10) — grid matching + determinism |
| 5 | Consolidation boundary declared but never applied (a 40%-owned JV counted at 100%) | ✅ Fixed (PR #12) |
| 6 | Scope 3 completeness invisible — 3-of-15 categories reads as "100% complete" | ✅ Fixed (PR #7) |
| 7 | Mandatory energy totals silently drop carriers; ESOS had no gate | ✅ Fixed (PR #6) |
| 8 | EN 15804/15978 Module D netted into the headline LCA total (−25%) | ✅ Fixed (PR #6) |
| 9 | A `gwp_set` typo silently zeroes the footprint (or 500s) | ✅ Fixed (PR #4) |

## Entity-level completeness gaps (from the completeness critic)

| Gap | Status |
|---|---|
| A. Scope 3 never structured into the 15 GHG-Protocol categories | ✅ Fixed (PR #7) |
| B. Financed emissions (PCAF = Cat 15) never roll into the entity total | ✅ Fixed (PR #9) |
| C. No inventory line for removals (DAC, biochar, afforestation) | ✅ Fixed (PR #14) |
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
- **PR #9** — _Category 15 = PCAF financed emissions._ Freeze financed emissions
  into the run (`RunFinancedLine`) and roll them into the disclosed entity total in
  ESRS/ISSB/CDP (for a bank, the majority of the footprint) while `run.total_co2e`
  and the pedigree data-quality score stay untouched; fixed the `pcaf` `as_of`
  exact-match bug that silently returned a zero portfolio. A pre-merge adversarial
  review caught three more issues — a HIGH re-introduction of the silent-zero in
  the freeze block (a filtered-empty `as_of` froze `financed_co2e = 0`), a
  staleness-fingerprint false-positive, and a `scope3_gross` double-count — all
  fixed with regression tests.

- **PR #10** — _Market-based Scope 2 grid matching + determinism._ A contractual
  instrument now only covers consumption on its declared `market` (a US REC can no
  longer zero German load); a NULL market still applies but flags
  `kwh_market_unverified` (backward compatible); the activity set is id-ordered so
  the market total is deterministic. A pre-merge adversarial review confirmed the
  change is sound (one low-severity disclosure-consistency nit, fixed).

- **PR #12** — _GHG Protocol Ch.3 organisational boundary applied._ The declared
  consolidation approach now decides what share of each entity enters the inventory
  (a 40% JV consolidates at 40%, not 100%). `accounting_category` appears in no
  weight branch — the same 20% associate is 100% or 0% purely on asserted control
  (IFRS S2 Ex. 2A/2B). The share is applied per LINE so `sum(lines) == total_co2e`
  holds by construction; the boundary is frozen per run (`run_entity_boundary`) so a
  later ownership edit is detected drift, never a silent restatement. Fail-open on
  the number (an unresolved fact includes at 100% — never understate) and fail-closed
  on the disclosure. The excluded residual is measured and blocks, but is never
  auto-routed to Scope 3. Designed by a 3-way panel + judge; a pre-merge adversarial
  review caught six issues (incl. a falsy NULL check that silently excluded a 50/50
  JV at 0%, and gross energy disclosed beside consolidated emissions) — all fixed.

- **PR #13** — _GHG Protocol Ch.5 base-year recalculation detection._ An SBTi
  trajectory measured across two different organisational boundaries is meaningless
  (like measuring across GWP vintages). The SBTi report now blocks the trajectory when
  a structural change — approach change, entity acquisition/divestment, or ownership
  restatement — occurred between the base year and now, and forces a re-base. Organic
  growth (same entities, more activity) is correctly comparable; detection reads only
  frozen run state, and a filed run is never restated. Closes the follow-up the
  consolidation panel flagged as needing to land immediately after PR #12.

- **PR #14** — _Inventory removals (GHG Protocol Land Sector & Removals)._ A fourth
  frozen pool (`RemovalRecord` / `RunRemovalLine` / `total_removals_co2e`) for the
  org's OWN within-boundary sequestration — technological or land-based — reported
  SEPARATELY from gross emissions (never in `total_co2e`; net is render-time only),
  distinct from purchased offset credits and from biogenic CO2. Weighted by the
  organisational-boundary entity share; reversals reduce the current period's net
  without restating a filed run. Fail-closed gate: a land-based removal with no
  monitoring/reversal basis blocks, a tonne also sold as a credit blocks, permanence
  is never overclaimed. Designed by a 3-way panel + judge; a pre-merge adversarial
  review caught two gate defects (a period-scope mismatch that false-blocked a
  fiscal-year-rollover run, and a post-filing-sale escape) — both fixed.

- **PR #15** — _IFRS S2 ¶29(a)(iv) per-scope entity disaggregation._ Repays the debt
  flagged in PR #12: the boundary froze one all-scope figure per entity, and the
  disaggregation reported that number under a "Scope 1+2 split" label — a clause it did
  not satisfy. Scope 1 and Scope 2 (location-based) are now frozen per entity on
  `run_entity_boundary` and disaggregated between the consolidated accounting group and
  other investees, so ¶29(a)(iv) is actually met. Scope 2 is fed from the location line
  once (no market double-count); the headline `total_co2e` is unchanged (frozen columns
  + render-time split only). Legacy runs (NULL columns) fall back to the all-scope figure
  with `scope_split_available=False` rather than a silent Scope 1/2 = 0 — fail-closed on
  disclosure, reproduction contract preserved. Migration additive/nullable/reversible;
  single head, zero drift. A pre-merge 3-lens adversarial review returned zero findings.

- **PR #17** — _DEFRA factor `lca_boundary` backfill: Table 5.4 gets teeth._ The DEFRA
  loader hardcoded `lca_boundary=None` on every row, so every DEFRA-derived factor was
  "boundary not assessable": the Scope 3 gate could only warn (W1) and the Table 5.4
  minimum-boundary check (B12) had nothing to compare against. The boundary is now
  derived from DEFRA's published (Scope, Level 1) structure for the Scope 3 tables —
  `WTT-*`/T&D → `well_to_tank`/`td_loss` (Cat 3), waste → `waste_treatment` (Cat 5/12),
  business travel/freighting → `ttw` (Cat 4/6/7/9…), material use → `cradle_to_gate`
  (Cat 1/2) — and left `None` (honest W1) wherever the structure is ambiguous, mirroring
  `boundary_meets_minimum`'s "never silently True" doctrine. No taxonomy edit (it is
  append-only/frozen) and no gate-semantics change. A pre-merge 3-lens adversarial review
  CONFIRMED a false-block regression in an earlier draft: a factor is scope-AGNOSTIC, so
  deriving `combustion`/`generation` for Scope-1 fuel and Scope-2 grid factors would be
  rejected by the other scope1_2-family categories (Cat 8/13/14 reject `combustion`;
  Cat 4/6/7/9 reject `generation`) whenever such a factor is legitimately used on a
  Scope-3 leased-asset/franchise/EV line — turning a safe W1 into a false B12 block of a
  COMPLIANT disclosure. Fixed by leaving those factors boundaryless; captured as a
  regression test. The false-pass and faithfulness lenses returned zero findings.

## Strengths worth preserving

Fail-closed quantity/unit handling; real immutability and frozen lineage; correct
AR5/AR6 GWP constants; sound per-line pedigree data-quality; solid multi-tenancy
(no cross-tenant IDOR); the fail-closed doctrine already applied by most renderers.
These are the foundation and should not be disturbed while fixing the above.
