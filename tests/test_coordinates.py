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


def test_polar_projection_matches_lola_crs() -> None:
    """Must match pyproj on the LOLA LDEM_80S CRS (+proj=stere +lat_0=-90 +lon_0=0 +R=1737400)."""
    reference = [
        (-89.66, 222.7, -6991.8, -7576.9),  # Shackleton Rim
        (-86.04, 357.1, -6077.6, 119974.5),  # Malapert Massif (+Y = 0 E)
        (-87.18, 84.3, 85106.2, 8494.7),  # Faustini Rim A (+X = 90 E)
    ]
    for lat, lon, exp_x, exp_y in reference:
        x, y = lat_lon_to_polar_xy(lat, lon)
        assert math.isclose(x, exp_x, abs_tol=0.2), (lat, lon)
        assert math.isclose(y, exp_y, abs_tol=0.2), (lat, lon)
