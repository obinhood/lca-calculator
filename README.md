# lca-calculator

An audit-grade calculation and disclosure engine for organisational and product
carbon accounting. FastAPI + SQLAlchemy, SQLite for dev and Postgres for
production, with a small React SPA over the top.

The design bias throughout is **defensibility over convenience**: every figure is
reproducible from an immutable run, every methodological choice is frozen onto
that run rather than read live, and a number that cannot be derived honestly is
refused with a reason instead of estimated.

## What is actually here

| | |
|---|---|
| Framework registry | 42 entries — 24 `built`, 16 `partial`, 2 `reference` |
| API | 123 endpoints over 44 ORM models |
| Engine | ~25k lines Python across 76 modules |
| Tests | 1,472, including property-based (Hypothesis) and calculation oracles |

`GET /frameworks` is the authoritative inventory and each entry states its own
`platform_support` level. `partial` means partial — the guidance says what is not
produced.

### Calculation

- **Per-gas storage with GWP applied at calculation time.** Sources disagree on
  GWP vintage (DEFRA is AR5, USEEIO v1.4 is AR6); pre-aggregated CO₂e is never
  blended across them. Per-gas masses are stored and the GWP set applied at
  compute time, so AR5/AR6 is a switch rather than a re-import.
- **Deterministic factor resolution** with immutable, versioned factors —
  published rows are never mutated; a correction is a new row with a
  `supersedes_id` link, so a historical run stays reproducible.
- **Unit normalisation** via Pint, with conversion failures surfaced per line.
- **Temporal proration** — a consumption window straddling a period boundary
  contributes only its overlapping share.
- **Organisational boundary** — equity share / financial control / operational
  control, with the entity population and every weight frozen onto the run
  (including entities weighted 0.0, which are the "excluded investees" list a
  disclosure asks for).
- **Dual Scope 2** — location and market basis on every run, with residual-mix
  pricing for uncovered load, plus **hourly temporal matching** as a parallel
  method (`GET /reports/hourly_scope2`) for the proposed GHG Protocol revision:
  granular certificates, a CFE score, and deliverability gating. Surplus in one
  hour never offsets a deficit in another.
- **PACT Pathfinder v3, both sides** — import and validate a supplier's product
  carbon footprint and materialise it as a `supplier_specific` factor (which on
  a real portfolio moved the uncertainty band from ±195.6% to ±10.3%), and serve
  the network: `GET /3/footprints`, OAuth2 client credentials, CloudEvents.
- **SBTi Corporate Net-Zero Standard V2.0** — the ≥5% significance test on
  categories 1–14 with the WTW-uplift denominator, company categorisation,
  C14.2/3 exclusion validation and C12 Scope 2 conformance. (C8.3 recalculation
  triggers are implemented in the service layer but no endpoint emits them yet.)
- **Versioned classification crosswalks** whose uncertainty is *measured* —
  σ = ln(GSD) of the candidate set's own factors, so a one-to-one hop is exactly
  zero. A direct UNSPSC→NAICS hop is flagged uncitable, because UNSPSC classifies
  the product and NAICS the establishment.
- **Spend-based** normalisation with inflation and price-basis adjustment.

### Data quality and uncertainty

- **ecoinvent pedigree matrix** — five representativeness indicators per line,
  mapped to a lognormal geometric standard deviation. Conservative by default:
  an indicator that cannot be scored takes the worst value, never a flattering
  middle one.
- **Monte Carlo propagation** to an inventory-level interval, with a
  variance-share ranking of what drives the width —
  `GET /runs/{run_id}/uncertainty`. Correlation is explicit: lines sharing an
  emission factor share its error, and the narrowest (`independent`) and widest
  (`perfect`) bounds are always reported alongside the `by_factor` default so
  the reader can see how much the answer rests on that assumption.
- **Pre-calculation screening** as an assurance misstatement ledger, not an
  anomaly detector — `POST /activities/screen`. Each finding carries a stated
  expectation, the threshold in force, a quantified effect and an auditable
  disposition, and the accumulated *uncorrected* effect is tracked against
  materiality (ISAE 3410 ¶¶50–56, ISSA 5000 ¶¶153–161). Deterministic checks
  only: a z-score is provably blind at twelve monthly points.

### Reporting and assurance

