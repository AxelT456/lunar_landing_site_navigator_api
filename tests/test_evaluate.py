"""Tests for site operational evaluation endpoint."""

from fastapi.testclient import TestClient


def test_evaluate_site_success(client: TestClient) -> None:
    """Test evaluating candidate landing site with valid parameters."""
    payload = {
        "lat": -89.66,
        "lon": 222.7,
        "start_utc": "2026-12-01T00:00:00Z",
        "duration_days": 14.0,
        "step_minutes": 60,
        "panel_efficiency": 0.28,
        "panel_area_m2": 4.0,
    }
    response = client.post("/api/v1/evaluate", json=payload)
    assert response.status_code == 200
    data = response.json()

    # Site status checks
    assert "site" in data
    assert data["site"]["lat"] == -89.66
    assert data["site"]["lon"] == 222.7
    assert data["site"]["elevation_m"] == 2048.5
    assert data["site"]["slope_deg"] == 6.4
    assert data["site"]["nearest_psr_km"] == 1.8
    assert data["site"]["status"] in ["GO", "NO-GO", "CAUTION"]
    assert isinstance(data["site"]["reasons"], list)

    # Summary checks
    assert "summary" in data
    summary = data["summary"]
    assert summary["sun_visible_pct"] == 82.1
    assert summary["earth_visible_pct"] == 64.3
    assert summary["max_continuous_sun_hours"] == 196.0
    assert summary["max_continuous_dte_hours"] == 140.0
    assert summary["avg_power_factor"] == 0.74
    assert summary["estimated_peak_power_w"] == 1528.0

    # Horizon profile checks
    assert "horizon_profile" in data
    assert len(data["horizon_profile"]) == 360
    assert data["horizon_profile"][0]["az_deg"] == 0

    # Timeline checks
    assert "timeline" in data
    assert len(data["timeline"]) > 0
    t0 = data["timeline"][0]
    assert "timestamp_utc" in t0
    assert len(t0["sun_vector_3d"]) == 3
    assert len(t0["earth_vector_3d"]) == 3
    assert isinstance(t0["sun_visible"], bool)
    assert isinstance(t0["earth_visible"], bool)


def test_evaluate_site_out_of_bounds_lat(client: TestClient) -> None:
    """Test latitude out of South Pole range returns 422."""
    payload = {
        "lat": -75.0,
        "lon": 222.7,
        "start_utc": "2026-12-01T00:00:00Z",
    }
    response = client.post("/api/v1/evaluate", json=payload)
    assert response.status_code == 422


def test_evaluate_site_invalid_panel_efficiency(client: TestClient) -> None:
    """Test efficiency > 1.0 or < 0.0 returns 422."""
    payload = {
        "lat": -89.66,
        "lon": 222.7,
        "start_utc": "2026-12-01T00:00:00Z",
        "panel_efficiency": 1.5,
    }
    response = client.post("/api/v1/evaluate", json=payload)
    assert response.status_code == 422


def test_evaluate_site_invalid_duration(client: TestClient) -> None:
    """Test duration outside [0.5, 30.0] returns 422."""
    payload = {
        "lat": -89.66,
        "lon": 222.7,
        "start_utc": "2026-12-01T00:00:00Z",
        "duration_days": 40.0,
    }
    response = client.post("/api/v1/evaluate", json=payload)
    assert response.status_code == 422


def test_evaluate_site_invalid_step_minutes(client: TestClient) -> None:
    """Test step_minutes outside [10, 360] returns 422."""
    payload = {
        "lat": -89.66,
        "lon": 222.7,
        "start_utc": "2026-12-01T00:00:00Z",
        "step_minutes": 5,
    }
    response = client.post("/api/v1/evaluate", json=payload)
    assert response.status_code == 422
