"""Tests for landing sites catalog endpoint."""

from fastapi.testclient import TestClient


def test_get_sites_success(client: TestClient) -> None:
    """Test retrieving candidate landing sites catalog."""
    response = client.get("/api/v1/sites")
    assert response.status_code == 200
    data = response.json()
    assert "count" in data
    assert "sites" in data
    assert data["count"] == len(data["sites"])
    assert data["count"] >= 10

    # Verify Shackleton Rim site content matches contract specification
    shackleton = next((s for s in data["sites"] if s["id"] == "artemis-shackleton-rim"), None)
    assert shackleton is not None
    assert shackleton["name"] == "Shackleton Rim"
    assert shackleton["category"] == "Artemis III"
    assert shackleton["lat"] == -89.66
    assert shackleton["lon"] == 222.7
    assert shackleton["polar_x_m"] == -6975.1
    assert shackleton["polar_y_m"] == 7569.4
    assert shackleton["elevation_m"] == 2048.5
    assert shackleton["slope_deg"] == 6.4
    assert shackleton["nearest_psr_km"] == 1.8


def test_sites_polar_coords_match_projection(client: TestClient) -> None:
    """Catalog polar coords must agree with lat_lon_to_polar_xy (within 1 km)."""
    from app.domain.coordinates import lat_lon_to_polar_xy

    for site in client.get("/api/v1/sites").json()["sites"]:
        x, y = lat_lon_to_polar_xy(site["lat"], site["lon"])
        assert abs(site["polar_x_m"] - x) < 1000, site["id"]
        assert abs(site["polar_y_m"] - y) < 1000, site["id"]
