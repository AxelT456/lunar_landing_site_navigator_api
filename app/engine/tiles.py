"""Lunar terrain tiles for the globe (handoff P-008, D-016, D-019).

Tiles follow the geographic scheme of NASA Moon Trek and Cesium's GeographicTilingScheme:
level 0 has 2 columns x 1 row, each tile spans 180 / 2**z degrees, row 0 is the north edge
and column 0 starts at -180 deg. Every tile covers the whole Moon, so the globe can use a
single terrain source (no seam where two sources meet). Three products share the scheme:

* Elevation (Terrarium PNG, 256 x 256 samples at pixel centers, z 0..10):

      elevation_m = R * 256 + G + B / 256 - 32768      (resolution 1/256 m)

  Terrarium is used instead of 16-bit grayscale because browsers read PNGs through an
  8-bit canvas, which would truncate 16-bit samples.
* Heightmap (65 x 65 little-endian float32, z 0..12): samples ON the tile edges and
  corners (vertex grid), the layout Cesium's heightmap terrain expects, so neighbouring
  tiles share bit-identical edge values. Values are quantized to 1/256 m like Terrarium.
* Hillshade (8-bit gray PNG, 256 x 256 at pixel centers, z 0..10): Lambert cosine of the
  angle between a distant light and the terrain normal, without cast shadows. Slopes are
  central differences on the DEM's own grid (polar stereographic for the polar DEM, with
  its scale factor; latitude/longitude for the global DEM), and every pixel reads its
  neighbours from the raster, not from the tile, so tile edges show no seams.

Elevations are metres relative to the 1,737,400 m lunar sphere (the same datum as
Ellipsoid.MOON and /terrain/patch). Sources, best first: the LOLA south polar DEM whose
resolution best matches the sample spacing (20/40/80 m) where it has data, then the finest
global LOLA DEM on disk (ldem_256, ~118 m/px, or ldem_64, ~474 m/px).

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
from dataclasses import dataclass
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
MAX_TILE_LEVEL = 10  # ~21 m/px along a meridian: matches the 20 m LOLA polar DEM
HEIGHTMAP_SIZE = 65
MAX_HEIGHTMAP_LEVEL = 12  # 180 / 2**12 / 64 deg ~ 21 m between samples
TERRARIUM_OFFSET_M = 32768.0
# Default hillshade light: overhead at lat -22 deg, lon 135 deg E (the globe's fixed light).
DEFAULT_LIGHT_LON_DEG = 135.0
DEFAULT_LIGHT_LAT_DEG = -22.0
# Above this decimation factor, nearest-neighbour reads touch only the rows GDAL needs;
# averaging would read every block of a very large window (low zoom levels).
_AVERAGE_MAX_FACTOR = 8
_TILE_CACHE_MAX_BYTES = 64 * 1024 * 1024


class TileOutOfRangeError(ValueError):
    """z/row/col outside the tiling scheme."""


class TileUnavailableError(FileNotFoundError):
    """The DEMs on the server do not cover the whole tile."""


def tile_bounds(z: int, row: int, col: int, max_level: int = MAX_TILE_LEVEL) -> tuple[float, float, float, float]:
    """Return (lon_west, lat_south, lon_east, lat_north) in degrees for a tile."""
    if not 0 <= z <= max_level:
        raise TileOutOfRangeError(f"z must be between 0 and {max_level}")
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


def _tile_vertex_latlon(z: int, row: int, col: int, size: int = HEIGHTMAP_SIZE) -> tuple[np.ndarray, np.ndarray]:
    """Latitude/longitude of a size x size vertex grid that includes the tile edges (row 0 = north).

    Positions are computed as (index + fraction) * span from the scheme origin, so the last
    column of a tile and the first column of its eastern neighbour are the same floats.
    """
    tile_bounds(z, row, col, MAX_HEIGHTMAP_LEVEL)
    span = 180.0 / 2**z
    frac = np.arange(size, dtype=np.float64) / (size - 1)
    lon = -180.0 + (col + frac) * span
    lon = np.where(lon >= 180.0, lon - 360.0, lon)  # +180 is -180: the same floats on both sides of the antimeridian
    lat = 90.0 - (row + frac) * span
    lon_grid, lat_grid = np.meshgrid(lon, lat)
    return lat_grid, lon_grid


def _tile_ground_resolution_m(z: int) -> float:
    """Ground size of one tile pixel along a meridian (the coarser axis near the pole)."""
    return math.radians((180.0 / 2**z) / TILE_SIZE) * MOON_RADIUS_M


def _vertex_spacing_m(z: int, size: int = HEIGHTMAP_SIZE) -> float:
    """Ground distance between heightmap samples along a meridian."""
    return math.radians((180.0 / 2**z) / (size - 1)) * MOON_RADIUS_M


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




def _effective_resolution_m(native_m: float, target_m: float) -> float:
    """Spacing of the data actually read: the native pixel times the decimation factor."""
    return native_m * _decimation_factor(target_m, native_m)


def _global_native_m(ds: rasterio.DatasetReader) -> float:
    return math.radians(180.0 / ds.height) * MOON_RADIUS_M


def _sample_polar_at(lat: np.ndarray, lon: np.ndarray, target_m: float) -> tuple[np.ndarray, str | None, float | None]:
    """Polar DEM samples for the spacing target_m: (elevations, tier, effective resolution)."""
    ds, tier = _pick_polar_dataset(target_m)
    if ds is None:
        return np.full(lat.shape, np.nan), None, None
    native = abs(float(ds.transform.a))
    x, y = _polar_xy(lat, lon)
    col_f, row_f = (~ds.transform) * (x, y)
    u = np.asarray(col_f, dtype=np.float64) - 0.5
    v = np.asarray(row_f, dtype=np.float64) - 0.5
    factor = _decimation_factor(target_m, native)
    scale, offset = _scale_and_offset(ds)
    return _sample_decimated(ds, u, v, factor, scale, offset), tier, native * factor


def _sample_polar(lat: np.ndarray, lon: np.ndarray, z: int) -> tuple[np.ndarray, str | None]:
    elev, tier, _ = _sample_polar_at(lat, lon, _tile_ground_resolution_m(z))
    return elev, tier


def _sample_global_at(lat: np.ndarray, lon: np.ndarray, target_m: float) -> tuple[np.ndarray, float | None]:
    """Global DEM samples for 1D lat/lon arrays: (elevations, effective resolution)."""
    ds = _get_global_dataset()
    if ds is None or lat.size == 0:
        return np.full(lat.shape, np.nan), None
    u, v, wrap_x = _global_latlon_to_uv(ds, lat, lon)
    native_m = _global_native_m(ds)
    factor = _decimation_factor(target_m, native_m)
    scale, offset = _scale_and_offset(ds, is_global=True)
    return _sample_decimated(ds, u, v, factor, scale, offset, wrap_x=wrap_x), native_m * factor


def _sample_global(lat: np.ndarray, lon: np.ndarray, z: int) -> np.ndarray:
    """Global DEM samples for 1D lat/lon arrays (only the pixels the polar DEM left empty)."""
    return _sample_global_at(lat, lon, _tile_ground_resolution_m(z))[0]


def _source_label(tier: str | None, used_polar: bool, used_global: bool) -> str:
    if used_polar and used_global:
        return f"lola_polar_{tier.lower()}+lola_global"
    if used_polar:
        return f"lola_polar_{tier.lower()}"
    return "lola_global"


def _elevations(lat: np.ndarray, lon: np.ndarray, target_m: float, label: str) -> tuple[np.ndarray, str, float]:
    """Elevations (metres) at lat/lon for a sample spacing, a source label and the coarsest
    effective resolution among the DEMs used. Raises TileUnavailableError on any gap."""
    elev, tier, polar_res = _sample_polar_at(lat, lon, target_m)
    used_polar = bool(np.any(np.isfinite(elev)))

    missing = ~np.isfinite(elev)
    used_global, global_res = False, None
    if np.any(missing):
        global_elev, global_res = _sample_global_at(lat[missing], lon[missing], target_m)
        elev[missing] = global_elev
        used_global = bool(np.any(np.isfinite(global_elev)))

    if not np.all(np.isfinite(elev)):
        raise TileUnavailableError(f"The LOLA DEMs on the server do not cover tile {label} (global DEM missing?)")

    resolutions = [r for r, used in ((polar_res, used_polar), (global_res, used_global)) if used and r]
    return elev, _source_label(tier, used_polar, used_global), max(resolutions)


def tile_elevations(z: int, row: int, col: int) -> tuple[np.ndarray, str]:
    """Elevation grid (256 x 256, metres, row 0 = north) and a source label for one tile.

    Raises TileOutOfRangeError, or TileUnavailableError (a FileNotFoundError) when the
    DEMs on the server do not cover every pixel of the tile.
    """
    lat, lon = _tile_pixel_latlon(z, row, col)
    elev, source, _ = _elevations(lat, lon, _tile_ground_resolution_m(z), f"{z}/{row}/{col}")
    return elev, source


def heightmap_elevations(z: int, row: int, col: int) -> tuple[np.ndarray, str, float]:
    """65 x 65 vertex-grid elevations (metres, row 0 = north edge), source label and resolution."""
    lat, lon = _tile_vertex_latlon(z, row, col)
    return _elevations(lat, lon, _vertex_spacing_m(z), f"heightmap {z}/{row}/{col}")


# ---------------------------------------------------------------- hillshade


def _polar_slopes(lat: np.ndarray, lon: np.ndarray, target_m: float) -> tuple[np.ndarray, np.ndarray, str | None, float | None]:
    """East/north ground slopes (dz/ds) from the polar DEM, NaN where it lacks data.

    Central differences one decimated cell away along the stereographic X and Y axes. The
    projection is conformal with scale factor k = sec^2((90 + lat) / 2) (map metres per
    ground metre), so the ground gradient is k times the map gradient, rotated into the
    local East/North axes. Horizontal distances grow with height by (R + h) / R.
    """
    ds, tier = _pick_polar_dataset(target_m)
    if ds is None:
        nan = np.full(lat.shape, np.nan)
        return nan, nan.copy(), None, None
    native = abs(float(ds.transform.a))
    factor = _decimation_factor(target_m, native)
    step = native * factor
    x, y = _polar_xy(lat, lon)
    xs = np.concatenate([(x + step).ravel(), (x - step).ravel(), x.ravel(), x.ravel()])
    ys = np.concatenate([y.ravel(), y.ravel(), (y + step).ravel(), (y - step).ravel()])
    col_f, row_f = (~ds.transform) * (xs, ys)
    scale, offset = _scale_and_offset(ds)
    vals = _sample_decimated(
        ds, np.asarray(col_f, dtype=np.float64) - 0.5, np.asarray(row_f, dtype=np.float64) - 0.5, factor, scale, offset
    ).reshape((4,) + lat.shape)
    gx = (vals[0] - vals[1]) / (2.0 * step)
    gy = (vals[2] - vals[3]) / (2.0 * step)
    k = 1.0 / np.cos(np.radians((90.0 + lat) * 0.5)) ** 2
    lam = np.radians(lon)
    # East = (cos lon, -sin lon) and North = (sin lon, cos lon) in stereographic X/Y.
    corr = k * MOON_RADIUS_M / (MOON_RADIUS_M + vals.mean(axis=0))
    d_east = corr * (gx * np.cos(lam) - gy * np.sin(lam))
    d_north = corr * (gx * np.sin(lam) + gy * np.cos(lam))
    return d_east, d_north, tier, step


def _global_slopes(lat: np.ndarray, lon: np.ndarray, target_m: float) -> tuple[np.ndarray, np.ndarray, float | None]:
    """East/north ground slopes from the global DEM for 1D lat/lon arrays.

    Central differences one decimated cell away in latitude; in longitude the step widens
    by 1 / cos(lat) so both span the same ground distance (capped at 180 deg near the poles,
    where the east difference then vanishes and only the north slope remains).
    """
    ds = _get_global_dataset()
    if ds is None or lat.size == 0:
        nan = np.full(lat.shape, np.nan)
        return nan, nan.copy(), None
    native_m = _global_native_m(ds)
    factor = _decimation_factor(target_m, native_m)
    dlat = (180.0 / ds.height) * factor
    coslat = np.cos(np.radians(lat))
    dlon = np.minimum(dlat / np.maximum(coslat, 1e-12), 180.0)
    lat_n = np.minimum(lat + dlat, 90.0)
    lat_s = np.maximum(lat - dlat, -90.0)
    lats = np.concatenate([lat, lat, lat_n, lat_s])
    lons = np.concatenate([lon + dlon, lon - dlon, lon, lon])
    u, v, wrap_x = _global_latlon_to_uv(ds, lats, lons)
    scale, offset = _scale_and_offset(ds, is_global=True)
    vals = _sample_decimated(ds, u, v, factor, scale, offset, wrap_x=wrap_x).reshape(4, lat.size)
    corr = MOON_RADIUS_M / (MOON_RADIUS_M + vals.mean(axis=0))
    east_run = 2.0 * MOON_RADIUS_M * coslat * np.radians(dlon)
    with np.errstate(divide="ignore", invalid="ignore"):
        d_east = np.where(east_run > 1e-3, (vals[0] - vals[1]) / east_run, 0.0)
        d_east = np.where(np.isfinite(vals[0]) & np.isfinite(vals[1]), d_east, np.nan)
    d_north = (vals[2] - vals[3]) / (MOON_RADIUS_M * np.radians(lat_n - lat_s))
    return corr * d_east, corr * d_north, native_m * factor


def light_vector(light_lon_deg: float, light_lat_deg: float) -> np.ndarray:
    """Unit vector (Moon body-fixed X = lon 0, Y = lon 90 E, Z = north) toward a distant light
    that is overhead at (light_lat_deg, light_lon_deg)."""
    phi, lam = math.radians(light_lat_deg), math.radians(light_lon_deg)
    return np.array([math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi)])


def lambert_shade(
    lat: np.ndarray, lon: np.ndarray, d_east: np.ndarray, d_north: np.ndarray, light: np.ndarray, exaggeration: float = 1.0
) -> np.ndarray:
    """cos(incidence) in [0, 1] of a surface with ground slopes (d_east, d_north) at lat/lon."""
    phi, lam = np.radians(lat), np.radians(lon)
    sp, cp, sl, cl = np.sin(phi), np.cos(phi), np.sin(lam), np.cos(lam)
    lx, ly, lz = light
    # The light in each pixel's East/North/Up axes.
    l_east = -sl * lx + cl * ly
    l_north = -sp * cl * lx - sp * sl * ly + cp * lz
    l_up = cp * cl * lx + cp * sl * ly + sp * lz
    ge, gn = exaggeration * d_east, exaggeration * d_north
    return np.clip((-ge * l_east - gn * l_north + l_up) / np.sqrt(ge * ge + gn * gn + 1.0), 0.0, 1.0)


def hillshade_values(
    z: int,
    row: int,
    col: int,
    light_lon_deg: float = DEFAULT_LIGHT_LON_DEG,
    light_lat_deg: float = DEFAULT_LIGHT_LAT_DEG,
    exaggeration: float = 1.0,
) -> tuple[np.ndarray, str, float]:
    """Shade (256 x 256 in [0, 1], row 0 = north), source label and resolution for one tile."""
    lat, lon = _tile_pixel_latlon(z, row, col)
    target = _tile_ground_resolution_m(z)
    d_east, d_north, tier, polar_res = _polar_slopes(lat, lon, target)
    ok = np.isfinite(d_east) & np.isfinite(d_north)
    used_polar = bool(np.any(ok))
    used_global, global_res = False, None
    if not np.all(ok):
        missing = ~ok
        g_east, g_north, global_res = _global_slopes(lat[missing], lon[missing], target)
        d_east[missing], d_north[missing] = g_east, g_north
        used_global = bool(np.any(np.isfinite(g_east) & np.isfinite(g_north)))
    if not (np.all(np.isfinite(d_east)) and np.all(np.isfinite(d_north))):
        raise TileUnavailableError(
            f"The LOLA DEMs on the server do not cover hillshade tile {z}/{row}/{col} (global DEM missing?)"
        )
    shade = lambert_shade(lat, lon, d_east, d_north, light_vector(light_lon_deg, light_lat_deg), exaggeration)
    resolutions = [r for r, used in ((polar_res, used_polar), (global_res, used_global)) if used and r]
    return shade, _source_label(tier, used_polar, used_global), max(resolutions)


def fade_alpha(lat: np.ndarray, fade_start_lat: float, fade_end_lat: float) -> np.ndarray:
    """Opacity in [0, 1]: 0 at fade_start_lat, 1 at fade_end_lat, linear in latitude between them."""
    return np.clip((lat - fade_start_lat) / (fade_end_lat - fade_start_lat), 0.0, 1.0)


# ---------------------------------------------------------------- encoding


def _encode_png(pixels: np.ndarray) -> bytes:
    """Encode an (H, W, C) uint8 array as PNG: C = 1 gray, 2 gray + alpha, 3 RGB."""
    color_type = {1: 0, 2: 4, 3: 2}[pixels.shape[2]]
    height, width, channels = pixels.shape
    rows = pixels.reshape(height, width * channels)
    # PNG "Up" filter (type 2): terrain rows are similar, so the deltas compress well.
    prev = np.vstack([np.zeros((1, width * channels), dtype=np.uint8), rows[:-1]])
    filtered = (rows.astype(np.int16) - prev.astype(np.int16)).astype(np.uint8)
    raw = np.hstack([np.full((height, 1), 2, dtype=np.uint8), filtered]).tobytes()

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)  # 8-bit, no interlace
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


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
    return _encode_png(rgb)


def encode_heightmap(elevations_m: np.ndarray) -> bytes:
    """Little-endian float32 bytes, row 0 first, quantized to 1/256 m (exact in float32
    below 65,536 m, and the low mantissa bits left at zero compress well)."""
    q = np.round(np.asarray(elevations_m, dtype=np.float64) * 256.0) / 256.0
    return q.astype("<f4").tobytes()


def encode_hillshade_png(shade: np.ndarray, alpha: np.ndarray | None = None) -> bytes:
    """8-bit gray PNG of shade in [0, 1]; gray + alpha when an opacity grid is given."""
    gray = np.round(np.clip(shade, 0.0, 1.0) * 255.0).astype(np.uint8)
    if alpha is None:
        return _encode_png(gray[..., None])
    a = np.round(np.clip(alpha, 0.0, 1.0) * 255.0).astype(np.uint8)
    return _encode_png(np.stack([gray, a], axis=-1))


# ---------------------------------------------------------------- rendering and cache


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


def tile_etag(
    z: int,
    row: int,
    col: int,
    signature: tuple[Any, ...] | None = None,
    kind: str = "terrarium",
    params: tuple[Any, ...] = (),
) -> str:
    """Strong ETag for a tile: depends on the product, its parameters, the tile index and on
    the DEM files on disk. Terrarium tiles keep the ETag they had before the other products."""
    sig = dem_signature() if signature is None else signature
    ident = (z, row, col, sig) if kind == "terrarium" and not params else (kind, z, row, col, params, sig)
    digest = hashlib.sha1(repr(ident).encode()).hexdigest()[:20]
    return f'"{digest}"'


@dataclass(frozen=True)
class RenderedTile:
    body: bytes
    media_type: str
    source: str
    resolution_m: float
    etag: str


_tile_cache: OrderedDict[tuple[Any, ...], tuple[bytes, str, float]] = OrderedDict()
_tile_cache_bytes = 0
_tile_cache_lock = threading.Lock()

_MEDIA_TYPES = {"terrarium": "image/png", "heightmap": "application/octet-stream", "hillshade": "image/png"}


def _hillshade_params(
    light_lon: float, light_lat: float, exaggeration: float, fade_start_lat: float | None, fade_end_lat: float | None
) -> tuple[Any, ...]:
    """Validated parameters rounded to 0.01 (also the cache key, so near-identical requests share it)."""
    if not -90.0 <= light_lat <= 90.0:
        raise ValueError("light_lat must be between -90 and 90")
    if not 0.1 <= exaggeration <= 20.0:
        raise ValueError("exaggeration must be between 0.1 and 20")
    if (fade_start_lat is None) != (fade_end_lat is None):
        raise ValueError("fade_start_lat and fade_end_lat go together")
    fade: tuple[float, float] | None = None
    if fade_start_lat is not None and fade_end_lat is not None:
        if not (-90.0 <= fade_start_lat <= 90.0 and -90.0 <= fade_end_lat <= 90.0):
            raise ValueError("fade latitudes must be between -90 and 90")
        fade = (round(fade_start_lat, 2), round(fade_end_lat, 2))
        if fade[0] == fade[1]:
            raise ValueError("fade_start_lat and fade_end_lat must differ")
    return (round(light_lon % 360.0, 2) % 360.0, round(light_lat, 2), round(exaggeration, 2), fade)


def _render(kind: str, z: int, row: int, col: int, params: tuple[Any, ...]) -> tuple[bytes, str, float]:
    if kind == "terrarium":
        lat, lon = _tile_pixel_latlon(z, row, col)
        elev, source, res = _elevations(lat, lon, _tile_ground_resolution_m(z), f"{z}/{row}/{col}")
        return encode_terrarium_png(elev), source, res
    if kind == "heightmap":
        elev, source, res = heightmap_elevations(z, row, col)
        return encode_heightmap(elev), source, res
    light_lon, light_lat, exaggeration, fade = params
    shade, source, res = hillshade_values(z, row, col, light_lon, light_lat, exaggeration)
    alpha = None
    if fade is not None:
        lat, _ = _tile_pixel_latlon(z, row, col)
        alpha = fade_alpha(lat, *fade)
    return encode_hillshade_png(shade, alpha), source, res


def render_tile(
    kind: str,
    z: int,
    row: int,
    col: int,
    *,
    light_lon: float = DEFAULT_LIGHT_LON_DEG,
    light_lat: float = DEFAULT_LIGHT_LAT_DEG,
    exaggeration: float = 1.0,
    fade_start_lat: float | None = None,
    fade_end_lat: float | None = None,
) -> RenderedTile:
    """Render one tile product ("terrarium", "heightmap" or "hillshade"), with an in-process
    LRU cache bounded in bytes. The cache key includes the DEM signature, so replacing a
    raster invalidates it. Raises TileOutOfRangeError (also for invalid hillshade
    parameters) or TileUnavailableError."""
    if kind not in _MEDIA_TYPES:
        raise ValueError(f"unknown tile product {kind!r}")
    tile_bounds(z, row, col, MAX_HEIGHTMAP_LEVEL if kind == "heightmap" else MAX_TILE_LEVEL)
    params: tuple[Any, ...] = ()
    if kind == "hillshade":
        try:
            params = _hillshade_params(light_lon, light_lat, exaggeration, fade_start_lat, fade_end_lat)
        except ValueError as exc:
            raise TileOutOfRangeError(str(exc)) from exc
    sig = dem_signature()
    etag = tile_etag(z, row, col, sig, kind, params)
    key = (kind, z, row, col, params, sig)
    with _tile_cache_lock:
        hit = _tile_cache.get(key)
        if hit is not None:
            _tile_cache.move_to_end(key)
            return RenderedTile(hit[0], _MEDIA_TYPES[kind], hit[1], hit[2], etag)
    body, source, res = _render(kind, z, row, col, params)
    _cache_put(key, (body, source, res))
    return RenderedTile(body, _MEDIA_TYPES[kind], source, res, etag)


def _cache_put(key: tuple[Any, ...], value: tuple[bytes, str, float]) -> None:
    global _tile_cache_bytes
    with _tile_cache_lock:
        old = _tile_cache.pop(key, None)
        if old is not None:
            _tile_cache_bytes -= len(old[0])
        _tile_cache[key] = value
        _tile_cache_bytes += len(value[0])
        while _tile_cache_bytes > _TILE_CACHE_MAX_BYTES and len(_tile_cache) > 1:
            _, dropped = _tile_cache.popitem(last=False)
            _tile_cache_bytes -= len(dropped[0])


def render_terrain_tile(z: int, row: int, col: int) -> tuple[bytes, str, str]:
    """Terrarium PNG bytes, source label and ETag for a tile (see render_tile)."""
    tile = render_tile("terrarium", z, row, col)
    return tile.body, tile.source, tile.etag


def clear_tile_cache() -> None:
    global _tile_cache_bytes
    with _tile_cache_lock:
        _tile_cache.clear()
        _tile_cache_bytes = 0
