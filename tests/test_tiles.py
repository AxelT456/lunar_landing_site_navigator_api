"""South pole terrain tiles (handoff P-008): tiling scheme, Terrarium PNG and the HTTP endpoint.

Uses small synthetic rasters in a temporary DATA_DIR: a LOLA-style polar DEM whose elevation
is a known plane in polar metres, and an SVS-style global DEM. No NASA datasets needed.
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

import numpy as np
import pytest
import rasterio
from fastapi.testclient import TestClient
from rasterio.transform import from_origin

from app.engine import tiles
from app.engine.coordinates import polar_stereographic_from_latlon
from app.main import app

POLAR_RES_M = 80.0
POLAR_HALF_M = 24_000.0  # synthetic polar raster covers |x|, |y| <= 24 km (lat < about -89.2)
GLOBAL_ELEV_M = 1200.0

# z=8 tile touching the pole: lat -89.297..-90, lon 30.94..31.64, fully inside the polar raster.
POLE_TILE = (8, 255, 300)
# z=4 tile from -78.75 to -90: crosses -80 and exceeds the polar raster, so it needs the global DEM.
MIXED_TILE = (4, 15, 16)


def plane_m(x: np.ndarray | float, y: np.ndarray | float) -> np.ndarray | float:
    return 500.0 + 0.002 * x - 0.001 * y


# ---------------------------------------------------------------- helpers


def decode_terrarium_png(data: bytes) -> np.ndarray:
    """Minimal PNG reader for 8-bit RGB (filters None/Up), returning Terrarium elevations."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, width = 8, b"", 0
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        kind = data[pos + 4 : pos + 8]
        body = data[pos + 8 : pos + 8 + length]
        (crc,) = struct.unpack(">I", data[pos + 8 + length : pos + 12 + length])
        assert crc == zlib.crc32(kind + body) & 0xFFFFFFFF
        if kind == b"IHDR":
            width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", body)
            assert (depth, color, interlace) == (8, 2, 0)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + length
    raw = np.frombuffer(zlib.decompress(idat), dtype=np.uint8).reshape(height, 1 + width * 3)
    rows = np.zeros((height, width * 3), dtype=np.uint8)
    prev = np.zeros(width * 3, dtype=np.uint8)
    for r in range(height):
        ftype, line = raw[r, 0], raw[r, 1:]
        assert ftype in (0, 2)
        rows[r] = line if ftype == 0 else (line.astype(np.uint16) + prev).astype(np.uint8)
        prev = rows[r]
    rgb = rows.reshape(height, width, 3).astype(np.float64)
    return rgb[..., 0] * 256.0 + rgb[..., 1] + rgb[..., 2] / 256.0 - 32768.0


def write_polar_dem(data_dir: Path) -> None:
    lola = data_dir / "raw" / "lola"
    lola.mkdir(parents=True, exist_ok=True)
    n = int(2 * POLAR_HALF_M / POLAR_RES_M)
    centers = -POLAR_HALF_M + (np.arange(n) + 0.5) * POLAR_RES_M
    x, y = np.meshgrid(centers, centers[::-1])  # row 0 = +Y (north-up raster)
    with rasterio.open(
        lola / "LDEM_80S_80MPP_ADJ.TIF", "w", driver="GTiff", height=n, width=n, count=1,
        dtype="float32", nodata=-9999.0,
        transform=from_origin(-POLAR_HALF_M, POLAR_HALF_M, POLAR_RES_M, POLAR_RES_M),
    ) as dst:
        dst.write(plane_m(x, y).astype(np.float32), 1)


def write_global_dem(data_dir: Path) -> None:
    """SVS-style un-georeferenced float32 DEM in km relative to 1737.4 km (1 deg/px)."""
    gdir = data_dir / "raw" / "lola_global"
    gdir.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        gdir / "ldem_64.tif", "w", driver="GTiff", height=180, width=360, count=1, dtype="float32"
    ) as dst:
        dst.write(np.full((180, 360), GLOBAL_ELEV_M / 1000.0, dtype=np.float32), 1)


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    tiles.clear_tile_cache()
    yield tmp_path
    tiles.clear_tile_cache()


