"""Unit and NASA reference validation tests for app.engine."""

from __future__ import annotations

import math
import os
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.crs import CRS
from rasterio.transform import from_origin

import app.engine as engine
from app.engine.coordinates import (
    MOON_RADIUS_M,
    az_el_to_vector_3d,
    latlon_from_polar_stereographic,
    polar_stereographic_from_latlon,
)
from app.engine.terrain import _RASTER_CACHE

# Tests marked with these run only where the real NASA datasets are mounted
# (DATA_DIR, /data in the server container). CI has no datasets and skips them.
_DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
requires_lola = pytest.mark.skipif(
    not any((_DATA_DIR / "raw" / "lola").glob("LDEM_80S_*MPP_ADJ.TIF")),
    reason="Real LOLA polar DEM not present in DATA_DIR",
)
requires_spice = pytest.mark.skipif(
    not any((_DATA_DIR / "raw" / "spice").glob("*.bsp")),
    reason="SPICE kernels not present in DATA_DIR",
)


def _clear_raster_cache() -> None:
    for _, (_, ds) in list(_RASTER_CACHE.items()):
        try:
            ds.close()
        except Exception:
            pass
    _RASTER_CACHE.clear()


def test_public_contract_exports() -> None:
    expected = {
        "polar_stereographic_from_latlon",
        "latlon_from_polar_stereographic",
        "sample_point_terrain",
        "extract_terrain_patch",
        "compute_horizon_profile",
        "evaluate_site_mission",
        "moon_center_ephemeris",
    }
    assert expected.issubset(set(engine.__all__))
    for name in expected:
        assert callable(getattr(engine, name))


def test_polar_stereographic_axes_and_roundtrip() -> None:
    assert polar_stereographic_from_latlon(-90.0, 0.0) == (0.0, 0.0)
    assert latlon_from_polar_stereographic(0.0, 0.0) == (-90.0, 0.0)

    # +Y is lon 0 deg, +X is lon 90 deg E
    x0, y0 = polar_stereographic_from_latlon(-85.0, 0.0)
    assert abs(x0) < 1e-8
    assert y0 > 150_000.0

    x90, y90 = polar_stereographic_from_latlon(-85.0, 90.0)
    assert x90 > 150_000.0
    assert abs(y90) < 1e-8
    assert math.isclose(x90, y0, rel_tol=1e-12)

    for lat, lon in [(-89.66, 222.7), (-89.45, 222.7), (-86.04, 357.1), (-84.2, -45.0)]:
        px, py = polar_stereographic_from_latlon(lat, lon)
        rlat, rlon = latlon_from_polar_stereographic(px, py)
        assert math.isclose(rlat, lat, abs_tol=1e-8)
        assert math.isclose(rlon, lon % 360.0, abs_tol=1e-8)


def test_polar_stereographic_matches_lola_geotiff_crs() -> None:
    pyproj = pytest.importorskip("pyproj")
    ldem_path = Path(os.environ.get("DATA_DIR", "/data")) / "raw" / "lola" / "LDEM_80S_80MPP_ADJ.TIF"
    if not ldem_path.exists():
        ldem_path = Path("/data/raw/lola/LDEM_80S_80MPP_ADJ.TIF")
    if not ldem_path.exists():
        pytest.skip("Real LOLA LDEM GeoTIFF not present on disk")

    with rasterio.open(ldem_path) as ds:
        tf = pyproj.Transformer.from_crs(ds.crs.geodetic_crs, ds.crs, always_xy=True)
        for lat, lon in [(-89.66, 222.7), (-89.45, 222.7), (-86.04, 357.1), (-82.5, 135.0)]:
            ref_x, ref_y = tf.transform(lon, lat)
            got_x, got_y = polar_stereographic_from_latlon(lat, lon)
            assert math.isclose(got_x, ref_x, abs_tol=1e-5)
            assert math.isclose(got_y, ref_y, abs_tol=1e-5)


