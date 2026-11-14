"""Unit tests for terrain domain service, slopes, and horizon profiles."""

import math
import numpy as np
import pytest

from app.domain.services.terrain import (
    TerrainService,
    compute_cell_size,
    compute_slopes,
    compute_solar_power_factor,
)
from app.infrastructure.datasets.dem_reader import DEMReader


def test_compute_cell_size() -> None:
    """Test cell size spacing calculations."""
    assert compute_cell_size(12.0, 128) == 93.75
    assert compute_cell_size(6.4, 64) == 100.0
    assert compute_cell_size(25.6, 256) == 100.0


def test_compute_slopes_flat_surface() -> None:
    """A completely flat elevation surface must have 0.0 degrees slope everywhere."""
    grid = np.ones((32, 32), dtype=float) * 2000.0
    slopes = compute_slopes(grid, cell_size_m=50.0)
    assert np.allclose(slopes, 0.0, atol=1e-5)


def test_compute_slopes_45_degree_ramp() -> None:
    """An elevation ramp with dz/dx = 1.0 must have 45.0 degree slope in interior."""
    cell_size = 10.0
    # Gradient in x direction of 1.0 (10 meters rise per 10 meters run)
    x = np.arange(32) * cell_size
    grid = np.tile(x, (32, 1)).astype(float)

    slopes = compute_slopes(grid, cell_size_m=cell_size)
    # Check interior points (away from boundary finite differences)
    interior_slopes = slopes[2:-2, 2:-2]
    assert np.allclose(interior_slopes, 45.0, atol=1e-3)


def test_compute_slopes_clamping() -> None:
    """Extreme gradient values must clamp strictly between 0 and 90 degrees."""
    grid = np.zeros((10, 10))
    grid[5, 5] = 1e9  # Huge spike
    slopes = compute_slopes(grid, cell_size_m=10.0)
    assert np.all(slopes >= 0.0)
    assert np.all(slopes <= 90.0)


def test_solar_power_factor_darkness() -> None:
    """Power factor must be 0.0 when Sun is below horizon or occluded."""
    assert compute_solar_power_factor(sun_el_deg=-1.5, sun_visible=False) == 0.0
    assert compute_solar_power_factor(sun_el_deg=1.5, sun_visible=False) == 0.0
    assert compute_solar_power_factor(sun_el_deg=-0.5, sun_visible=True) == 0.0


def test_solar_power_factor_grazing_sunlight() -> None:
    """Vertical tracking solar panel captures strong power under polar grazing sun."""
    pf_grazing = compute_solar_power_factor(sun_el_deg=1.77, sun_visible=True)
    assert 0.70 <= pf_grazing <= 0.95

    # Higher sun approaches peak 1.0
    pf_high = compute_solar_power_factor(sun_el_deg=5.0, sun_visible=True)
    assert pf_high == 1.0


def test_generate_patch_resolutions() -> None:
    """Test patch generation produces correct array sizes for all permitted resolutions."""
    service = TerrainService()

    for res in [64, 128, 256]:
        patch = service.generate_patch(lat=-88.0, lon=0.0, size_km=10.0, resolution=res)
        assert len(patch.grid.elevations_m) == res * res
        assert len(patch.grid.slopes_deg) == res * res
        assert patch.grid.min_elevation_m <= patch.grid.max_elevation_m
        assert patch.center.lat == -88.0
        assert patch.center.lon == 0.0


def test_horizon_profile_360_points() -> None:
    """Test that horizon profile generates exactly 360 points from 0 to 359 degrees."""
    service = TerrainService()
    profile = service.generate_horizon_profile(lat=-89.0, lon=45.0, num_points=360)

    assert len(profile) == 360
    assert profile[0].az_deg == 0.0
    assert profile[-1].az_deg == 359.0

    # Every point must have a realistic elevation angle
    for p in profile:
        assert -10.0 <= p.horizon_el_deg <= 15.0