# ---------------------------------------------------------------- tiling scheme


def test_tile_bounds_match_moon_trek_geographic_scheme() -> None:
    assert tiles.tile_bounds(0, 0, 0) == (-180.0, -90.0, 0.0, 90.0)
    assert tiles.tile_bounds(0, 0, 1) == (0.0, -90.0, 180.0, 90.0)
    lon_w, lat_s, lon_e, lat_n = tiles.tile_bounds(*POLE_TILE)
    assert lat_s == -90.0 and lat_n == pytest.approx(-89.296875)
    assert lon_e - lon_w == pytest.approx(180.0 / 256)


@pytest.mark.parametrize("z,row,col", [(-1, 0, 0), (10, 0, 0), (2, 4, 0), (2, 0, 8)])
def test_tile_bounds_reject_out_of_range(z: int, row: int, col: int) -> None:
    with pytest.raises(tiles.TileOutOfRangeError):
        tiles.tile_bounds(z, row, col)


def test_polar_coverage_is_south_of_minus_80() -> None:
    assert tiles.tile_in_polar_coverage(*POLE_TILE)
    assert tiles.tile_in_polar_coverage(*MIXED_TILE)  # -78.75..-90 reaches below -80
    assert not tiles.tile_in_polar_coverage(4, 14, 16)  # -67.5..-78.75
    assert not tiles.tile_in_polar_coverage(2, 1, 0)  # 0..-45


def test_vectorized_projection_matches_engine_projection() -> None:
    lat = np.array([-89.66, -85.99, -80.0])
    lon = np.array([222.7, 2.9, 77.0])
    x, y = tiles._polar_xy(lat, lon)
    for i in range(3):
        ex, ey = polar_stereographic_from_latlon(float(lat[i]), float(lon[i]))
        assert x[i] == pytest.approx(ex, abs=1e-6) and y[i] == pytest.approx(ey, abs=1e-6)


# ---------------------------------------------------------------- Terrarium PNG


def test_terrarium_png_roundtrip_precision() -> None:
    rng = np.random.default_rng(7)
    elev = rng.uniform(-9000.0, 11000.0, size=(256, 256))
    elev[0, 0], elev[0, 1] = -9000.0, 10999.996
    decoded = decode_terrarium_png(tiles.encode_terrarium_png(elev))
    assert decoded.shape == (256, 256)
    assert np.max(np.abs(decoded - elev)) <= 0.5 / 256 + 1e-9


# ---------------------------------------------------------------- sampling


def test_pole_tile_matches_polar_dem(data_dir: Path) -> None:
    write_polar_dem(data_dir)
    elev, source = tiles.tile_elevations(*POLE_TILE)
    assert source == "lola_polar_80mpp"

    lat, lon = tiles._tile_pixel_latlon(*POLE_TILE)
    x, y = tiles._polar_xy(lat, lon)
    # Bilinear interpolation of a plane is exact; only float32 storage rounds.
    assert np.max(np.abs(elev - plane_m(x, y))) < 0.01

    png, _ = tiles.render_terrain_tile(*POLE_TILE)
    assert np.max(np.abs(decode_terrarium_png(png) - plane_m(x, y))) < 0.01


def test_decimated_read_matches_plane(data_dir: Path) -> None:
    write_polar_dem(data_dir)
    ds, tier = tiles._pick_polar_dataset(target_res_m=400.0)
    assert tier == "80MPP"
    x = np.linspace(-20_000.0, 20_000.0, 97)
    y = np.linspace(15_000.0, -15_000.0, 89)
    xx, yy = np.meshgrid(x, y)
    col_f, row_f = (~ds.transform) * (xx, yy)
    got = tiles._sample_decimated(ds, np.asarray(col_f) - 0.5, np.asarray(row_f) - 0.5, 4, 1.0, 0.0)
    # Averaging a plane keeps it a plane; misalignment of partial blocks stays well under 1 m.
    assert np.max(np.abs(got - plane_m(xx, yy))) < 0.5


