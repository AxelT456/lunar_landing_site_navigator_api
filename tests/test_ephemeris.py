"""Unit tests for ephemeris domain service and celestial coordinate transformations."""

import math
from datetime import datetime, timedelta, timezone
import pytest

from app.domain.services.ephemeris import (
    LUNAR_SYNODIC_DAY_HOURS,
    LIBRATION_CYCLE_HOURS,
    LUNAR_OBLIQUITY_DEG,
    REFERENCE_EPOCH,
    REF_SHACKLETON_LAT,
    REF_SHACKLETON_LON,
    EphemerisService,
    az_el_to_visor_vector,
    calculate_earth_position,
    calculate_sun_position,
)


def test_visor_vector_unit_norm() -> None:
    """Test that generated 3D vectors have strictly unit norm ||v|| = 1.0."""
    test_angles = [
        (0.0, 0.0),
        (45.0, 30.0),
        (90.0, 0.0),
        (135.0, -15.0),
        (180.0, 1.54),
        (225.0, 60.0),
        (270.0, -2.0),
        (315.0, -89.0),
        (57.38, 1.77),
        (143.96, -2.06),
    ]

    for az, el in test_angles:
        # Test unrounded
        vec_exact = az_el_to_visor_vector(az, el, round_digits=None)
        norm_exact = math.sqrt(sum(c**2 for c in vec_exact))
        assert math.isclose(norm_exact, 1.0, abs_tol=1e-7)

        # Test rounded
        vec_rounded = az_el_to_visor_vector(az, el, round_digits=3)
        norm_rounded = math.sqrt(sum(c**2 for c in vec_rounded))
        assert math.isclose(norm_rounded, 1.0, abs_tol=0.01)


def test_visor_reference_frame_cardinal_directions() -> None:
    """
    Test coordinate axes mapping in the visor reference frame:
    [+X East, +Y Up, -Z North].
    """
    # Due North: az = 0, el = 0 -> +Y=0, +X=0, -Z=North => [0, 0, -1]
    v_north = az_el_to_visor_vector(0.0, 0.0, round_digits=3)
    assert v_north == [0.0, 0.0, -1.0]

    # Due East: az = 90, el = 0 -> +X=1, +Y=0, Z=0 => [1, 0, 0]
    v_east = az_el_to_visor_vector(90.0, 0.0, round_digits=3)
    assert v_east == [1.0, 0.0, 0.0]

    # Due South: az = 180, el = 0 -> +X=0, +Y=0, Z=1 => [0, 0, 1]
    v_south = az_el_to_visor_vector(180.0, 0.0, round_digits=3)
    assert v_south == [0.0, 0.0, 1.0]

    # Due West: az = 270, el = 0 -> +X=-1, +Y=0, Z=0 => [-1, 0, 0]
    v_west = az_el_to_visor_vector(270.0, 0.0, round_digits=3)
    assert v_west == [-1.0, 0.0, 0.0]

    # Zenith / Directly Up: el = 90 -> +Y=1 => [0, 1, 0]
    v_up = az_el_to_visor_vector(0.0, 90.0, round_digits=3)
    assert v_up == [0.0, 1.0, 0.0]

    # Nadir / Directly Down: el = -90 -> +Y=-1 => [0, -1, 0]
    v_down = az_el_to_visor_vector(0.0, -90.0, round_digits=3)
    assert v_down == [0.0, -1.0, 0.0]


def test_sun_synodic_diurnal_periodicity() -> None:
    """Test Sun completes exactly 360 degrees rotation in one lunar synodic day (~708.73h)."""
    t0 = REFERENCE_EPOCH
    t1 = t0 + timedelta(hours=LUNAR_SYNODIC_DAY_HOURS)

    sun_t0 = calculate_sun_position(REF_SHACKLETON_LAT, REF_SHACKLETON_LON, t0)
    sun_t1 = calculate_sun_position(REF_SHACKLETON_LAT, REF_SHACKLETON_LON, t1)

    # After exactly one synodic period, azimuth must complete full 360-deg circuit
    diff_az = abs(sun_t1.azimuth_deg - sun_t0.azimuth_deg)
    assert diff_az < 0.1 or math.isclose(diff_az, 360.0, abs_tol=0.1)


def test_sun_polar_grazing_elevation_bounds() -> None:
    """Test that solar elevation stays within polar grazing bounds (~ +/- 2 deg)."""
    dt = REFERENCE_EPOCH
    for step in range(24 * 30):  # Check hourly for 30 days
        t = dt + timedelta(hours=step)
        sun_pos = calculate_sun_position(REF_SHACKLETON_LAT, REF_SHACKLETON_LON, t)
        assert -2.0 <= sun_pos.elevation_deg <= 2.0


def test_earth_libration_periodicity() -> None:
    """Test Earth azimuth and elevation oscillate periodically with ~27.32d cycle."""
    t0 = REFERENCE_EPOCH
    t_full_cycle = t0 + timedelta(hours=LIBRATION_CYCLE_HOURS)

    e_t0 = calculate_earth_position(REF_SHACKLETON_LAT, REF_SHACKLETON_LON, t0)
    e_cycle = calculate_earth_position(REF_SHACKLETON_LAT, REF_SHACKLETON_LON, t_full_cycle)

    assert math.isclose(e_t0.azimuth_deg, e_cycle.azimuth_deg, abs_tol=0.2)
    assert math.isclose(e_t0.elevation_deg, e_cycle.elevation_deg, abs_tol=0.2)


def test_ephemeris_service_timeline_length() -> None:
    """Test EphemerisService generates correct snapshot counts across requested timeline."""
    service = EphemerisService()
    start = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    duration_days = 7.0
    step_minutes = 60

    timeline = service.get_timeline(
        lat=-89.0,
        lon=0.0,
        start_utc=start,
        duration_days=duration_days,
        step_minutes=step_minutes,
    )

    expected_steps = int((duration_days * 24 * 60) / step_minutes)
    assert len(timeline) == expected_steps
    assert timeline[0].timestamp_utc == start
    assert timeline[-1].timestamp_utc == start + timedelta(minutes=(expected_steps - 1) * step_minutes)


def test_ephemeris_polar_latitude_bounds() -> None:
    """Test ephemeris calculation behaves consistently across full South Pole latitude range."""
    dt = datetime(2026, 12, 1, 12, 0, 0, tzinfo=timezone.utc)
    service = EphemerisService()

    for lat in [-90.0, -88.0, -85.0, -80.0]:
        for lon in [-180.0, 0.0, 120.0, 222.7, 359.0]:
            snapshot = service.get_snapshot(lat, lon, dt)
            assert 0.0 <= snapshot.sun.azimuth_deg < 360.0
            assert 0.0 <= snapshot.earth.azimuth_deg < 360.0
            assert len(snapshot.sun.vector_3d) == 3
            assert len(snapshot.earth.vector_3d) == 3