def test_az_el_to_vector_3d_threejs_frame() -> None:
    # North (az=0, el=0) -> -Z
    v_n = az_el_to_vector_3d(0.0, 0.0)
    assert np.allclose(v_n, [0.0, 0.0, -1.0], atol=1e-12)
    # East (az=90, el=0) -> +X
    v_e = az_el_to_vector_3d(90.0, 0.0)
    assert np.allclose(v_e, [1.0, 0.0, 0.0], atol=1e-12)
    # South (az=180, el=0) -> +Z
    v_s = az_el_to_vector_3d(180.0, 0.0)
    assert np.allclose(v_s, [0.0, 0.0, 1.0], atol=1e-12)
    # Zenith (el=90) -> +Y
    v_u = az_el_to_vector_3d(42.0, 90.0)
    assert np.allclose(v_u, [0.0, 1.0, 0.0], atol=1e-12)
    # Right-handedness: East x Up = South (+Z)
    assert np.allclose(np.cross(v_e, v_u), v_s, atol=1e-12)


@requires_lola
def test_sample_point_terrain_real_lola() -> None:
    pt = engine.sample_point_terrain(-89.66, 222.7)
    assert set(pt.keys()) == {"elevation_m", "slope_deg", "polar_x_m", "polar_y_m", "source"}
    assert pt["source"] == "lola"
    assert -8000.0 <= pt["elevation_m"] <= 8000.0
    assert 0.0 <= pt["slope_deg"] <= 90.0


def test_sample_point_terrain_scaled_int16_geotiff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_raster_cache()
    lola_dir = tmp_path / "raw" / "lola"
    lola_dir.mkdir(parents=True)

    crs = CRS.from_proj4("+proj=stere +lat_0=-90 +lat_ts=-90 +lon_0=0 +x_0=0 +y_0=0 +R=1737400 +units=m +no_defs")
    transform = from_origin(-4000.0, 4000.0, 80.0, 80.0)
    # 100x100 int16 raster with scale=0.5, offset=-100.0, nodata=-32768 -> physical elevation = 2000 * 0.5 - 100 = 900.0 m
    dem_data = np.full((100, 100), 2000, dtype=np.int16)
    dem_data[0, 0] = -32768
    with rasterio.open(
        lola_dir / "LDEM_80S_80MPP_ADJ.TIF",
        "w",
        driver="GTiff",
        height=100,
        width=100,
        count=1,
        dtype="int16",
        crs=crs,
        transform=transform,
        nodata=-32768,
    ) as dst:
        dst.write(dem_data, 1)
        dst.scales = (0.5,)
        dst.offsets = (-100.0,)

    slope_data = np.full((100, 100), 120, dtype=np.int16)
    with rasterio.open(
        lola_dir / "LDSM_80S_80MPP_ADJ.TIF",
        "w",
        driver="GTiff",
        height=100,
        width=100,
        count=1,
        dtype="int16",
        crs=crs,
        transform=transform,
        nodata=-32768,
    ) as dst:
        dst.write(slope_data, 1)
        dst.scales = (0.1,)
        dst.offsets = (0.0,)

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    pt = engine.sample_point_terrain(-90.0, 0.0)
    assert pt["source"] == "lola"
    assert math.isclose(pt["elevation_m"], 900.0, abs_tol=1e-4)
    assert math.isclose(pt["slope_deg"], 12.0, abs_tol=1e-4)
    _clear_raster_cache()


