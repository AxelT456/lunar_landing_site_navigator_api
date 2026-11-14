"""Tests for scenario comparison endpoint."""

from fastapi.testclient import TestClient


def test_compare_scenarios_success(client: TestClient) -> None:
    """Test comparative evaluation between two valid landing scenarios."""
    payload = {
        "scenarios": [
            {
                "label": "Escenario A: Shackleton (1 Dic)",
                "lat": -89.66,
                "lon": 222.7,
                "start_utc": "2026-12-01T00:00:00Z",
            },
            {
                "label": "Escenario B: Shackleton Retrasado (15 Dic)",
                "lat": -89.66,
                "lon": 222.7,
                "start_utc": "2026-12-15T00:00:00Z",
            },
        ],
        "duration_days": 14.0,
        "step_minutes": 120,
    }
    response = client.post("/api/v1/compare", json=payload)
    assert response.status_code == 200
    data = response.json()

    assert "results" in data
    assert len(data["results"]) == 2
    r0 = data["results"][0]
    assert r0["label"] == "Escenario A: Shackleton (1 Dic)"
    assert "site" in r0
    assert "summary" in r0
    assert r0["site"]["lat"] == -89.66


def test_compare_scenarios_minimum_violation(client: TestClient) -> None:
    """Test fewer than 2 scenarios returns 422."""
    payload = {
        "scenarios": [
            {
                "label": "Solo un escenario",
                "lat": -89.66,
                "lon": 222.7,
                "start_utc": "2026-12-01T00:00:00Z",
            }
        ]
    }
    response = client.post("/api/v1/compare", json=payload)
    assert response.status_code == 422


def test_compare_scenarios_maximum_violation(client: TestClient) -> None:
    """Test more than 5 scenarios returns 422."""
    scenarios = [
        {
            "label": f"Escenario {i}",
            "lat": -89.0 - (i * 0.1),
            "lon": float(i * 10),
            "start_utc": "2026-12-01T00:00:00Z",
        }
        for i in range(6)
    ]
    payload = {"scenarios": scenarios}
    response = client.post("/api/v1/compare", json=payload)
    assert response.status_code == 422


def test_compare_scenarios_invalid_coordinate(client: TestClient) -> None:
    """Test invalid coordinate in any scenario returns 422."""
    payload = {
        "scenarios": [
            {
                "label": "Escenario A",
                "lat": -89.66,
                "lon": 222.7,
                "start_utc": "2026-12-01T00:00:00Z",
            },
            {
                "label": "Escenario B fuera de rango",
                "lat": -70.0,  # Invalid
                "lon": 222.7,
                "start_utc": "2026-12-01T00:00:00Z",
            },
        ]
    }
    response = client.post("/api/v1/compare", json=payload)
    assert response.status_code == 422


def test_compare_defaults_match_evaluate() -> None:
    """Shared tuning params must default identically in /compare and /evaluate."""
    from app.schemas.compare import CompareRequest
    from app.schemas.evaluate import EvaluateRequest

    for field in ("duration_days", "step_minutes", "panel_efficiency", "panel_area_m2"):
        assert (
            CompareRequest.model_fields[field].default
            == EvaluateRequest.model_fields[field].default
        ), field
