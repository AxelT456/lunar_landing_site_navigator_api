"""Tests for lunar coordinate domain transformations."""

import math
import pytest
from app.domain.coordinates import lat_lon_to_polar_xy, normalize_longitude


def test_normalize_longitude() -> None:
    """Test normalizing longitudes into [-180, 180) range."""
    assert normalize_longitude(0.0) == 0.0
    assert normalize_longitude(180.0) == -180.0
    assert normalize_longitude(222.7) == pytest.approx(-137.3, abs=1e-5)
    assert normalize_longitude(-180.0) == -180.0
    assert normalize_longitude(360.0) == 0.0


def test_polar_stereographic_projection_pole() -> None:
    """At true South Pole (-90), radial distance must be 0."""
    x, y = lat_lon_to_polar_xy(-90.0, 0.0)
    assert math.isclose(x, 0.0, abs_tol=1e-1)
    assert math.isclose(y, 0.0, abs_tol=1e-1)


def test_polar_stereographic_projection_scale() -> None:
    """Colatitude of 0.34 deg (-89.66) should project to ~10.3 km distance."""
    x, y = lat_lon_to_polar_xy(-89.66, 222.7)
    r = math.hypot(x, y)
    # Approx 10.3 km from pole
    assert 10000.0 < r < 10600.0