def test_terrain_patch_and_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    patch = engine.extract_terrain_patch(-89.66, 222.7, size_km=12.0, resolution=64)
    assert set(patch.keys()) == {"center", "grid"}
    grid = patch["grid"]
    assert grid["resolution"] == 64
    assert math.isclose(grid["cell_size_m"], 12_000.0 / 63.0, rel_tol=1e-6)
    assert len(grid["elevations_m"]) == 64 * 64
    assert len(grid["slopes_deg"]) == 64 * 64
    assert all(math.isfinite(v) for v in grid["elevations_m"])
    assert all(math.isfinite(v) and v >= 0.0 for v in grid["slopes_deg"])
    assert grid["min_elevation_m"] <= grid["max_elevation_m"]

    # Missing GeoTIFFs should cleanly trigger deterministic fallback
    _clear_raster_cache()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    fb_pt = engine.sample_point_terrain(-89.66, 222.7)
    assert fb_pt["source"] == "fallback"
    fb_patch = engine.extract_terrain_patch(-89.66, 222.7, size_km=6.0, resolution=32)
    assert len(fb_patch["grid"]["elevations_m"]) == 32 * 32
    _clear_raster_cache()


def test_horizon_profile_and_lunar_curvature(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = engine.compute_horizon_profile(-89.66, 222.7, max_radius_km=60.0)
    assert len(profile) == 360
    assert [p["az_deg"] for p in profile] == list(range(360))
    assert all(math.isfinite(p["horizon_el_deg"]) and -25.0 <= p["horizon_el_deg"] <= 65.0 for p in profile)

    # Verify lunar curvature correction (-d^2 / (2*R)) on synthetic DEM with a ridge due North
    _clear_raster_cache()
    lola_dir = tmp_path / "raw" / "lola"
    lola_dir.mkdir(parents=True)
    crs = CRS.from_proj4("+proj=stere +lat_0=-90 +lat_ts=-90 +lon_0=0 +x_0=0 +y_0=0 +R=1737400 +units=m +no_defs")
    # 700x700 grid at 80 m/px covering [-28000, +28000]
    transform = from_origin(-28000.0, 28000.0, 80.0, 80.0)
    dem = np.zeros((700, 700), dtype=np.float32)
    # Place a +1000 m peak at y = +20000 m, x = 0 m (az=0 deg from South Pole, distance=20 km)
    # Row 0 is y=+28000, row index for y=+20000 is (28000 - 20000)/80 = 100; col for x=0 is 350
    dem[98:103, 348:353] = 1000.0
    with rasterio.open(
        lola_dir / "LDEM_80S_80MPP_ADJ.TIF",
        "w",
        driver="GTiff",
        height=700,
        width=700,
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=np.nan,
    ) as dst:
        dst.write(dem, 1)

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    curv_profile = engine.compute_horizon_profile(-90.0, 0.0, max_radius_km=25.0)
    az0_el = curv_profile[0]["horizon_el_deg"]
    flat_earth_el = math.degrees(math.atan2(1000.0, 20_000.0))  # ~2.862 deg
    # Curvature drop at 20 km is d^2 / (2*R) ~ 115.1 m -> ~2.53 deg (< flat_earth_el by ~0.33 deg)
    expected_el = math.degrees(
        math.atan2(
            (MOON_RADIUS_M + 1000.0) * math.cos(20_000.0 / MOON_RADIUS_M) - MOON_RADIUS_M,
            (MOON_RADIUS_M + 1000.0) * math.sin(20_000.0 / MOON_RADIUS_M),
        )
    )
    assert math.isclose(az0_el, expected_el, abs_tol=0.05)
    assert az0_el < flat_earth_el - 0.25
    _clear_raster_cache()


@requires_spice
def test_evaluate_site_mission_jpl_horizons_validation() -> None:
    # Reference live query from NASA JPL Horizons for lat=-89.45, lon=222.7, elev=0 at 2026-12-01T00:00:00Z:
    # Sun: az = 57.376893 deg, el = 1.766700 deg
    # Earth: az = 143.960668 deg, el = -2.054888 deg
    flat_horizon = [{"az_deg": az, "horizon_el_deg": 0.0} for az in range(360)]
    res = engine.evaluate_site_mission(
        lat=-89.45,
        lon=222.7,
        elevation_m=0.0,
        slope_deg=5.0,
        horizon_profile=flat_horizon,
        start_utc="2026-12-01T00:00:00Z",
        duration_days=1.0,
        step_minutes=60,
    )
    t0 = res["timeline"][0]
    assert t0["timestamp_utc"] == "2026-12-01T00:00:00Z"
    assert abs(t0["sun_az_deg"] - 57.376893) < 0.5
    assert abs(t0["sun_el_deg"] - 1.766700) < 0.5
    assert abs(t0["earth_az_deg"] - 143.960668) < 0.5
    assert abs(t0["earth_el_deg"] - (-2.054888)) < 0.5
    assert t0["sun_visible"] is True
    assert t0["earth_visible"] is False
    assert t0["power_factor"] > 0.9


def test_evaluate_site_mission_summary_and_statuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # High horizon ridge in the north-east (az 0..90) to force sunset transition over 14 days
    horizon = [
        {"az_deg": az, "horizon_el_deg": 0.0 if az < 330 else 3.0}
        for az in range(360)
    ]
    res = engine.evaluate_site_mission(
        lat=-89.45,
        lon=222.7,
        elevation_m=1500.0,
        slope_deg=6.0,
        horizon_profile=horizon,
        start_utc="2026-12-01T00:00:00Z",
        duration_days=14.0,
        step_minutes=60,
    )
    assert res["status"] in {"GO", "CAUTION", "NO-GO"}
    assert len(res["reasons"]) >= 3
    assert len(res["timeline"]) == 14 * 24
    summary = res["summary"]
    assert 0.0 <= summary["sun_visible_pct"] <= 100.0
    assert 0.0 <= summary["earth_visible_pct"] <= 100.0
    assert 0.0 <= summary["avg_power_factor"] <= 1.0
    assert summary["max_continuous_sun_hours"] <= 14.0 * 24.0
    assert summary["next_sunset_utc"] is not None
    assert summary["hours_to_next_sunset"] is not None

    # Hazardous slope > 15 deg + blocked horizon -> NO-GO
    blocked = [{"az_deg": az, "horizon_el_deg": 25.0} for az in range(360)]
    nogo = engine.evaluate_site_mission(
        lat=-89.45,
        lon=222.7,
        elevation_m=0.0,
        slope_deg=18.5,
        horizon_profile=blocked,
        start_utc="2026-12-01T00:00:00Z",
        duration_days=2.0,
        step_minutes=120,
    )
    assert nogo["status"] == "NO-GO"
    assert nogo["summary"]["sun_visible_pct"] == 0.0
    assert nogo["summary"]["estimated_peak_power_w"] == 0.0

    # Offline fallback without SPICE kernels
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    fb_res = engine.evaluate_site_mission(
        lat=-89.45,
        lon=222.7,
        elevation_m=0.0,
        slope_deg=4.0,
        horizon_profile=[{"az_deg": az, "horizon_el_deg": 0.0} for az in range(360)],
        start_utc="2026-12-01T00:00:00Z",
        duration_days=0.5,
        step_minutes=60,
    )
    assert len(fb_res["timeline"]) == 12
    assert fb_res["status"] in {"GO", "CAUTION", "NO-GO"}


@requires_lola
@requires_spice
def test_evaluate_real_connecting_ridge_go_and_earthset() -> None:
    pt = engine.sample_point_terrain(-89.45, 222.7)
    hor = engine.compute_horizon_profile(-89.45, 222.7, max_radius_km=60.0)
    res = engine.evaluate_site_mission(
        lat=-89.45,
        lon=222.7,
        elevation_m=pt["elevation_m"],
        slope_deg=pt["slope_deg"],
        horizon_profile=hor,
        start_utc="2026-12-01T00:00:00Z",
        duration_days=28.0,
        step_minutes=60,
    )
    assert res["status"] == "GO"
    summary = res["summary"]
    assert summary["sun_visible_pct"] > 50.0
    assert summary["earth_visible_pct"] > 15.0
    assert summary["next_sunset_utc"] is not None
    assert summary["next_earthset_utc"] is not None
    assert summary["hours_to_next_earthset"] > 0.0


def _write_polar_tif(path: Path, val: float, res_m: float = 80.0) -> None:
    crs = CRS.from_proj4("+proj=stere +lat_0=-90 +lat_ts=-90 +lon_0=0 +x_0=0 +y_0=0 +R=1737400 +units=m +no_defs")
    half = 40_000.0
    size = int(round((2.0 * half) / res_m))
    transform = from_origin(-half, half, res_m, res_m)
    arr = np.full((size, size), val, dtype=np.float32)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=size,
        width=size,
        count=1,
        dtype="float32",
        crs=crs,
        transform=transform,
        nodata=np.nan,
    ) as dst:
        dst.write(arr, 1)