def test_mixed_tile_fills_outside_polar_raster_with_global_dem(data_dir: Path) -> None:
    write_polar_dem(data_dir)
    write_global_dem(data_dir)
    elev, source = tiles.tile_elevations(*MIXED_TILE)
    assert source == "lola_polar_80mpp+lola_global"
    assert np.all(np.isfinite(elev))
    # North edge of the tile (-78.75 deg) is far outside the polar raster: global value.
    assert np.allclose(elev[0], GLOBAL_ELEV_M, atol=0.01)
    # Last row is about 0.02 deg from the pole: polar plane values (around 500 m).
    assert np.all(np.abs(elev[-1] - 500.0) < 60.0)


def test_global_only_when_no_polar_dem(data_dir: Path) -> None:
    write_global_dem(data_dir)
    elev, source = tiles.tile_elevations(*POLE_TILE)
    assert source == "lola_global"
    assert np.allclose(elev, GLOBAL_ELEV_M, atol=0.01)


def test_finer_polar_tier_is_used_at_high_zoom(data_dir: Path) -> None:
    write_polar_dem(data_dir)
    lola = data_dir / "raw" / "lola"
    n = int(2 * POLAR_HALF_M / 40.0)
    with rasterio.open(
        lola / "LDEM_80S_40MPP_ADJ.TIF", "w", driver="GTiff", height=n, width=n, count=1,
        dtype="float32", transform=from_origin(-POLAR_HALF_M, POLAR_HALF_M, 40.0, 40.0),
    ) as dst:
        dst.write(np.zeros((n, n), dtype=np.float32), 1)
    assert tiles._pick_polar_dataset(tiles._tile_ground_resolution_m(9))[1] == "40MPP"  # ~42 m
    assert tiles._pick_polar_dataset(tiles._tile_ground_resolution_m(8))[1] == "80MPP"  # ~83 m
    assert tiles._pick_polar_dataset(tiles._tile_ground_resolution_m(5))[1] == "80MPP"  # coarse: cheaper read
    assert math.isclose(tiles._tile_ground_resolution_m(8), 83.3, abs_tol=0.1)


# ---------------------------------------------------------------- HTTP endpoint


def _error_code(body: dict) -> str | None:
    error = body.get("error")
    return error.get("code") if isinstance(error, dict) else None


def test_tile_endpoint_returns_terrarium_png(data_dir: Path) -> None:
    write_polar_dem(data_dir)
    with TestClient(app) as client:
        r = client.get("/api/v1/terrain/tiles/8/255/300.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "public, max-age=86400"
    assert r.headers["x-terrain-source"] == "lola_polar_80mpp"
    lat, lon = tiles._tile_pixel_latlon(*POLE_TILE)
    x, y = tiles._polar_xy(lat, lon)
    assert np.max(np.abs(decode_terrarium_png(r.content) - plane_m(x, y))) < 0.01


def test_tile_endpoint_north_of_coverage_is_404(data_dir: Path) -> None:
    write_polar_dem(data_dir)
    with TestClient(app) as client:
        r = client.get("/api/v1/terrain/tiles/2/1/0.png")
    assert r.status_code == 404
    assert _error_code(r.json()) == "ERR_NOT_FOUND"
    assert "cache-control" not in r.headers


@pytest.mark.parametrize("path", ["10/0/0", "2/4/0", "2/0/8"])
def test_tile_endpoint_invalid_index_is_422(data_dir: Path, path: str) -> None:
    with TestClient(app) as client:
        r = client.get(f"/api/v1/terrain/tiles/{path}.png")
    assert r.status_code == 422
    assert _error_code(r.json()) == "ERR_INVALID_PAYLOAD"


def test_tile_endpoint_without_dem_is_503(data_dir: Path) -> None:
    with TestClient(app) as client:
        r = client.get("/api/v1/terrain/tiles/8/255/300.png")
    assert r.status_code == 503
    assert _error_code(r.json()) == "ERR_TERRAIN_UNAVAILABLE"
