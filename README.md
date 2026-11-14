# Lunar Landing Site Navigator API

REST API backend for evaluating and comparing lunar south pole landing sites, calculating solar power generation potential, and direct-to-Earth (DTE) communication windows using LOLA terrain data and SPICE/Horizons ephemerides.

## Features

- **Health check (`GET /api/v1/health`)**: System and dataset status.
- **Site Catalog (`GET /api/v1/sites`)**: Curated candidate landing sites (Artemis III, CLPS).
- **Terrain Patch (`GET /api/v1/terrain/patch`)**: Elevation grid and slope arrays ($64 \times 64$, $128 \times 128$, $256 \times 256$) around south pole coordinates.
- **Site Evaluation (`POST /api/v1/evaluate`)**: 360° local horizon profile, solar and Earth visibility, GO/NO-GO operational heuristics, and 3D lighting vectors.
- **Scenario Comparison (`POST /api/v1/compare`)**: Side-by-side evaluation of 2 to 5 landing scenarios.
- **Permanently Shadowed Regions (`GET /api/v1/psr/nearby`)**: GeoJSON boundaries of nearby PSRs.

## Architecture

Built with **FastAPI** and **Pydantic v2** following Clean Architecture principles:

- `app/api/`: HTTP transport layer, request routing, and parameter extraction.
- `app/schemas/`: Strict boundary validation matching the API specification (`lunar-landing-api-contract.md`).
- `app/domain/`: Pure scientific logic, polar stereographic projections, horizon calculation, and ephemeris analysis.
- `app/infrastructure/`: Adapters for LOLA elevation datasets and SPICE/Horizons external feeds.
- `app/core/`: Application settings, lunar physical constants, and operational thresholds.

## Setup and Development

### Requirements
- Python 3.10+ (or 3.15+)

### Local Installation
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### Running the API
```bash
uvicorn app.main:app --reload --port 8000
```
Interactive API docs are available at `http://localhost:8000/docs`.

### Running Tests
```bash
pytest
```