def test_polar_multiresolution_priority_and_tmp_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_raster_cache()
    lola_dir = tmp_path / "raw" / "lola"
    lola_dir.mkdir(parents=True)

    _write_polar_tif(lola_dir / "LDEM_80S_80MPP_ADJ.TIF", 100.0, 80.0)
    _write_polar_tif(lola_dir / "LDSM_80S_80MPP_ADJ.TIF", 8.0, 80.0)
    _write_polar_tif(lola_dir / "LDEM_80S_40MPP_ADJ.TIF", 400.0, 40.0)
    _write_polar_tif(lola_dir / "LDSM_80S_40MPP_ADJ.TIF", 4.0, 40.0)
    # Incomplete .tmp higher tier must be ignored
    _write_polar_tif(lola_dir / "LDEM_80S_20MPP_ADJ.TIF.tmp", 999.0, 20.0)

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    pt_40 = engine.sample_point_terrain(-89.5, 10.0)
    assert pt_40["source"] == "lola"
    assert math.isclose(pt_40["elevation_m"], 400.0, abs_tol=1e-4)
    assert math.isclose(pt_40["slope_deg"], 4.0, abs_tol=1e-4)

    patch_40 = engine.extract_terrain_patch(-89.5, 10.0, size_km=2.0, resolution=16)
    assert math.isclose(patch_40["center"]["elevation_m"], 400.0, abs_tol=1e-4)
    assert math.isclose(patch_40["grid"]["min_elevation_m"], 400.0, abs_tol=1e-4)

    # Once 20MPP is finalized without .tmp, it takes priority
    _write_polar_tif(lola_dir / "LDEM_80S_20MPP_ADJ.TIF", 200.0, 20.0)
    _write_polar_tif(lola_dir / "LDSM_80S_20MPP_ADJ.TIF", 2.0, 20.0)
    pt_20 = engine.sample_point_terrain(-89.5, 10.0)
    assert pt_20["source"] == "lola"
    assert math.isclose(pt_20["elevation_m"], 200.0, abs_tol=1e-4)
    assert math.isclose(pt_20["slope_deg"], 2.0, abs_tol=1e-4)
    _clear_raster_cache()


