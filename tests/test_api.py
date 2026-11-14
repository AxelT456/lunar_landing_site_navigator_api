"""HTTP contract tests that run without PostgreSQL or NASA datasets.

Database access is replaced with fakes (monkeypatch on app.db) and DATA_DIR
points to an empty directory, so the engine uses its synthetic terrain fallback.
The full stack with the real data is checked by scripts/contract_check.py in CI
(PostGIS service loaded with db/init) and by scripts/smoke.sh after each deploy.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app

SHACKLETON = {"lat": -89.66, "lon": 222.7}

FAKE_SITE = {
    "id": "artemis-shackleton-rim",
    "name": "Shackleton Rim",
    "category": "Artemis III",
    "lat": -89.66,
    "lon": 222.7,
    "polar_x_m": -6991.8,
    "polar_y_m": -7576.9,
    "elevation_m": 880.1,
    "slope_deg": 16.0,
    "nearest_psr_km": 4.7,
    "description": "Fake row for tests.",
}

FAKE_PSR = {
    "gid": 1,
    "name": "PSR 1",
    "area_km2": 2.5,
    "distance_km": 4.7,
    "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [10, 0], [10, 10], [0, 0]]]},
}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(app) as c:
        yield c


def _error_code(body: dict) -> str | None:
    error = body.get("error")
    return error.get("code") if isinstance(error, dict) else None


def test_health_reports_missing_database(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["database"]["connected"] is False
    assert body["engine"]["available"] is True


def test_sites_from_database(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "list_landing_sites", lambda: [dict(FAKE_SITE)])
    r = client.get("/api/v1/sites")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["sites"][0]["id"] == "artemis-shackleton-rim"
    assert body["sites"][0]["nearest_psr_km"] == 4.7


def test_sites_database_down_is_503_error_response(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def down() -> list:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(db, "list_landing_sites", down)
    r = client.get("/api/v1/sites")
    assert r.status_code == 503
    assert _error_code(r.json()) == "ERR_UPSTREAM_SERVICE"


def test_invalid_latitude_is_422_invalid_coordinates(client: TestClient) -> None:
    r = client.get("/api/v1/terrain/patch", params={"lat": 95, "lon": 10})
    assert r.status_code == 422
    body = r.json()
    assert _error_code(body) == "ERR_INVALID_COORDINATES"
    assert body["error"]["details"]["allowed_range"] == [-90.0, 90.0]


def test_unknown_route_is_404_not_found(client: TestClient) -> None:
    r = client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    assert _error_code(r.json()) == "ERR_NOT_FOUND"


def test_compare_needs_two_scenarios(client: TestClient) -> None:
    r = client.post(
        "/api/v1/compare",
        json={"scenarios": [{"label": "A", **SHACKLETON, "start_utc": "2026-12-01T00:00:00Z"}]},
    )
    assert r.status_code == 422
    assert _error_code(r.json()) == "ERR_SCENARIO_LIMIT_EXCEEDED"


def test_psr_nearby_uses_lola_polar_coordinates(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_nearby(**kwargs):
        calls.append(kwargs)
        return [dict(FAKE_PSR)]

    monkeypatch.setattr(db, "get_nearby_psrs", fake_nearby)
    r = client.get("/api/v1/psr/nearby", params=SHACKLETON)
    assert r.status_code == 200
    body = r.json()
    # D-012: x = r*sin(lon), y = r*cos(lon) in the LOLA south polar CRS.
    assert body["center"]["polar_x_m"] == pytest.approx(-6991.8, abs=0.5)
    assert body["center"]["polar_y_m"] == pytest.approx(-7576.9, abs=0.5)
    assert body["count"] == 1
    assert calls[0]["include_geometry"] is True


def test_terrain_patch_accepts_apollo_latitude(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "get_nearby_psrs", lambda **_: [])
    r = client.get("/api/v1/terrain/patch", params={"lat": 0.674, "lon": 23.473, "resolution": 64})
    assert r.status_code == 200
    grid = r.json()["grid"]
    assert grid["resolution"] == 64
    assert len(grid["elevations_m"]) == 64 * 64


def test_cache_control_on_reference_data_only(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "list_landing_sites", lambda: [dict(FAKE_SITE)])
    assert client.get("/api/v1/sites").headers.get("cache-control") == "public, max-age=3600"
    assert "cache-control" not in client.get("/api/v1/health").headers
    # Errors are never cached.
    assert "cache-control" not in client.get("/api/v1/terrain/patch", params={"lat": 95, "lon": 0}).headers


def test_terrain_patch_reports_source_and_rounds(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "get_nearby_psrs", lambda **_: [])
    grid = client.get("/api/v1/terrain/patch", params={**SHACKLETON, "resolution": 64}).json()["grid"]
    # No datasets in DATA_DIR: synthetic fallback, no native posting to report.
    assert grid["source"] == "synthetic"
    assert grid["native_resolution_m"] is None
    assert all(round(v, 1) == v for v in grid["elevations_m"][:200])


def test_psr_catalog_is_paginated_with_label_points(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def fake_list(**kwargs):
        calls.append(kwargs)
        row = {k: v for k, v in FAKE_PSR.items() if k != "distance_km"}
        return 1531, [{**row, "label_x_m": -6991.8, "label_y_m": -7576.9}]

    monkeypatch.setattr(db, "list_psrs", fake_list)
    r = client.get("/api/v1/psr", params={"offset": 500, "limit": 500})
    assert r.status_code == 200
    body = r.json()
    assert (body["total"], body["offset"], body["limit"], body["count"]) == (1531, 500, 500, 1)
    assert body["simplify_m"] == 500.0
    point = body["psrs"][0]["label_point"]
    assert point["lat"] == pytest.approx(-89.66, abs=0.01)
    assert point["lon"] == pytest.approx(222.7, abs=0.05)
    assert calls[0] == {"simplify_m": 500.0, "limit": 500, "offset": 500, "min_area_km2": 0.0}


def test_psr_catalog_limit_is_capped(client: TestClient) -> None:
    r = client.get("/api/v1/psr", params={"limit": 501})
    assert r.status_code == 422
    assert _error_code(r.json()) == "ERR_INVALID_PAYLOAD"


def test_ephemeris_needs_spice_kernels(client: TestClient) -> None:
    r = client.get("/api/v1/ephemeris", params={"utc": "2026-12-01T00:00:00Z"})
    assert r.status_code == 503
    assert _error_code(r.json()) == "ERR_UPSTREAM_SERVICE"


def test_ephemeris_date_range(client: TestClient) -> None:
    r = client.get("/api/v1/ephemeris", params={"utc": "2051-01-01T00:00:00Z"})
    assert r.status_code == 422
    assert _error_code(r.json()) == "ERR_DATE_OUT_OF_RANGE"


def test_compare_include_timeline(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.routers.compare as compare_router

    seen: list[bool] = []

    def fake_eval(**kwargs):
        seen.append(kwargs["include_details"])
        site = {"lat": kwargs["lat"], "lon": kwargs["lon"], "elevation_m": 0.0, "slope_deg": 1.0,
                "nearest_psr_km": None, "status": "GO", "reasons": [], "checks": []}
        summary = {"sun_visible_pct": 90.0, "earth_visible_pct": 50.0, "max_continuous_sun_hours": 10.0,
                   "max_continuous_dte_hours": 5.0, "avg_power_factor": 0.5, "estimated_peak_power_w": 100.0}
        point = {"timestamp_utc": kwargs["start_utc"], "sun_az_deg": 0.0, "sun_el_deg": 1.0,
                 "sun_horizon_el_deg": 0.0, "sun_visible": True, "sun_vector_3d": [0.0, 0.0, -1.0],
                 "earth_az_deg": 0.0, "earth_el_deg": 1.0, "earth_horizon_el_deg": 0.0, "earth_visible": True,
                 "earth_vector_3d": [0.0, 0.0, -1.0], "power_factor": 1.0}
        out = {"site": site, "summary": summary}
        if kwargs["include_details"]:
            out |= {"horizon_profile": [], "timeline": [point]}
        return out

    monkeypatch.setattr(compare_router, "run_site_evaluation", fake_eval)
    scenarios = [
        {"label": "A", **SHACKLETON, "start_utc": "2026-12-01T00:00:00Z"},
        {"label": "B", "lat": -85.0, "lon": 0.0, "start_utc": "2026-12-01T00:00:00Z"},
    ]
    plain = client.post("/api/v1/compare", json={"scenarios": scenarios}).json()
    assert all(r["timeline"] is None for r in plain["results"])
    full = client.post("/api/v1/compare", json={"scenarios": scenarios, "include_timeline": True}).json()
    assert all(len(r["timeline"]) == 1 for r in full["results"])
    assert seen == [False, False, True, True]
