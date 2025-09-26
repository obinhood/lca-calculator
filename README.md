# Carbon Footprint MVP (FastAPI + SQLite)

A minimal, auditable MVP for organisational carbon accounting (Scopes 1, 2 and basic Scope 3).
Includes CSV upload, unit-normalisation, emission factor resolution, QA checks, calculation, and a simple report export.

## Features
- CSV upload for activities (energy, fuel, travel, waste, spend).
- Deterministic Emission Factor (EF) resolver with version pinning (DEFRA-style placeholders).
- QA checks for missing/negative values, unit consistency.
- Calculation engine (CO₂e) with AR5 vs AR6 switch.
- Simple plaintext/JSON report.
- SQLite database with SQLAlchemy models.
- Basic auto-mapping agent (rule-based + fuzzy suggestions).

## Quickstart
```bash
# 1) Create and activate a venv (recommended)
python3 -m venv .venv && source .venv/bin/activate

# 2) Install deps
pip install -r requirements.txt

# 3) Initialise DB (creates SQLite file + seeds EF catalog)
python scripts/init_db.py

# 4) Run API
uvicorn app.main:app --reload

# 5) Open docs
# http://127.0.0.1:8000/docs

# 6) Try sample upload (via Swagger UI -> /activities/upload_csv)
#    sample file: sample_data/sample_activities.csv
```

## Data Flow
1. Upload CSV → `ingestion.parse_csv()` → canonical schema
2. QA → `qa.check_records()`
3. EF mapping → `resolver.auto_map()` (deterministic; suggestions if low-confidence)
4. Calculation → `calc.compute_co2e()`
5. Results stored + available via `/results/summary` and `/reports/summary`

## Notes
- EF dataset here is **placeholder** demo data to show structure. Replace with your licensed DEFRA/econinvent data.
- Never mutate source EF rows; add new rows with `supersedes_id` when updating.
- Provenance is tracked per activity & factor mapping.

## Roadmap (next)
- OCR parsers for PDFs
- Spend→EEIO classification
- Scenario analysis module
- CSRD/CDP export templates
# lca-calculator