def test_global_dem_svs_km_cos_lat_slope_and_antimeridian(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_raster_cache()
    global_dir = tmp_path / "raw" / "lola_global"
    global_dir.mkdir(parents=True)

    # 1 deg/px SVS-style un-georeferenced float32 DEM in kilometers relative to 1737.4 km
    H, W = 180, 360
    lons_w = (np.arange(W, dtype=np.float64) + 0.5) / float(W) * 360.0 - 180.0
    lats = 90.0 - (np.arange(H, dtype=np.float64) + 0.5) / float(H) * 180.0
    lon_grid, lat_grid = np.meshgrid(lons_w, lats)

    # Constant east-west slope tan(alpha)=0.1 at lat=+60 deg (cos(60)=0.5), plus ridge due North of (0, 179.95)
    target_tan = 0.1
    elev_km = (
        0.001 * target_tan * MOON_RADIUS_M * math.cos(math.radians(60.0)) * np.radians(lon_grid)
    ).astype(np.float32)
    # Place a +2.0 km peak at lat=+1.5 deg, lon=-179.5 deg (near antimeridian)
    r_peak = int((90.0 - 1.5) / 180.0 * H)
    elev_km[r_peak, 0] = 2.0

    with rasterio.open(
        global_dir / "ldem_64.tif",
        "w",
        driver="GTiff",
        height=H,
        width=W,
        count=1,
        dtype="float32",
    ) as dst:
        dst.write(elev_km, 1)

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    pt = engine.sample_point_terrain(60.0, 20.0)
    assert pt["source"] == "lola_global"
    expected_elev_m = target_tan * MOON_RADIUS_M * math.cos(math.radians(60.0)) * math.radians(20.0)
    assert math.isclose(pt["elevation_m"], expected_elev_m, rel_tol=0.02)
    # Horn 3x3 metric slope at lat=60 deg must account for cos(60)=0.5 -> atan(0.1) = 5.7106 deg
    expected_slope_deg = math.degrees(math.atan(target_tan))
    assert math.isclose(pt["slope_deg"], expected_slope_deg, abs_tol=0.25)

    # Antimeridian wrap patch & horizon test around (0.0, 179.95)
    patch = engine.extract_terrain_patch(0.0, 179.95, size_km=40.0, resolution=16)
    assert len(patch["grid"]["elevations_m"]) == 16 * 16
    assert all(math.isfinite(v) for v in patch["grid"]["elevations_m"])

    hor = engine.compute_horizon_profile(0.0, 179.95, max_radius_km=60.0)
    assert len(hor) == 360
    assert all(math.isfinite(p["horizon_el_deg"]) for p in hor)
    _clear_raster_cache()


def test_global_dem_prefers_ldem_256_georeferenced_0_360(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_raster_cache()
    global_dir = tmp_path / "raw" / "lola_global"
    global_dir.mkdir(parents=True)

    # Coarse SVS-style DEM: flat 1 km everywhere (must lose to ldem_256.tif)
    with rasterio.open(
        global_dir / "ldem_64.tif", "w", driver="GTiff", height=90, width=180, count=1, dtype="float32"
    ) as dst:
        dst.write(np.full((90, 180), 1.0, dtype=np.float32), 1)

    # Layout written by scripts/build_global_dem.sh: geographic 0..360, int16 DN in half meters
    H, W = 180, 360
    lons = (np.arange(W, dtype=np.float64) + 0.5) / float(W) * 360.0
    dn = np.broadcast_to((lons * 2.0).astype(np.int16), (H, W))  # height (m) = lon in degrees
    with rasterio.open(
        global_dir / "ldem_256.tif",
        "w",
        driver="GTiff",
        height=H,
        width=W,
        count=1,
        dtype="int16",
        crs=CRS.from_proj4(f"+proj=longlat +R={MOON_RADIUS_M} +no_defs"),
        transform=from_origin(0.0, 90.0, 1.0, 1.0),
    ) as dst:
        dst.write(dn, 1)
        dst.scales = (0.5,)
        dst.offsets = (0.0,)

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    east = engine.sample_point_terrain(10.0, 200.5)
    assert east["source"] == "lola_global"
    assert math.isclose(east["elevation_m"], 200.5, abs_tol=0.6)
    # Negative longitudes map onto 180..360
    west = engine.sample_point_terrain(-20.0, -30.5)
    assert math.isclose(west["elevation_m"], 329.5, abs_tol=0.6)

    patch = engine.extract_terrain_patch(10.0, 200.5, size_km=40.0, resolution=16)
    assert patch["grid"]["source"] == "lola_global"
    assert math.isclose(patch["grid"]["native_resolution_m"], 2.0 * math.pi * MOON_RADIUS_M / W, abs_tol=0.1)
    _clear_raster_cache()


def test_real_global_apollo11_and_south_pole_multires() -> None:
    _clear_raster_cache()
    global_path = Path("/data/raw/lola_global/ldem_64.tif")
    if not global_path.exists():
        global_path = Path(os.environ.get("DATA_DIR", "/data")) / "raw" / "lola_global" / "ldem_64.tif"
    if not global_path.exists():
        pytest.skip("Real global ldem_64.tif not present on disk")

    ap11 = engine.sample_point_terrain(0.674, 23.473)
    assert ap11["source"] == "lola_global"
    assert -2200.0 <= ap11["elevation_m"] <= -1600.0
    assert 0.0 <= ap11["slope_deg"] <= 20.0

    patch = engine.extract_terrain_patch(0.674, 23.473, size_km=12.0, resolution=64)
    assert len(patch["grid"]["elevations_m"]) == 64 * 64
    assert patch["grid"]["min_elevation_m"] < patch["grid"]["max_elevation_m"]

    hor = engine.compute_horizon_profile(0.674, 23.473, max_radius_km=60.0)
    assert len(hor) == 360
    assert all(math.isfinite(p["horizon_el_deg"]) for p in hor)

    eval_res = engine.evaluate_site_mission(
        lat=0.674,
        lon=23.473,
        elevation_m=ap11["elevation_m"],
        slope_deg=ap11["slope_deg"],
        horizon_profile=hor,
        start_utc="2026-12-01T00:00:00Z",
        duration_days=14.0,
        step_minutes=120,
    )
    assert eval_res["status"] in {"GO", "CAUTION", "NO-GO"}
    assert eval_res["summary"]["earth_visible_pct"] > 90.0


def test_status_checks_mirror_reasons() -> None:
    from app.engine.ephemeris import _compute_status_and_reasons

    cases = [
        ((5.0, 80.0, 60.0), "GO", ["SLOPE_SAFE", "SUN_HIGH", "DTE_AVAILABLE"]),
        ((12.0, 40.0, 10.0), "CAUTION", ["SLOPE_MODERATE", "SUN_LIMITED", "DTE_REDUCED"]),
        ((16.0, 10.0, 0.0), "NO-GO", ["SLOPE_CRITICAL", "SUN_CRITICAL", "DTE_NONE"]),
    ]
    for args, status, codes in cases:
        got_status, reasons, checks = _compute_status_and_reasons(*args)
        assert got_status == status
        assert [c["code"] for c in checks] == codes
        assert len(reasons) == len(checks)
        levels = {c["level"] for c in checks}
        assert ("nogo" in levels) == (status == "NO-GO")
        assert all(c["message"] and c["unit"] in {"deg", "pct"} for c in checks)


@requires_spice
def test_moon_center_ephemeris_matches_site_view() -> None:
    eph = engine.moon_center_ephemeris("2026-12-01T00:00:00Z")
    assert eph["frame"].startswith("MOON_")
    for body in ("sun", "earth"):
        assert np.linalg.norm(eph[body]["direction"]) == pytest.approx(1.0, abs=1e-5)
    # The sub-solar latitude stays within the lunar obliquity; Earth within libration.
    assert abs(eph["sun"]["sub_lat_deg"]) < 1.7
    assert abs(eph["earth"]["sub_lat_deg"]) < 7.0
    assert 356_000 < eph["earth"]["distance_km"] < 407_000
    # Seen from the south pole, the Sun's elevation is about minus the sub-solar latitude.
    flat = [{"az_deg": az, "horizon_el_deg": -90.0} for az in range(360)]
    site = engine.evaluate_site_mission(
        lat=-90.0, lon=0.0, elevation_m=0.0, slope_deg=0.0, horizon_profile=flat,
        start_utc="2026-12-01T00:00:00Z", duration_days=1.0 / 24.0, step_minutes=60,
    )
    assert site["timeline"][0]["sun_el_deg"] == pytest.approx(-eph["sun"]["sub_lat_deg"], abs=0.05)
