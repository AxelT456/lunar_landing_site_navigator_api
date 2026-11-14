"""South pole elevation tiles for the globe (handoff P-008).

Tiles follow the geographic scheme of NASA Moon Trek and Cesium's GeographicTilingScheme:
level 0 has 2 columns x 1 row, each tile spans 180 / 2**z degrees, row 0 is the north edge
and column 0 starts at -180 deg. Every tile is 256 x 256 samples at pixel centers.

Elevations are metres relative to the 1,737,400 m lunar sphere (the same datum as
Ellipsoid.MOON and /terrain/patch), encoded as Terrarium PNG:

    elevation_m = R * 256 + G + B / 256 - 32768      (resolution 1/256 m)

Terrarium is used instead of 16-bit grayscale because browsers read PNGs through an
8-bit canvas, which would truncate 16-bit samples.

Sources, best first: the LOLA south polar DEM whose resolution best matches the tile
(20/40/80 m), then the global LOLA DEM for samples outside the polar raster. Tiles that
lie entirely north of POLAR_COVERAGE_MAX_LAT are not served: the globe keeps its own
global terrain there.
"""

from __future__ import annotations

import math
import struct
import threading
import zlib
from collections import OrderedDict
from typing import Any

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window

from app.engine.coordinates import MOON_RADIUS_M
from app.engine.terrain import (
    _RASTER_LOCK,
    POLAR_TIERS,
    _bilinear_blend,
    _get_data_dir,
    _get_dataset,
    _get_global_dataset,
    _global_latlon_to_uv,
    _scale_and_offset,
)

TILE_SIZE = 256
MAX_TILE_LEVEL = 9  # ~42 m/px along a meridian: matches the 40 m LOLA polar DEM
POLAR_COVERAGE_MAX_LAT = -80.0  # LOLA LDEM_80S_* products cover lat <= -80 deg
TERRARIUM_OFFSET_M = 32768.0
# Above this decimation factor, nearest-neighbour reads touch only the rows GDAL needs;
# averaging would read every block of a very large window (low zoom levels).
_AVERAGE_MAX_FACTOR = 8
_TILE_CACHE_SIZE = 256


class TileOutOfRangeError(ValueError):
    """z/row/col outside the tiling scheme."""


class TileOutsideCoverageError(LookupError):
    """The tile does not reach the south polar region served by this endpoint."""


def tile_bounds(z: int, row: int, col: int) -> tuple[float, float, float, float]:
    """Return (lon_west, lat_south, lon_east, lat_north) in degrees for a tile."""
    if not 0 <= z <= MAX_TILE_LEVEL:
        raise TileOutOfRangeError(f"z must be between 0 and {MAX_TILE_LEVEL}")
    if not 0 <= row < 2**z:
        raise TileOutOfRangeError(f"row must be between 0 and {2**z - 1} at z={z}")
    if not 0 <= col < 2 ** (z + 1):
        raise TileOutOfRangeError(f"col must be between 0 and {2 ** (z + 1) - 1} at z={z}")
    span = 180.0 / 2**z
    lon_w = -180.0 + col * span
    lat_n = 90.0 - row * span
    return lon_w, lat_n - span, lon_w + span, lat_n


def tile_in_polar_coverage(z: int, row: int, col: int) -> bool:
    _, lat_s, _, _ = tile_bounds(z, row, col)
    return lat_s < POLAR_COVERAGE_MAX_LAT


def _tile_pixel_latlon(z: int, row: int, col: int) -> tuple[np.ndarray, np.ndarray]:
    """Latitude/longitude of the 256 x 256 pixel centers (row 0 = north)."""
    lon_w, _, _, lat_n = tile_bounds(z, row, col)
    step = (180.0 / 2**z) / TILE_SIZE
    idx = np.arange(TILE_SIZE, dtype=np.float64) + 0.5
    lon = lon_w + idx * step
    lat = lat_n - idx * step
    lon_grid, lat_grid = np.meshgrid(lon, lat)
    return lat_grid, lon_grid


def _tile_ground_resolution_m(z: int) -> float:
    """Ground size of one tile pixel along a meridian (the coarser axis near the pole)."""
    return math.radians((180.0 / 2**z) / TILE_SIZE) * MOON_RADIUS_M


