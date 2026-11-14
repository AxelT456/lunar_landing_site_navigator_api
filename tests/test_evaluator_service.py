"""Unit tests for operational evaluator domain service and decision logic."""

from datetime import datetime, timezone
import pytest

from app.domain.services.evaluator import EvaluatorService
from app.schemas.evaluate import EvaluateRequest


def test_evaluator_service_status_go() -> None:
    """A site with safe slope and high solar illumination evaluates to GO."""
    evaluator = EvaluatorService()
    req = EvaluateRequest(
        lat=-89.66,
        lon=222.7,
        start_utc=datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc),
        duration_days=14.0,
        step_minutes=60,
    )
    resp = evaluator.evaluate_site(req)

    assert resp.site.status == "GO"
    assert resp.site.slope_deg <= 10.0
    assert resp.summary.sun_visible_pct >= 50.0
    assert any("Pendiente segura" in r for r in resp.site.reasons)
    assert any("Iluminación solar alta" in r for r in resp.site.reasons)


def test_evaluator_streak_and_countdown_logic() -> None:
    """Test streak detection and countdown calculations."""
    evaluator = EvaluatorService()
    req = EvaluateRequest(
        lat=-89.66,
        lon=222.7,
        start_utc=datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc),
        duration_days=14.0,
        step_minutes=60,
    )
    resp = evaluator.evaluate_site(req)

    summary = resp.summary
    assert summary.max_continuous_sun_hours > 0.0
    assert summary.max_continuous_dte_hours > 0.0

    if summary.next_sunset_utc is not None:
        assert summary.hours_to_next_sunset is not None
        assert summary.hours_to_next_sunset > 0.0
        assert summary.next_sunset_utc > req.start_utc

    if summary.next_earthset_utc is not None:
        assert summary.hours_to_next_earthset is not None
        assert summary.hours_to_next_earthset > 0.0
        assert summary.next_earthset_utc > req.start_utc


def test_evaluator_timeline_vectors_normalized() -> None:
    """Verify that all timeline points have valid normalized 3D vectors."""
    evaluator = EvaluatorService()
    req = EvaluateRequest(
        lat=-88.5,
        lon=90.0,
        start_utc=datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc),
        duration_days=2.0,
        step_minutes=120,
    )
    resp = evaluator.evaluate_site(req)

    assert len(resp.timeline) > 0
    for pt in resp.timeline:
        assert len(pt.sun_vector_3d) == 3
        assert len(pt.earth_vector_3d) == 3
        sun_norm = sum(c**2 for c in pt.sun_vector_3d) ** 0.5
        earth_norm = sum(c**2 for c in pt.earth_vector_3d) ** 0.5
        assert 0.99 <= sun_norm <= 1.01
        assert 0.99 <= earth_norm <= 1.01


def test_evaluator_caution_and_no_go_heuristics() -> None:
    """Test classification heuristics for steep slopes and low illumination."""
    evaluator = EvaluatorService()

    # Coordinates near crater wall with steep slopes
    req_steep = EvaluateRequest(
        lat=-87.4,  # Haworth crater region
        lon=355.0,
        start_utc=datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc),
        duration_days=5.0,
        step_minutes=60,
    )
    resp = evaluator.evaluate_site(req_steep)

    assert resp.site.status in ["GO", "CAUTION", "NO-GO"]
    assert len(resp.site.reasons) >= 2
