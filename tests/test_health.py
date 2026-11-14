"""Tests for health endpoint."""

from fastapi.testclient import TestClient


def test_root_endpoint(client: TestClient) -> None:
    """Test service root endpoint."""
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert "name" in data
    assert "version" in data


def test_health_check(client: TestClient) -> None:
    """Test GET /api/v1/health status and structure."""
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert "database" in data
    assert data["database"]["landing_sites_count"] == 10
    assert data["database"]["psr_regions_count"] == 1531