def _polar_xy(lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized LOLA south polar stereographic projection (same as polar_stereographic_from_latlon)."""
    rho = 2.0 * MOON_RADIUS_M * np.tan(np.radians((90.0 + lat) * 0.5))
    lon_rad = np.radians(lon)
    return rho * np.sin(lon_rad), rho * np.cos(lon_rad)


def _sample_decimated(
    ds: rasterio.DatasetReader,
    u: np.ndarray,
    v: np.ndarray,
    factor: int,
    scale: float,
    offset: float,
) -> np.ndarray:
    """Bilinear samples at 0-based pixel-center coordinates (u, v), reading the window decimated by `factor`.

    Points outside the raster are NaN. With factor 1 this matches the full-resolution sampler.
    """
    out = np.full(u.shape, np.nan, dtype=np.float64)
    valid = (
        np.isfinite(u) & np.isfinite(v)
        & (u >= -0.5) & (u <= ds.width - 0.5)
        & (v >= -0.5) & (v <= ds.height - 0.5)
    )
    if not np.any(valid):
        return out

    uv, vv = u[valid], v[valid]
    c0 = max(0, int(math.floor(uv.min())))
    r0 = max(0, int(math.floor(vv.min())))
    c1 = min(ds.width, int(math.floor(uv.max())) + 2)
    r1 = min(ds.height, int(math.floor(vv.max())) + 2)
    width, height = c1 - c0, r1 - r0

    f = max(1, int(factor))
    out_w = max(1, math.ceil(width / f))
    out_h = max(1, math.ceil(height / f))
    resampling = Resampling.average if f <= _AVERAGE_MAX_FACTOR else Resampling.nearest

    with _RASTER_LOCK:
        if ds.closed:
            return out
        raw = ds.read(
            1,
            window=Window(c0, r0, width, height),
            out_shape=(out_h, out_w),
            resampling=resampling,
        ).astype(np.float64)

    nodata = ds.nodata
    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        raw = np.where(raw == float(nodata), np.nan, raw)
    if scale != 1.0 or offset != 0.0:
        raw = raw * scale + offset

    # Native pixel-center coordinates -> decimated grid pixel-center coordinates.
    sx, sy = width / out_w, height / out_h
    ud = np.clip((uv + 0.5 - c0) / sx - 0.5, 0.0, out_w - 1.0)
    vd = np.clip((vv + 0.5 - r0) / sy - 0.5, 0.0, out_h - 1.0)
    cc0 = np.floor(ud).astype(np.int64)
    rr0 = np.floor(vd).astype(np.int64)
    cc1 = np.minimum(cc0 + 1, out_w - 1)
    rr1 = np.minimum(rr0 + 1, out_h - 1)
    out[valid] = _bilinear_blend(
        raw[rr0, cc0], raw[rr0, cc1], raw[rr1, cc0], raw[rr1, cc1], ud - cc0, vd - rr0
    )
    return out


def _pick_polar_dataset(target_res_m: float) -> tuple[rasterio.DatasetReader | None, str | None]:
    """Coarsest available polar DEM that still resolves the tile pixel; the finest one otherwise."""
    available: list[tuple[float, str, rasterio.DatasetReader]] = []
    for tier, dem_name, _ in POLAR_TIERS:
        ds = _get_dataset(dem_name)
        if ds is not None:
            available.append((abs(float(ds.transform.a)), tier, ds))
    if not available:
        return None, None
    fitting = [item for item in available if item[0] <= target_res_m]
    res, tier, ds = max(fitting) if fitting else min(available)
    return ds, tier


def _sample_polar(lat: np.ndarray, lon: np.ndarray, z: int) -> tuple[np.ndarray, str | None]:
    target = _tile_ground_resolution_m(z)
    ds, tier = _pick_polar_dataset(target)
    if ds is None:
        return np.full(lat.shape, np.nan), None
    x, y = _polar_xy(lat, lon)
    col_f, row_f = (~ds.transform) * (x, y)
    u = np.asarray(col_f, dtype=np.float64) - 0.5
    v = np.asarray(row_f, dtype=np.float64) - 0.5
    factor = int(target // abs(float(ds.transform.a)))
    scale, offset = _scale_and_offset(ds)
    return _sample_decimated(ds, u, v, factor, scale, offset), tier


def _sample_global(lat: np.ndarray, lon: np.ndarray, z: int) -> np.ndarray:
    ds = _get_global_dataset()
    if ds is None:
        return np.full(lat.shape, np.nan)
    u, v, _ = _global_latlon_to_uv(ds, lat.ravel(), lon.ravel())
    native_m = math.radians(180.0 / ds.height) * MOON_RADIUS_M
    factor = int(_tile_ground_resolution_m(z) // native_m)
    scale, offset = _scale_and_offset(ds, is_global=True)
    return _sample_decimated(ds, u, v, factor, scale, offset).reshape(lat.shape)


def tile_elevations(z: int, row: int, col: int) -> tuple[np.ndarray, str]:
    """Elevation grid (256 x 256, metres, row 0 = north) and a source label for one tile.

    Raises TileOutOfRangeError, TileOutsideCoverageError, or FileNotFoundError when no DEM
    covers the tile.
    """
    if not tile_in_polar_coverage(z, row, col):
        raise TileOutsideCoverageError(
            f"Tile {z}/{row}/{col} lies north of {POLAR_COVERAGE_MAX_LAT} deg: no polar terrain tile"
        )
    lat, lon = _tile_pixel_latlon(z, row, col)
    elev, tier = _sample_polar(lat, lon, z)
    used_polar = bool(np.any(np.isfinite(elev)))

    missing = ~np.isfinite(elev)
    used_global = False
    if np.any(missing):
        global_elev = _sample_global(lat, lon, z)
        fill = missing & np.isfinite(global_elev)
        if np.any(fill):
            elev[fill] = global_elev[fill]
            used_global = True

    if not (used_polar or used_global):
        raise FileNotFoundError("No LOLA terrain DEM covers this tile (polar or global)")
    still_missing = ~np.isfinite(elev)
    if np.any(still_missing):
        # Gaps only when the global DEM is absent: keep the tile continuous instead of holes.
        elev[still_missing] = float(np.nanmean(elev))

    if used_polar and used_global:
        source = f"lola_polar_{tier.lower()}+lola_global"
    elif used_polar:
        source = f"lola_polar_{tier.lower()}"
    else:
        source = "lola_global"
    return elev, source


def encode_terrarium_png(elevations_m: np.ndarray) -> bytes:
    """Encode a 2D elevation grid (metres) as an 8-bit RGB Terrarium PNG."""
    elev = np.asarray(elevations_m, dtype=np.float64)
    if elev.ndim != 2:
        raise ValueError("elevations must be a 2D array")
    val = np.clip(elev + TERRARIUM_OFFSET_M, 0.0, 65535.0 + 255.0 / 256.0)
    scaled = np.round(val * 256.0).astype(np.int64)  # 1/256 m steps
    rgb = np.empty(elev.shape + (3,), dtype=np.uint8)
    rgb[..., 0] = (scaled >> 16) & 0xFF
    rgb[..., 1] = (scaled >> 8) & 0xFF
    rgb[..., 2] = scaled & 0xFF

    height, width = elev.shape
    rows = rgb.reshape(height, width * 3)
    # PNG "Up" filter (type 2): terrain rows are similar, so the deltas compress well.
    prev = np.vstack([np.zeros((1, width * 3), dtype=np.uint8), rows[:-1]])
    filtered = (rows.astype(np.int16) - prev.astype(np.int16)).astype(np.uint8)
    raw = np.hstack([np.full((height, 1), 2, dtype=np.uint8), filtered]).tobytes()

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB, no interlace
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


_tile_cache: OrderedDict[tuple[Any, ...], tuple[bytes, str]] = OrderedDict()
_tile_cache_lock = threading.Lock()


def render_terrain_tile(z: int, row: int, col: int) -> tuple[bytes, str]:
    """Terrarium PNG bytes and source label for a tile, with a small in-process LRU cache."""
    key = (str(_get_data_dir()), z, row, col)
    with _tile_cache_lock:
        hit = _tile_cache.get(key)
        if hit is not None:
            _tile_cache.move_to_end(key)
            return hit
    elev, source = tile_elevations(z, row, col)
    result = (encode_terrarium_png(elev), source)
    with _tile_cache_lock:
        _tile_cache[key] = result
        _tile_cache.move_to_end(key)
        while len(_tile_cache) > _TILE_CACHE_SIZE:
            _tile_cache.popitem(last=False)
    return result


def clear_tile_cache() -> None:
    with _tile_cache_lock:
        _tile_cache.clear()
