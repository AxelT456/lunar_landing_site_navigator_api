"""Lunar elevation tiles for the globe (handoff P-008).

Tiles follow the geographic scheme of NASA Moon Trek and Cesium's GeographicTilingScheme:
level 0 has 2 columns x 1 row, each tile spans 180 / 2**z degrees, row 0 is the north edge
and column 0 starts at -180 deg. Every tile is 256 x 256 samples at pixel centers and
covers the whole Moon, so the globe can use a single terrain source (no seam where two
sources meet).

Elevations are metres relative to the 1,737,400 m lunar sphere (the same datum as
Ellipsoid.MOON and /terrain/patch), encoded as Terrarium PNG:

    elevation_m = R * 256 + G + B / 256 - 32768      (resolution 1/256 m)

Terrarium is used instead of 16-bit grayscale because browsers read PNGs through an
8-bit canvas, which would truncate 16-bit samples.

Sources, best first: the LOLA south polar DEM whose resolution best matches the tile
(20/40/80 m) where it has data, then the global LOLA DEM (ldem_64, ~474 m/px).

Seamless decimation: at low zoom the DEM is read decimated by a power-of-two factor over
whole blocks aligned to the raster origin. Every tile of a zoom level therefore samples
the same decimated grid, so neighbouring tiles agree on their shared edge. The global DEM
wraps around at +/-180 deg.
"""

from __future__ import annotations

import hashlib
import math
import struct
import threading
import zlib
from collections import OrderedDict
from pathlib import Path
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
    _find_global_dem_file,
    _find_lola_file,
    _get_dataset,
    _get_global_dataset,
    _global_latlon_to_uv,
    _scale_and_offset,
)

TILE_SIZE = 256
MAX_TILE_LEVEL = 9  # ~42 m/px along a meridian: matches the 40 m LOLA polar DEM
TERRARIUM_OFFSET_M = 32768.0
# Above this decimation factor, nearest-neighbour reads touch only the rows GDAL needs;
# averaging would read every block of a very large window (low zoom levels).
_AVERAGE_MAX_FACTOR = 8
_TILE_CACHE_SIZE = 256


class TileOutOfRangeError(ValueError):
    """z/row/col outside the tiling scheme."""


class TileUnavailableError(FileNotFoundError):
    """The DEMs on the server do not cover the whole tile."""


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


def _decimation_factor(target_m: float, native_m: float) -> int:
    """Largest power of two not above target/native (1 = full resolution)."""
    ratio = target_m / native_m
    return 2 ** int(math.floor(math.log2(ratio))) if ratio >= 2.0 else 1


