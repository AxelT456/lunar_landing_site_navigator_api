"""Lunar terrain tiles (handoff P-008): tiling scheme, Terrarium PNG, seams and the HTTP endpoint.

Uses small synthetic rasters in a temporary DATA_DIR: LOLA-style polar DEMs (a known plane,
or a rough surface for seam checks) and an SVS-style global DEM. No NASA datasets needed.
"""

from __future__ import annotations

import os
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
POLAR_HALF_M = 24_000.0  # plane polar raster covers |x|, |y| <= 24 km (lat < about -89.2)
GLOBAL_ELEV_M = 1200.0

# z=8 tile touching the pole: lat -89.297..-90, lon 30.94..31.64, fully inside the plane raster.
POLE_TILE = (8, 255, 300)
# z=4 tile from -78.75 to -90: exceeds the plane raster, so it needs the global DEM.
MIXED_TILE = (4, 15, 16)
# z=2 tile from 0 to -45 deg: global DEM only.
NORTH_TILE = (2, 1, 0)


def plane_m(x: np.ndarray | float, y: np.ndarray | float) -> np.ndarray | float:
    return 500.0 + 0.002 * x - 0.001 * y


def rough_polar_m(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    return 1000.0 + 800.0 * np.sin(x / 7000.0) * np.cos(y / 9000.0) + 300.0 * np.sin((x + y) / 2300.0)


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


def write_polar_dem(data_dir: Path, res_m: float = POLAR_RES_M, half_m: float = POLAR_HALF_M,
                    surface=plane_m, name: str = "LDEM_80S_80MPP_ADJ.TIF") -> Path:
    lola = data_dir / "raw" / "lola"
    lola.mkdir(parents=True, exist_ok=True)
    n = int(round(2 * half_m / res_m))
    centers = -half_m + (np.arange(n) + 0.5) * res_m
    x, y = np.meshgrid(centers, centers[::-1])  # row 0 = +Y (north-up raster)
    path = lola / name
    with rasterio.open(
        path, "w", driver="GTiff", height=n, width=n, count=1, dtype="float32", nodata=-9999.0,
        transform=from_origin(-half_m, half_m, res_m, res_m),
    ) as dst:
        dst.write(surface(x, y).astype(np.float32), 1)
    return path


def write_global_dem(data_dir: Path, rough: bool = False, width: int = 360) -> None:
    """SVS-style un-georeferenced float32 DEM in km relative to 1737.4 km."""
    gdir = data_dir / "raw" / "lola_global"
    gdir.mkdir(parents=True, exist_ok=True)
    height = width // 2
    if rough:
        lon = np.radians((np.arange(width) + 0.5) / width * 360.0 - 180.0)
        lat = np.radians(90.0 - (np.arange(height) + 0.5) / height * 180.0)
        lon_g, lat_g = np.meshgrid(lon, lat)
        km = 1.0 + 0.9 * np.sin(7 * lon_g) * np.cos(5 * lat_g) + 0.4 * np.sin(23 * lon_g + 11 * lat_g)
    else:
        km = np.full((height, width), GLOBAL_ELEV_M / 1000.0)
    with rasterio.open(
        gdir / "ldem_64.tif", "w", driver="GTiff", height=height, width=width, count=1, dtype="float32"
    ) as dst:
        dst.write(km.astype(np.float32), 1)


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


def test_vectorized_projection_matches_engine_projection() -> None:
    lat = np.array([-89.66, -85.99, -80.0])
    lon = np.array([222.7, 2.9, 77.0])
    x, y = tiles._polar_xy(lat, lon)
    for i in range(3):
        ex, ey = polar_stereographic_from_latlon(float(lat[i]), float(lon[i]))
        assert x[i] == pytest.approx(ex, abs=1e-6) and y[i] == pytest.approx(ey, abs=1e-6)


def test_decimation_factor_is_power_of_two() -> None:
    assert tiles._decimation_factor(83.3, 80.0) == 1
    assert tiles._decimation_factor(166.6, 80.0) == 2
    assert tiles._decimation_factor(1333.0, 80.0) == 16
    assert tiles._decimation_factor(2667.0, 640.0) == 4


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

    png, _, _ = tiles.render_terrain_tile(*POLE_TILE)
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
    # Block averages of a plane lie on the plane, and the blocks are aligned: exact up to float32.
    assert np.max(np.abs(got - plane_m(xx, yy))) < 0.01


@pytest.mark.parametrize("average_max_factor", [8, 1], ids=["average", "nearest"])
def test_point_value_does_not_depend_on_the_batch(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch, average_max_factor: int
) -> None:
    """Seam invariant: a point gets the same value whatever other points share the read.

    Neighbouring tiles read different windows; if the decimated grid followed the window
    they would disagree on their shared edge (review of PR #7, point 1).
    """
    monkeypatch.setattr(tiles, "_AVERAGE_MAX_FACTOR", average_max_factor)
    write_polar_dem(data_dir, res_m=100.0, half_m=40_000.0, surface=rough_polar_m)
    ds, _ = tiles._pick_polar_dataset(1000.0)
    rng = np.random.default_rng(3)
    probe = rng.uniform(-30_000.0, 30_000.0, size=(2, 50))
    far = rng.uniform(-39_000.0, 39_000.0, size=(2, 400))

    def sample(xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        col_f, row_f = (~ds.transform) * (xs, ys)
        return tiles._sample_decimated(ds, np.asarray(col_f) - 0.5, np.asarray(row_f) - 0.5, 8, 1.0, 0.0)

    alone = sample(probe[0], probe[1])
    with_others = sample(np.concatenate([probe[0], far[0]]), np.concatenate([probe[1], far[1]]))[:50]
    assert np.all(np.isfinite(alone))
    assert np.array_equal(alone, with_others)


def test_global_dem_wraps_at_antimeridian(data_dir: Path) -> None:
    write_global_dem(data_dir, rough=True, width=720)  # 0.5 deg/px, first/last columns at -/+179.75
    lat = np.full(4, 10.0)
    west, east, first_col, last_col = tiles._sample_global(lat, np.array([-179.99, 179.99, -179.75, 179.75]), z=2)
    assert abs(first_col - last_col) > 1.0  # the two edge columns really differ
    # 0.02 deg apart across +/-180: both blend the last and first columns almost equally.
    # Without wrapping they would be clamped to different columns (difference = full column step).
    assert abs(west - east) < 0.1 * abs(first_col - last_col)


@pytest.mark.parametrize(
    "z,row,col_a,col_b",
    [(3, 7, 4, 5), (3, 7, 15, 0), (2, 3, 3, 4), (2, 1, 7, 0), (5, 31, 40, 41)],
    ids=["z3", "z3-antimeridian-polar", "z2", "z2-antimeridian-global", "z5"],
)
def test_adjacent_tiles_have_no_seam(data_dir: Path, z: int, row: int, col_a: int, col_b: int) -> None:
    write_polar_dem(data_dir, res_m=640.0, half_m=352_000.0, surface=rough_polar_m)
    write_global_dem(data_dir, rough=True, width=1440)
    a, _ = tiles.tile_elevations(z, row, col_a)
    b, _ = tiles.tile_elevations(z, row, col_b)
    edge = np.mean(np.abs(a[:, -1] - b[:, 0]))
    inner = np.mean(np.abs(np.diff(a, axis=1)))
    # A seam shows up as an edge jump several times larger than neighbouring pixels.
    assert edge < 1.5 * inner


def test_mixed_tile_fills_outside_polar_raster_with_global_dem(data_dir: Path) -> None:
    write_polar_dem(data_dir)
    write_global_dem(data_dir)
    elev, source = tiles.tile_elevations(*MIXED_TILE)
    assert source == "lola_polar_80mpp+lola_global"
    assert np.all(np.isfinite(elev))
    assert np.allclose(elev[0], GLOBAL_ELEV_M, atol=0.01)  # -78.75 deg: global
    assert np.all(np.abs(elev[-1] - 500.0) < 60.0)  # ~0.02 deg from the pole: polar plane


def test_tile_north_of_polar_region_comes_from_global_dem(data_dir: Path) -> None:
    write_polar_dem(data_dir)
    write_global_dem(data_dir)
    elev, source = tiles.tile_elevations(*NORTH_TILE)
    assert source == "lola_global"
    assert np.allclose(elev, GLOBAL_ELEV_M, atol=0.01)


def test_gap_without_global_dem_is_unavailable(data_dir: Path) -> None:
    write_polar_dem(data_dir)
    with pytest.raises(tiles.TileUnavailableError):
        tiles.tile_elevations(*MIXED_TILE)


def test_finer_polar_tier_is_used_at_high_zoom(data_dir: Path) -> None:
    write_polar_dem(data_dir)
    write_polar_dem(data_dir, res_m=40.0, surface=lambda x, y: np.zeros_like(x), name="LDEM_80S_40MPP_ADJ.TIF")
    assert tiles._pick_polar_dataset(tiles._tile_ground_resolution_m(9))[1] == "40MPP"  # ~42 m
    assert tiles._pick_polar_dataset(tiles._tile_ground_resolution_m(8))[1] == "80MPP"  # ~83 m
    assert tiles._pick_polar_dataset(tiles._tile_ground_resolution_m(5))[1] == "80MPP"  # coarse: cheaper read
    assert tiles._tile_ground_resolution_m(8) == pytest.approx(83.3, abs=0.1)


def test_replacing_a_dem_invalidates_cache_and_etag(data_dir: Path) -> None:
    path = write_polar_dem(data_dir)
    png1, _, etag1 = tiles.render_terrain_tile(*POLE_TILE)
    write_polar_dem(data_dir, surface=lambda x, y: plane_m(x, y) + 100.0)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # make sure mtime moves
    png2, _, etag2 = tiles.render_terrain_tile(*POLE_TILE)
    assert etag1 != etag2
    assert np.allclose(decode_terrarium_png(png2) - decode_terrarium_png(png1), 100.0, atol=0.01)


# ---------------------------------------------------------------- HTTP endpoint


def _error_code(body: dict) -> str | None:
    error = body.get("error")
    return error.get("code") if isinstance(error, dict) else None


def test_tile_endpoint_returns_terrarium_png(data_dir: Path) -> None:
    write_polar_dem(data_dir)
    with TestClient(app) as client:
        r = client.get("/api/v1/terrain/tiles/8/255/300.png", headers={"Origin": "http://localhost:5174"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["cache-control"] == "public, max-age=3600"
    assert r.headers["x-terrain-source"] == "lola_polar_80mpp"
    assert r.headers["etag"].startswith('"')
    assert "x-terrain-source" in r.headers["access-control-expose-headers"].lower()
    lat, lon = tiles._tile_pixel_latlon(*POLE_TILE)
    x, y = tiles._polar_xy(lat, lon)
    assert np.max(np.abs(decode_terrarium_png(r.content) - plane_m(x, y))) < 0.01


def test_tile_endpoint_revalidates_with_etag(data_dir: Path) -> None:
    write_polar_dem(data_dir)
    with TestClient(app) as client:
        first = client.get("/api/v1/terrain/tiles/8/255/300.png")
        again = client.get("/api/v1/terrain/tiles/8/255/300.png", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == first.headers["etag"]


def test_tile_endpoint_serves_tiles_north_of_the_pole(data_dir: Path) -> None:
    write_global_dem(data_dir)
    with TestClient(app) as client:
        r = client.get("/api/v1/terrain/tiles/2/1/0.png")
    assert r.status_code == 200
    assert r.headers["x-terrain-source"] == "lola_global"
    assert np.allclose(decode_terrarium_png(r.content), GLOBAL_ELEV_M, atol=0.01)


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
    assert "cache-control" not in r.headers
