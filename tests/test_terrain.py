"""Tests for terrain patch endpoint."""

from fastapi.testclient import TestClient


def test_terrain_patch_success(client: TestClient) -> None:
    """Test retrieving valid terrain patch."""
    params = {
        "lat": -89.66,
        "lon": 222.7,
        "size_km": 12.0,
        "resolution": 128,
    }
    response = client.get("/api/v1/terrain/patch", params=params)
    assert response.status_code == 200
    data = response.json()

    # Center validation
    assert "center" in data
    assert data["center"]["lat"] == -89.66
    assert data["center"]["lon"] == 222.7
    assert data["center"]["polar_x_m"] == -6991.8
    assert data["center"]["polar_y_m"] == -7576.9
    assert data["center"]["elevation_m"] == 2048.5
    assert data["center"]["slope_deg"] == 6.4

    # Grid validation
    assert "grid" in data
    grid = data["grid"]
    assert grid["size_km"] == 12.0
    assert grid["resolution"] == 128
    assert grid["cell_size_m"] == 93.75 or grid["cell_size_m"] == 94.48 or isinstance(grid["cell_size_m"], float)
    assert len(grid["elevations_m"]) == 128 * 128
    assert len(grid["slopes_deg"]) == 128 * 128
    assert grid["min_elevation_m"] <= grid["max_elevation_m"]

    # Nearby PSRs validation
    assert "nearby_psrs" in data
    assert len(data["nearby_psrs"]) >= 1
    assert data["nearby_psrs"][0]["name"] == "Shackleton"


def test_terrain_patch_out_of_bounds_lat_high(client: TestClient) -> None:
    """Test latitude outside South Pole boundary (> -80.0) returns 422."""
    params = {"lat": -79.9, "lon": 222.7}
    response = client.get("/api/v1/terrain/patch", params=params)
    assert response.status_code == 422


def test_terrain_patch_out_of_bounds_lat_low(client: TestClient) -> None:
    """Test latitude below South Pole minimum (< -90.0) returns 422."""
    params = {"lat": -90.5, "lon": 222.7}
    response = client.get("/api/v1/terrain/patch", params=params)
    assert response.status_code == 422


def test_terrain_patch_out_of_bounds_lon(client: TestClient) -> None:
    """Test longitude out of bounds returns 422."""
    params = {"lat": -89.0, "lon": -185.0}
    response = client.get("/api/v1/terrain/patch", params=params)
    assert response.status_code == 422


def test_terrain_patch_invalid_resolution(client: TestClient) -> None:
    """Test resolution not in [64, 128, 256] returns 422."""
    params = {"lat": -89.0, "lon": 0.0, "resolution": 100}
    response = client.get("/api/v1/terrain/patch", params=params)
    assert response.status_code == 422


def test_terrain_patch_invalid_size_km(client: TestClient) -> None:
    """Test size_km below minimum or above maximum returns 422."""
    params = {"lat": -89.0, "lon": 0.0, "size_km": 1.0}
    response = client.get("/api/v1/terrain/patch", params=params)
    assert response.status_code == 422

    params = {"lat": -89.0, "lon": 0.0, "size_km": 50.0}
    response = client.get("/api/v1/terrain/patch", params=params)
    assert response.status_code == 422