def _polar_xy(lat: np.ndarray, lon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Vectorized LOLA south polar stereographic projection (same as polar_stereographic_from_latlon)."""
    rho = 2.0 * MOON_RADIUS_M * np.tan(np.radians((90.0 + lat) * 0.5))
    lon_rad = np.radians(lon)
    return rho * np.sin(lon_rad), rho * np.cos(lon_rad)


def _contiguous_runs(values: np.ndarray) -> list[tuple[int, int]]:
    """Split sorted unique integers into inclusive (start, end) runs."""
    breaks = np.nonzero(np.diff(values) > 1)[0]
    starts = np.concatenate(([values[0]], values[breaks + 1]))
    ends = np.concatenate((values[breaks], [values[-1]]))
    return [(int(s), int(e)) for s, e in zip(starts, ends)]


def _sample_decimated(
    ds: rasterio.DatasetReader,
    u: np.ndarray,
    v: np.ndarray,
    factor: int,
    scale: float,
    offset: float,
    wrap_x: bool = False,
) -> np.ndarray:
    """Bilinear samples at 0-based native pixel-center coordinates (u, v) on a decimated grid.

    The decimated grid is made of whole factor x factor blocks counted from the raster
    origin, so it is the same for every caller. Points outside the raster are NaN; with
    wrap_x, columns wrap around (global DEM at +/-180 deg). factor 1 is full resolution.
    """
    out = np.full(u.shape, np.nan, dtype=np.float64)
    f = max(1, int(factor))
    nbx, nby = ds.width // f, ds.height // f
    while f > 1 and (nbx < 2 or nby < 2):
        f //= 2
        nbx, nby = ds.width // f, ds.height // f

    valid = np.isfinite(u) & np.isfinite(v) & (v >= -0.5) & (v <= ds.height - 0.5)
    if not wrap_x:
        valid &= (u >= -0.5) & (u <= ds.width - 0.5)
    if not np.any(valid):
        return out

    # Native pixel centers -> decimated block centers.
    ud = (u[valid] + 0.5) / f - 0.5
    vd = np.clip((v[valid] + 0.5) / f - 0.5, 0.0, nby - 1.0)
    if wrap_x:
        ud = np.mod(ud, float(nbx))
        cc0 = np.floor(ud).astype(np.int64) % nbx
        cc1 = (cc0 + 1) % nbx
        wc = ud - np.floor(ud)
    else:
        ud = np.clip(ud, 0.0, nbx - 1.0)
        cc0 = np.floor(ud).astype(np.int64)
        cc1 = np.minimum(cc0 + 1, nbx - 1)
        wc = ud - cc0
    rr0 = np.floor(vd).astype(np.int64)
    rr1 = np.minimum(rr0 + 1, nby - 1)
    wr = vd - rr0

    r_lo, r_hi = int(rr0.min()), int(rr1.max())
    cols = np.unique(np.concatenate((cc0, cc1)))
    runs = _contiguous_runs(cols)
    if not wrap_x and len(runs) > 1:
        runs = [(runs[0][0], runs[-1][1])]

    resampling = Resampling.average if f <= _AVERAGE_MAX_FACTOR else Resampling.nearest
    pieces = []
    col_index = np.full(nbx, -1, dtype=np.int64)
    pos = 0
    with _RASTER_LOCK:
        if ds.closed:
            return out
        for c_lo, c_hi in runs:
            n_cols, n_rows = c_hi - c_lo + 1, r_hi - r_lo + 1
            pieces.append(
                ds.read(
                    1,
                    window=Window(c_lo * f, r_lo * f, n_cols * f, n_rows * f),
                    out_shape=(n_rows, n_cols),
                    resampling=resampling,
                ).astype(np.float64)
            )
            col_index[c_lo : c_hi + 1] = np.arange(pos, pos + n_cols)
            pos += n_cols
    raw = np.hstack(pieces)

    nodata = ds.nodata
    if nodata is not None and not (isinstance(nodata, float) and math.isnan(nodata)):
        raw = np.where(raw == float(nodata), np.nan, raw)
    if scale != 1.0 or offset != 0.0:
        raw = raw * scale + offset

    a0, a1 = col_index[cc0], col_index[cc1]
    b0, b1 = rr0 - r_lo, rr1 - r_lo
    out[valid] = _bilinear_blend(raw[b0, a0], raw[b0, a1], raw[b1, a0], raw[b1, a1], wc, wr)
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
    _, tier, ds = max(fitting, key=lambda i: i[0]) if fitting else min(available, key=lambda i: i[0])
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
    factor = _decimation_factor(target, abs(float(ds.transform.a)))
    scale, offset = _scale_and_offset(ds)
    return _sample_decimated(ds, u, v, factor, scale, offset), tier


def _sample_global(lat: np.ndarray, lon: np.ndarray, z: int) -> np.ndarray:
    """Global DEM samples for 1D lat/lon arrays (only the pixels the polar DEM left empty)."""
    ds = _get_global_dataset()
    if ds is None or lat.size == 0:
        return np.full(lat.shape, np.nan)
    u, v, wrap_x = _global_latlon_to_uv(ds, lat, lon)
    native_m = math.radians(180.0 / ds.height) * MOON_RADIUS_M
    factor = _decimation_factor(_tile_ground_resolution_m(z), native_m)
    scale, offset = _scale_and_offset(ds, is_global=True)
    return _sample_decimated(ds, u, v, factor, scale, offset, wrap_x=wrap_x)


def tile_elevations(z: int, row: int, col: int) -> tuple[np.ndarray, str]:
    """Elevation grid (256 x 256, metres, row 0 = north) and a source label for one tile.

    Raises TileOutOfRangeError, or TileUnavailableError (a FileNotFoundError) when the
    DEMs on the server do not cover every pixel of the tile.
    """
    lat, lon = _tile_pixel_latlon(z, row, col)
    elev, tier = _sample_polar(lat, lon, z)
    used_polar = bool(np.any(np.isfinite(elev)))

    missing = ~np.isfinite(elev)
    used_global = False
    if np.any(missing):
        global_elev = _sample_global(lat[missing], lon[missing], z)
        elev[missing] = global_elev
        used_global = bool(np.any(np.isfinite(global_elev)))

    if not np.all(np.isfinite(elev)):
        raise TileUnavailableError(
            f"The LOLA DEMs on the server do not cover tile {z}/{row}/{col} (global DEM missing?)"
        )

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


def dem_signature() -> tuple[tuple[str, int, int], ...]:
    """(path, mtime_ns, size) of every DEM a tile may read: changes when a raster is replaced."""
    paths: list[Path | None] = [_find_lola_file(dem_name) for _, dem_name, _ in POLAR_TIERS]
    paths.append(_find_global_dem_file())
    sig = []
    for path in paths:
        if path is None:
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        sig.append((str(path), st.st_mtime_ns, st.st_size))
    return tuple(sig)


def tile_etag(z: int, row: int, col: int, signature: tuple[Any, ...] | None = None) -> str:
    """Strong ETag for a tile: depends on the tile index and on the DEM files on disk."""
    sig = dem_signature() if signature is None else signature
    digest = hashlib.sha1(repr((z, row, col, sig)).encode()).hexdigest()[:20]
    return f'"{digest}"'


_tile_cache: OrderedDict[tuple[Any, ...], tuple[bytes, str]] = OrderedDict()
_tile_cache_lock = threading.Lock()


def render_terrain_tile(z: int, row: int, col: int) -> tuple[bytes, str, str]:
    """Terrarium PNG bytes, source label and ETag for a tile, with an in-process LRU cache.

    The cache key includes the DEM signature, so replacing a raster invalidates it.
    """
    tile_bounds(z, row, col)  # validate before touching the disk
    sig = dem_signature()
    etag = tile_etag(z, row, col, sig)
    key = (z, row, col, sig)
    with _tile_cache_lock:
        hit = _tile_cache.get(key)
        if hit is not None:
            _tile_cache.move_to_end(key)
            return hit[0], hit[1], etag
    elev, source = tile_elevations(z, row, col)
    png = encode_terrarium_png(elev)
    with _tile_cache_lock:
        _tile_cache[key] = (png, source)
        _tile_cache.move_to_end(key)
        while len(_tile_cache) > _TILE_CACHE_SIZE:
            _tile_cache.popitem(last=False)
    return png, source, etag


def clear_tile_cache() -> None:
    with _tile_cache_lock:
        _tile_cache.clear()
