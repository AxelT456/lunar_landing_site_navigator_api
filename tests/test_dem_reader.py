"""Unit tests for analytical polar DEM reader and topography engine."""

import math
import pytest

from app.infrastructure.datasets.dem_reader import DEMReader, LUNAR_SOUTH_POLE_FEATURES


def test_dem_reader_features_catalog() -> None:
    """Verify that prominent lunar south pole features are registered."""
    reader = DEMReader()
    feature_names = [f.name for f in reader.features]
    assert "Shackleton" in feature_names
    assert "Malapert Mountain" in feature_names
    assert "Haworth" in feature_names
    assert "Amundsen" in feature_names
    assert "Shoemaker" in feature_names
    assert "Faustini" in feature_names


def test_dem_reader_elevation_landmarks() -> None:
    """Test elevation values at specific landmarks."""
    reader = DEMReader()

    # Exact Shackleton Rim benchmark elevation
    shackleton_elev = reader.get_elevation(-89.66, 222.7)
    assert shackleton_elev == 2048.5

    # Malapert Mountain peak should be high elevated massif (> 3000m)
    malapert_elev = reader.get_elevation(-86.04, 2.7)
    assert malapert_elev > 3000.0

    # Haworth crater floor should be deeply excavated (< 0m)
    haworth_elev = reader.get_elevation(-87.4, 355.0)
    assert haworth_elev < 0.0


def test_dem_reader_slope_bounds() -> None:
    """Test slope values are physical and within [0, 90] degrees."""
    reader = DEMReader()
    # Shackleton Rim
    slope_shackleton = reader.get_slope(-89.66, 222.7)
    assert slope_shackleton == 6.4

    for lat in [-89.5, -87.0, -84.0]:
        for lon in [0.0, 90.0, 180.0, 270.0]:
            slope = reader.get_slope(lat, lon)
            assert 0.0 <= slope <= 90.0


def test_dem_reader_patch_generation() -> None:
    """Test 2D DEM patch generation."""
    reader = DEMReader()
    elev_grid, slope_grid, min_e, max_e = reader.get_patch(
        lat=-89.0,
        lon=45.0,
        size_km=10.0,
        resolution=64,
    )

    assert elev_grid.shape == (64, 64)
    assert slope_grid.shape == (64, 64)
    assert min_e <= max_e
    assert elev_grid is not None
    assert slope_grid is not None


def test_dem_reader_horizon_elevation_with_curvature() -> None:
    """Test horizon elevation calculation incorporating lunar spherical curvature."""
    reader = DEMReader()
    # Benchmark Shackleton profile
    hor_el_0 = reader.get_horizon_elevation(-89.66, 222.7, 0.0)
    hor_el_90 = reader.get_horizon_elevation(-89.66, 222.7, 90.0)
    assert 0.1 <= hor_el_0 <= 5.0
    assert 0.1 <= hor_el_90 <= 5.0

    # Non-benchmark point horizon profile
    hor_el_other = reader.get_horizon_elevation(-88.0, 0.0, 180.0)
    assert -5.0 <= hor_el_other <= 15.0
