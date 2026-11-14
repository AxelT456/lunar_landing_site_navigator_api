"""Tests for permanently shadowed regions (PSR) endpoint."""

from fastapi.testclient import TestClient


def test_get_nearby_psrs_success(client: TestClient) -> None:
    """Test retrieving nearby PSRs around Shackleton Crater."""
    params = {
        "lat": -89.66,
        "lon": 222.7,
        "radius_km": 25.0,
        "limit": 50,
        "simplify_m": 150.0,
    }
    response = client.get("/api/v1/psr/nearby", params=params)
    assert response.status_code == 200
    data = response.json()

    assert "center" in data
    assert data["center"]["lat"] == -89.66
    assert data["center"]["lon"] == 222.7
    assert data["center"]["polar_x_m"] == -6975.1
    assert data["center"]["polar_y_m"] == 7569.4
    assert data["radius_km"] == 25.0
    assert "count" in data
    assert "psrs" in data
    assert data["count"] == len(data["psrs"])
    assert data["count"] >= 1

    psr0 = data["psrs"][0]
    assert psr0["gid"] == 14
    assert psr0["name"] == "Shackleton"
    assert psr0["area_km2"] == 182.4
    assert psr0["distance_km"] == 1.8
    assert "geometry" in psr0
    assert psr0["geometry"]["type"] in ["MultiPolygon", "Polygon"]
    assert len(psr0["geometry"]["coordinates"]) > 0


def test_get_nearby_psrs_out_of_bounds_lat(client: TestClient) -> None:
    """Test latitude out of South Pole boundary returns 422."""
    params = {"lat": -79.0, "lon": 0.0}
    response = client.get("/api/v1/psr/nearby", params=params)
    assert response.status_code == 422


def test_get_nearby_psrs_invalid_radius(client: TestClient) -> None:
    """Test non-positive or excessive search radius returns 422."""
    params = {"lat": -89.0, "lon": 0.0, "radius_km": -5.0}
    response = client.get("/api/v1/psr/nearby", params=params)
    assert response.status_code == 422

    params = {"lat": -89.0, "lon": 0.0, "radius_km": 300.0}
    response = client.get("/api/v1/psr/nearby", params=params)
    assert response.status_code == 422


def test_get_nearby_psrs_invalid_limit(client: TestClient) -> None:
    """Test non-positive limit returns 422."""
    params = {"lat": -89.0, "lon": 0.0, "limit": 0}
    response = client.get("/api/v1/psr/nearby", params=params)
    assert response.status_code == 422