Framework renderers under `app/reports/` — ESRS E1, ISSB S2, GRI, CDP, SECR,
California SB 253, SBTi, PCAF, SFDR PAI, EU Taxonomy, CBAM, EU/UK ETS, ESOS,
TCFD, EcoVadis, EPD, PEF, RICS whole-life carbon, ISO 14064-2, CSDDD, TNFD/SBTN.

Every renderer honours a **reproduction contract**: it reads only what the run
froze, never the live activity or factor tables. Re-rendering a filed run years
later returns the same statement even after activities are re-mapped or factors
corrected.

Assurance engagements are first-class (`ISAE 3410` / `ISO 14064-3` /
`ISSA 5000`) with findings, lineage and access grants — not a PDF export.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
```

```bash
pip install -r requirements.txt && python scripts/init_db.py
```

```bash
uvicorn app.main:app --reload
```

Interactive docs at `http://127.0.0.1:8000/docs`. Register an organisation via
`POST /organisations` — the API key is returned once and every other endpoint
requires it as `X-API-Key`.

A worked path: `POST /activities/upload_csv` with
`sample_data/sample_activities.csv`, review suggestions at `GET /mappings/review`,
then `POST /calculate/run`.

### Frontend

```bash
cd frontend && npm install && npm run dev
```

### Tests

```bash
pip install -r requirements-dev.txt && pytest -q
```

The property suite has a deeper profile for engine changes:

```bash
HYPOTHESIS_PROFILE=deep pytest tests/test_calculation_properties.py
```

### Production

`DATABASE_URL` switches SQLite for Postgres with no code change; Alembic reads
the same value. A `Dockerfile` and `railway.json` are included.

## Data sources

`app/ef_catalog/registry.py` is the researched landscape of emission-factor
databases, and it encodes two rules the code enforces:

1. **Licence compliance gates what ships.** ecoinvent, Sphera and full Agribalyse
   LCI are not redistributable (results only); EXIOBASE's free tier is
   non-commercial. The `redistributable` flag decides what may live inside the
   product.
2. **Never blend pre-aggregated CO₂e across GWP vintages.** Re-derive from
   per-gas splits at a common GWP set.

The bundled DEFRA CSV is **demo data for structure**, not a licensed factor set.
Load real factors with `scripts/load_factors.py`.

The audit-grade free stack the registry recommends: DEFRA/DESNZ + US EPA GHG
Factors Hub for activity data, USEEIO + Open CEDA for spend, AIB Residual Mix +
eGRID for market-based Scope 2.

## Known gaps

Stated plainly because a reader deciding whether to use this deserves them:

- **Ingestion is CSV only.** No ERP, utility, expense or travel connectors.
- **No supplier portal**, and PACT conformance is untested. The API is served but
  has not been run against the official conformance tool, which needs a hosted
  endpoint and a registered test account. Outbound event delivery with retry is
  not built.
- **Nothing on the reduction side** — no abatement levers, no MACC, no scenario
  modelling.
- **No AI-assisted mapping.** The resolver is rule-based plus fuzzy matching.
- **Period-over-period screening covers declared series only.** `series_key` is
  preparer-supplied and never written by the engine; a row without one is not
  screened for year-on-year change, and the unenrolled share is reported by name.
- **Reviewer identity is still unavailable.** The mapping audit trail records
  what changed and when, but authentication is an organisation-scoped API key
  with no concept of a person, so *who* decided cannot be recorded.
- **Hourly Scope 2 has no data feed.** The matching engine, certificates and
  deliverability model exist, but nothing ingests interval meter data
  automatically — hourly load arrives by CSV like everything else.
- **Bill extraction has no OCR.** The validation layer — triage, arithmetic
  reconciliation, MPAN check, read quality, supersession — is built; character
  recognition sits behind a pluggable protocol and the library is the operator's
  choice (pdfplumber is MIT; PyMuPDF is AGPL; OCRmyPDF needs AGPL Ghostscript).
- Crosswalk versioning (chart-of-accounts → UNSPSC → NAICS/NACE) is documented in
  the registry but not implemented, so spend-mapping error is not yet carried
  into the uncertainty band.

## Conventions

- Never mutate a published emission factor; supersede it.
- A missing input yields `cannot_determine`, never a silent zero — and `NULL`
  must stay distinguishable from `[]`.
- Anything shown as a derivation must reproduce the figure it explains, or the
  working is refused rather than shown.
- Mark partial coverage `partial`, and say what is not covered.
