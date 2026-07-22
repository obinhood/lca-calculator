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
| Float accumulation vs neutrality threshold | ✅ Fixed (PR #27) |
| Temporal straddle proration | ✅ Fixed (PR #29) |
| GLEC not truly modelled | ⬜ Open |

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

- **PR #19** — _Table 5.4 acceptance vocabulary versioned apart from the standard._ The
  per-category accepted factor-boundary tokens were ASYMMETRIC across categories sharing the
  same `<party>_scope1_2` minimum (Cat 4/6/7/9 took `combustion` not `generation`; Cat 8/13/14
  the reverse; Cat 10 neither). Since a factor is scope-AGNOSTIC, that false-blocked compliant
  lines — the defect PR #17's review surfaced. `accepts_boundary` is OUR reading of the
  Protocol's prose minimum, not Protocol content, so it is lifted into an append-only,
  separately versioned `BOUNDARY_POLICIES` (`s3bnd-v2`), leaving `GHGP_TAXONOMIES` byte-identical
  and `GHGP_STANDARD_VERSION` unbumped (bumping it would claim the Protocol re-cut its
  categories and would restamp every declaration). v1 is the taxonomy verbatim, proven at
  import; v2 gives the whole scope1_2 family one direct-operational tier COMPOSED from each
  category's own `min_boundary`, so the asymmetry cannot be re-typed. Cat 1/2, Cat 3 and Cat
  11/15 inherit unchanged. Import-time proofs pin v1==taxonomy, v1 subset-of v2 (monotone: no
  filed line gains a blocker) and no upstream-only token in any scope1_2 set (the false-pass
  guard). The verdict's INPUT is now frozen beside it (policy version, normalised token,
  verdict basis) so a verdict is re-derivable without joining the live factor table. Designed
  by a 3-way panel + judge (which REJECTED a rival design for adding `cradle_to_gate` to Cat 3
  — the catalogue's most common token asserting conformance on an upstream-only bar). A 3-lens
  adversarial review raised 10, confirmed 3 (zero false-pass), all fixed: a MEDIUM where the
  policy composed from the LIVE standard version, so the append-only extension the module
  prescribes would silently rewrite a filed policy (now pinned per-version to its cut); two
  LOWs on un-versioned token normalisation (loaders strip at ingest; the replay guarantee is
  stated as token-set identity); and integrity proofs moved off `assert` (stripped by `python -O`).

- **PR #21** — _DEFRA Scope-1/2 boundaries restored + the ISO 14083 wheel-side split fixed._
  Completes the factor-boundary backfill: PR #17 had to withhold `combustion` (Scope 1 fuels)
  and `generation` (Scope 2 grid) because the then-current vocabulary false-blocked them; the
  s3bnd-v2 policy removed that block, so they are derived again. Cat 1/2/3 still reject them —
  a TRUE block (a combustion factor is not a cradle-to-gate goods or upstream-WTT figure).
  The pre-merge review then confirmed a MEDIUM defect in a SECOND consumer of the same tokens:
  `lca.py` summed the ISO 14083 tank-to-wheel figure from `combustion` + `tank_to_wheel` only,
  never `ttw` — the exact token the DEFRA adapter returns for every travel/freight table. The
  omission was pre-existing, but this change would have made it far worse: a DEFRA-sourced
  chain used to report `tank_to_wheel: 0.0` (loudly wrong) and would now report a plausible
  own-fleet-only figure while a whole third-party freight leg vanished, with the well-to-wheel
  TOTAL still correct — a detectable zero becoming an undetectable partial. Fixed at the token
  list AND structurally: named `_TTW_BOUNDARIES`/`_WTT_BOUNDARIES` constants so the spellings
  the codebase emits cannot drift from the split consuming them, and the split now RECONCILES
  (anything unclassified is surfaced with a `reconciles` flag, so WTT + TTW + unclassified ==
  the total). `generation`/`td_loss` sit in neither half by design — energy supply, not a
  wheel-side emission — now visible rather than dropped.

- **PR #23** — _GHGP Cat 2/11/12 temporal basis (the sale-year / acquisition-year assertion)._
  The largest remaining accuracy gap: the engine computes activity x factor FOR THE PERIOD,
  but Cat 2 needs the full cradle-to-gate of goods ACQUIRED in the year and Cats 11/12 the
  full expected lifetime / end-of-life of products SOLD in the year. A Cat 11 figure covering
  one year instead of a 12-year product life is understated ~12x, and Cat 11 is frequently a
  manufacturer's largest Scope 3 category — previously only WARNED, asking for free text
  nothing could verify. The platform cannot compute the lifetime, so it now demands a closed,
  CATEGORY-SCOPED, frozen, fingerprinted assertion of what the figure DENOMINATES (Cat 2 has
  no lifetime vocabulary at all — its conforming basis structurally fits the period model;
  `sold_quantity_consumed_in_use` keeps dissipative fuel/chemical sellers from being
  false-blocked). B15-B19 gate it, with an ASYMMETRIC 0.5x block / 2.0x warn band so a
  multi-SKU portfolio's averaging drift never blocks while a one-year filing always does.
  The anti-cliff mechanism is a NULL run-stamp: every pre-existing run only warns, which is
  load-bearing because the gate is re-evaluated at RENDER time on filed runs. Designed by a
  3-way panel + judge. A 3-lens adversarial review raised 13 and confirmed 8, collapsing to
  four defects, all fixed: **HIGH** — B19 divided the CONSOLIDATED line total by an UNWEIGHTED
  physical assertion, so implied == share x declared and any org selling through a sub-50%
  entity was falsely accused of a >2x understatement with NO honest exit (at a share near
  1/lifetime it produced the maximally specific false accusation, "equals precisely ONE
  YEAR"); **HIGH** — the `filed_kg > 0` guard skipped the check exactly at zero, making the
  maximum possible understatement the one case it never saw; **LOW** — the divide could
  underflow at RENDER time on a filed run (an HTTP 500, worse than a blocker); **LOW** — the
  entailment CHECK did not enforce what it documented (SQL three-valued logic).

- **PR #25** — _GHGP Scope 2 residual mix for uncovered market-based load._ Scope 2 Guidance
  requires consumption NOT covered by a contractual instrument to be priced at the RESIDUAL
  MIX (the grid average with attributes other purchasers already claimed removed). The engine
  priced it at the plain location grid average, double counting those attributes and
  UNDERSTATING the market figure — one-directional, since residual mix is always >= the grid
  average. THE KEY FINDING (from the design judge): the market branch was guarded by
  `if is_electricity and instruments:`, so an org holding ZERO instruments never entered the
  allocator and its ENTIRE market figure was the location figure — the population where the
  understatement is 100%, and where a residual leg placed inside the allocator would have been
  dead code. Adds `residual_mix_rates` (append-only published reference data, with
  `not_published` as an ATTESTED absence) and `run_residual_mix_statements` (one frozen row per
  (market, year) touched, including fully-contractual markets — complete by construction).
  `market_key()` is extracted from the instrument matcher so both can never use two different
  notions of "market"; resolution is exact, two-pass on GWP vintage, never broadened. Blockers
  are calibrated on WHO OWNS THE MISSING FACT — absence never blocks (publishers release year Y
  in mid-Y+1), org-fixable or provably-wrong cases do. `total_co2e` and sum(location lines) are
  untouched; only `total_co2e_market` moves, and only upward. TWO review rounds — 16 raised/15
  confirmed, then 7 more on the fixes — all fixed with regression tests. The most damaging:
  `grid_rate_avg` weighted over ALL electricity (including covered load) then compared against
  the uncovered remainder's rate, producing a FALSE inversion blocker on a correct run; an
  attested absence plus a contractual claim producing NEITHER blocker nor warning (the
  conditions were not complementary, so the strongest double-count case was the silent one);
  and `residual_mix_comparable` blocking GRI 305-5 for EVERY org on the version stamp alone,
  since nothing is back-filled so every base run is NULL.

- **PR #27** — _Last residual-mix defect + a neutrality tolerance that scales._ (a) An
  org-supplied `residual_mix` instrument is that org's own residual RATE, not a contractual
  attribute claim, but summary counted it as contractual — disclosing 100% contractual
  coverage for an org holding ZERO contractual instruments, contradicting the run's own
  frozen statement. Fixed with a NEW `kwh_contractual_rank0` key so the established
  `kwh_contractual` is not re-scoped for filed runs. (b) ISO 14068 neutrality judged
  `residual <= 1e-9 tCO2e` — one microgram, ABSOLUTE — but `gross_tco2e` is the sum of
  thousands of float line items, so its representation error scales with the inventory and
  for a large one exceeds a fixed microgram: the claim was decided on float noise, and the
  error grew with the org. The tolerance is now relative and DISCLOSED beside the residual;
  it can only forgive a rounding-scale residual (1e-9 relative on a megatonne is one
  kilogram), and a claim resting on it is flagged `neutral_within_tolerance_only`.

- **PR #29** — _Temporal straddle proration._ An ActivityRecord carries a single `date`, so a
  supply invoice covering 15 Dec - 15 Jan was attributed WHOLLY to whichever fiscal year that
  date fell in. Declaring `coverage_start`/`coverage_end` lets a period-scoped run prorate the
  quantity by the overlapping share (inclusive calendar days, frozen onto the line), so two
  adjacent periods together account for exactly the whole record. Both columns nullable and
  NEVER back-filled — with no window a record is attributed by `date` byte-identically to
  before, because the platform cannot infer a window it was not told. Membership follows the
  WINDOW when declared; the prorated quantity replaces the raw one at EVERY consumer
  (emissions, biogenic pool, Scope 2 kWh); fingerprint v5 so declaring a window makes a run
  STALE rather than silently changing a filed figure. Review raised 6 / confirmed 5 → three
  causes: **HIGH** `coverage_overlap` bailed to a 1.0 fraction whenever EITHER period bound was
  NULL while membership used only the bound that exists, so an open-bounded period booked 100%
  ON TOP of its neighbour's share (1000 kg counted as 1468.75); **HIGH** `_energy_kwh` read the
  LIVE quantity, putting SECR/ESOS/ESRS E1-5/GRI 302 energy on the GROSS basis beside prorated
  emissions — a wrong implied intensity and 2000 kWh across two periods for a 1000 kWh invoice;
  and the share being priced on the record's `date` rather than the period it landed in, so the
  FY24 slice took FY25's residual mix and no FY24 instrument could cover it.

- **PR #31** — _Table 5.4 policy drift detection (W3)._ Closes the loose end from PR #19,
  which froze `ghgp_boundary_token` per Scope 3 line precisely so a later move in the
  acceptance vocabulary could be detected on an already-filed run — and until now nothing
  read it. `boundary_policy_drift()` re-evaluates each frozen token under the CURRENT policy
  and reports which WAY the filing leans: `understating` (lines the run ACCEPTED that the
  current vocabulary REJECTS — those categories may be PARTIAL figures filed as compliant,
  and this is the direction that matters) or `conservative` (over-strict, nothing
  understated, and the message says so rather than reading as a problem). A WARNING and
  never a blocker: restating a filed verdict would break the reproduction contract, and a
  vocabulary move must not retroactively invalidate every filing made under the previous
  one. Lines frozen before the token existed are reported as `undeterminable`, never
  back-filled from the live catalogue. Pure read over frozen state — no schema, migration
  or engine change.

## Strengths worth preserving

Fail-closed quantity/unit handling; real immutability and frozen lineage; correct
AR5/AR6 GWP constants; sound per-line pedigree data-quality; solid multi-tenancy
(no cross-tenant IDOR); the fail-closed doctrine already applied by most renderers.
These are the foundation and should not be disturbed while fixing the above.
