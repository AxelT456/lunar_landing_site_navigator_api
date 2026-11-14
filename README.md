# Lunar Landing Site Navigator API

REST API backend for evaluating and comparing lunar south pole landing sites, calculating solar power generation potential, and direct-to-Earth (DTE) communication windows using LOLA terrain data and SPICE/Horizons ephemerides.

## Features

- **Health check (`GET /api/v1/health`)**: System and dataset status.
- **Site Catalog (`GET /api/v1/sites`)**: Curated landing sites from PostGIS (Artemis III, CLPS, Apollo 11/15/17).
- **Terrain Patch (`GET /api/v1/terrain/patch`)**: Elevation grid and slope arrays ($64 \times 64$, $128 \times 128$, $256 \times 256$) anywhere on the Moon (`lat` −90..90), from LOLA polar and global DEMs.
- **Site Evaluation (`POST /api/v1/evaluate`)**: 360° local horizon profile, solar and Earth visibility, GO/NO-GO operational heuristics, and 3D lighting vectors.
- **Scenario Comparison (`POST /api/v1/compare`)**: Side-by-side evaluation of 2 to 5 landing scenarios.
- **Permanently Shadowed Regions (`GET /api/v1/psr/nearby`)**: GeoJSON boundaries of nearby PSRs.

## Architecture

Built with **FastAPI**, **Pydantic v2**, **PostgreSQL + PostGIS** and NASA datasets:

- `app/main.py`: application, unified `ErrorResponse` handlers (`ERR_*` codes) and router registration.
- `app/routers/`: one module per endpoint; `_common.py` runs the evaluation pipeline with the PostGIS caches.
- `app/engine/`: physics engine. LOLA DEMs (polar 20/40/80 m and global `ldem_64`), 360° horizon raycasting with lunar curvature, SPICE ephemerides via Skyfield (JPL Horizons and an analytical model as fallbacks), GO/CAUTION/NO-GO verdict.
- `app/db.py`: PostGIS access (`landing_sites`, `psr_regions`, `horizon_profiles`, `ephemeris_cache`).
- `app/models.py`: request/response schemas matching `lunar-landing-api-contract.md` (v1.1).

Coordinates: south polar stereographic in the LOLA CRS (`x = ρ·sin λ`, `y = ρ·cos λ`); 3D vectors in the visor frame `[+X East, +Y Up, −Z North]`.

## Configuration

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL + PostGIS connection string. Without it the API starts, `/health` reports the database as disconnected and DB-backed endpoints answer `503`. |
| `DATA_DIR` | Root of the NASA datasets (default `/data`): `raw/lola/`, `raw/lola_global/`, `raw/spice/`. Without them the engine uses synthetic terrain and JPL Horizons / analytical ephemerides (fine for development, not for real results). |

## Setup and Development

### Requirements
- Python 3.12+
- Docker, for the local PostGIS seed (`db/README.md`)

### Local Installation
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
docker compose -f docker-compose.dev.yml up -d
cp .env.example .env   # then export its variables, e.g. `set -a; . ./.env; set +a`
```

### Running the API
```bash
uvicorn app.main:app --reload --port 8000
```
Interactive API docs are available at `http://localhost:8000/docs`.

### Running Tests
```bash
pytest                              # no database or datasets needed
python scripts/contract_check.py    # needs DATABASE_URL (seed database)
```

Tests that need the real NASA datasets are skipped unless they are present in `DATA_DIR`.

## Deploy

See `docs/DEPLOY.md` (Woodpecker CI → staging on merge, production by promotion).
