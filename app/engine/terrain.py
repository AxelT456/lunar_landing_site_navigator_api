"""Lunar LOLA DEM/slope sampling (South Pole multi-res + global), 3D terrain patches, and 360-deg horizon raycasting."""

from __future__ import annotations

import atexit
import logging
import math
import os
import threading
from pathlib import Path
from typing import Any

import numpy as np
import rasterio
from rasterio.errors import RasterioError
from rasterio.windows import Window

from app.engine.coordinates import (
    MOON_RADIUS_M,
    local_enu_basis_in_polar,
    polar_stereographic_from_latlon,
    polar_stereographic_scale_factor,
    validate_lat_lon,
)

logger = logging.getLogger(__name__)

POLAR_TIERS: tuple[tuple[str, str, str], ...] = (
    ("20MPP", "LDEM_80S_20MPP_ADJ.TIF", "LDSM_80S_20MPP_ADJ.TIF"),
    ("40MPP", "LDEM_80S_40MPP_ADJ.TIF", "LDSM_80S_40MPP_ADJ.TIF"),
    ("80MPP", "LDEM_80S_80MPP_ADJ.TIF", "LDSM_80S_80MPP_ADJ.TIF"),
)

LDEM_FILENAME = "LDEM_80S_80MPP_ADJ.TIF"
LDSM_FILENAME = "LDSM_80S_80MPP_ADJ.TIF"

GLOBAL_DEM_CANDIDATES: tuple[str, ...] = (
    "ldem_64.tif",
    "ldem_64_uint.tif",
    "ldem_16.tif",
    "ldem_16_uint.tif",
    "ldem_4.tif",
    "ldem_4_uint.tif",
)

_RASTER_LOCK = threading.RLock()
_RASTER_CACHE: dict[str, tuple[tuple[int, int], rasterio.DatasetReader]] = {}
_TRANSFORMER_CACHE: dict[tuple[str, str], Any] = {}


def _get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def _is_complete_raster_file(path: Path) -> bool:
    if path.name.endswith(".tmp") or path.name.startswith("."):
        return False
    try:
        return path.is_file() and path.stat().st_size > 1024
    except OSError:
        return False


def _find_lola_file(filename: str) -> Path | None:
    if filename.endswith(".tmp"):
        return None
    data_dir = _get_data_dir()
    candidates = [
        data_dir / "raw" / "lola" / filename,
        data_dir / "raw" / filename,
    ]
    for path in candidates:
        if _is_complete_raster_file(path):
            return path
    return None


def _find_global_dem_file() -> Path | None:
    data_dir = _get_data_dir()
    dirs = [
        data_dir / "raw" / "lola_global",
        data_dir / "lola_global",
    ]
    for directory in dirs:
        if not directory.is_dir():
            continue
        for name in GLOBAL_DEM_CANDIDATES:
            candidate = directory / name
            if _is_complete_raster_file(candidate):
                return candidate
        try:
            for entry in sorted(directory.iterdir()):
                if entry.suffix.lower() in {".tif", ".tiff"} and _is_complete_raster_file(entry):
                    return entry
        except OSError:
            continue
    return None


def _get_dataset_by_path(path: Path | None) -> rasterio.DatasetReader | None:
    if path is None or not _is_complete_raster_file(path):
        return None
    try:
        st = path.stat()
    except OSError:
        return None

    sig = (st.st_mtime_ns, st.st_size)
    key = str(path)
    with _RASTER_LOCK:
        cached = _RASTER_CACHE.get(key)
        if cached is not None:
            cached_sig, cached_ds = cached
            if cached_sig == sig and not cached_ds.closed:
                return cached_ds
            try:
                cached_ds.close()
            except Exception:
                pass
            _RASTER_CACHE.pop(key, None)

        try:
            ds = rasterio.open(path)
        except RasterioError as exc:
            logger.warning("Failed opening LOLA raster %s: %s", path, exc)
            return None

        _RASTER_CACHE[key] = (sig, ds)
        return ds


def _get_dataset(filename: str) -> rasterio.DatasetReader | None:
    return _get_dataset_by_path(_find_lola_file(filename))


def _get_best_polar_datasets() -> tuple[rasterio.DatasetReader | None, rasterio.DatasetReader | None, str | None]:
    """Return finest available South Pole (dem_ds, slope_ds, tier) in 20MPP -> 40MPP -> 80MPP order."""
    for tier, dem_name, slope_name in POLAR_TIERS:
        dem_ds = _get_dataset(dem_name)
        if dem_ds is not None:
            slope_ds = _get_dataset(slope_name)
            return dem_ds, slope_ds, tier
    return None, None, None


def _get_global_dataset() -> rasterio.DatasetReader | None:
    return _get_dataset_by_path(_find_global_dem_file())


def _safe_polar_coords(lat: float, lon: float) -> tuple[float, float]:
    """Compute South Polar Stereographic coordinates, guarding the +90 deg North Pole singularity."""
    if lat >= 90.0:
        return polar_stereographic_from_latlon(89.9999, lon)
    return polar_stereographic_from_latlon(lat, lon)


def _scale_and_offset(ds: rasterio.DatasetReader, is_global: bool = False) -> tuple[float, float]:
    scale = float(ds.scales[0]) if ds.scales and ds.scales[0] is not None else 1.0
    offset = float(ds.offsets[0]) if ds.offsets and ds.offsets[0] is not None else 0.0
    if not is_global:
        return scale, offset

    unit = (ds.units[0] or "").strip().lower() if ds.units else ""
    if unit in {"km", "kilometer", "kilometers", "kilometre", "kilometres"}:
        return scale * 1000.0, offset * 1000.0
    if unit in {"m", "meter", "meters", "metre", "metres"}:
        return scale, offset

    # Un-georeferenced NASA SVS LOLA global TIFFs (ldem_64.tif / ldem_16.tif / ldem_*_uint.tif)
    if ds.crs is None and ds.transform.is_identity and scale == 1.0 and offset == 0.0:
        dtype = str(ds.dtypes[0]).lower()
        if "uint" in dtype:
            # SVS uint16 is half-meters relative to 1,727,400 m (-10,000 m vs 1,737,400 m)
            return 0.5, -10_000.0
        if "float" in dtype:
            # SVS float32 is kilometers relative to 1,737.4 km
            return 1000.0, 0.0
    return scale, offset


def _read_scaled_window(
    ds: rasterio.DatasetReader,
    col0: int,
    row0: int,
    width: int,
    height: int,
    scale_override: float | None = None,
    offset_override: float | None = None,
) -> np.ndarray:
    """Read a raster window into float64, masking nodata and applying scale/offset."""
    out = np.full((height, width), np.nan, dtype=np.float64)
    c_start = max(0, col0)
    r_start = max(0, row0)
    c_end = min(ds.width, col0 + width)
    r_end = min(ds.height, row0 + height)
    if c_end <= c_start or r_end <= r_start:
        return out

    with _RASTER_LOCK:
        if ds.closed:
            return out
        raw = ds.read(
            1,
            window=Window(c_start, r_start, c_end - c_start, r_end - r_start),
        ).astype(np.float64)

    nodata = ds.nodata
    if nodata is not None:
        if isinstance(nodata, float) and math.isnan(nodata):
            pass
        else:
            raw = np.where(raw == float(nodata), np.nan, raw)

    if scale_override is None or offset_override is None:
        scale = float(ds.scales[0]) if ds.scales and ds.scales[0] is not None else 1.0
        offset = float(ds.offsets[0]) if ds.offsets and ds.offsets[0] is not None else 0.0
    else:
        scale, offset = scale_override, offset_override

    if scale != 1.0 or offset != 0.0:
        raw = raw * scale + offset

    dst_r0 = r_start - row0
    dst_c0 = c_start - col0
    out[dst_r0 : dst_r0 + raw.shape[0], dst_c0 : dst_c0 + raw.shape[1]] = raw
    return out


def _bilinear_blend(
    v00: np.ndarray,
    v01: np.ndarray,
    v10: np.ndarray,
    v11: np.ndarray,
    wc: np.ndarray,
    wr: np.ndarray,
) -> np.ndarray:
    """Mask-aware bilinear blend ignoring NaN corners."""
    w00 = (1.0 - wr) * (1.0 - wc)
    w01 = (1.0 - wr) * wc
    w10 = wr * (1.0 - wc)
    w11 = wr * wc

    vals = np.stack((v00, v01, v10, v11), axis=0)
    weights = np.stack((w00, w01, w10, w11), axis=0)
    finite_mask = np.isfinite(vals)
    weights_masked = np.where(finite_mask, weights, 0.0)
    vals_masked = np.where(finite_mask, vals, 0.0)

    w_sum = np.sum(weights_masked, axis=0)
    return np.where(
        w_sum > 0.0,
        np.sum(vals_masked * weights_masked, axis=0) / np.where(w_sum > 0.0, w_sum, 1.0),
        np.nan,
    )


def _sample_raster_bilinear(
    ds: rasterio.DatasetReader,
    x_coords: np.ndarray,
    y_coords: np.ndarray,
) -> np.ndarray:
    """Vectorized bilinear interpolation over GDAL pixel-center-registered raster."""
    x_arr = np.asarray(x_coords, dtype=np.float64)
    y_arr = np.asarray(y_coords, dtype=np.float64)
    orig_shape = x_arr.shape
    x_flat = x_arr.ravel()
    y_flat = y_arr.ravel()

    inv_t = ~ds.transform
    col_f, row_f = inv_t @ (x_flat, y_flat)
    u = np.asarray(col_f, dtype=np.float64) - 0.5
    v = np.asarray(row_f, dtype=np.float64) - 0.5

    valid = (
        np.isfinite(u)
        & np.isfinite(v)
        & (u >= -0.5)
        & (u <= ds.width - 0.5)
        & (v >= -0.5)
        & (v <= ds.height - 0.5)
    )
    result = np.full(x_flat.shape, np.nan, dtype=np.float64)
    if not np.any(valid):
        return result.reshape(orig_shape)

    u_v = np.clip(u[valid], 0.0, max(0.0, float(ds.width - 1)))
    v_v = np.clip(v[valid], 0.0, max(0.0, float(ds.height - 1)))

    c0 = np.floor(u_v).astype(np.int64)
    r0 = np.floor(v_v).astype(np.int64)
    c1 = np.minimum(c0 + 1, ds.width - 1)
    r1 = np.minimum(r0 + 1, ds.height - 1)

    wc = u_v - c0
    wr = v_v - r0

    win_c0 = int(c0.min())
    win_r0 = int(r0.min())
    win_c1 = int(c1.max()) + 1
    win_r1 = int(r1.max()) + 1

    patch = _read_scaled_window(ds, win_c0, win_r0, win_c1 - win_c0, win_r1 - win_r0)

    lc0 = c0 - win_c0
    lc1 = c1 - win_c0
    lr0 = r0 - win_r0
    lr1 = r1 - win_r0

    result[valid] = _bilinear_blend(
        patch[lr0, lc0],
        patch[lr0, lc1],
        patch[lr1, lc0],
        patch[lr1, lc1],
        wc,
        wr,
    )
    return result.reshape(orig_shape)


def _global_latlon_to_uv(
    ds: rasterio.DatasetReader,
    lat_flat: np.ndarray,
    lon_flat: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, bool]:
    """Map planetocentric (lat, lon) arrays to 0-based pixel-center coordinates (u, v) and wrap_x flag."""
    H, W = ds.height, ds.width
    lat_c = np.clip(lat_flat, -90.0, 90.0)

    if ds.crs is None and ds.transform.is_identity:
        # NASA SVS global equirectangular TIFFs are centered on 0 deg lon (-180..+180)
        lon_w = ((lon_flat + 180.0) % 360.0) - 180.0
        u = ((lon_w + 180.0) / 360.0) * float(W) - 0.5
        v = ((90.0 - lat_c) / 180.0) * float(H) - 0.5
        return u, v, True

    if ds.crs is not None and ds.crs.is_projected:
        import pyproj

        key = (ds.crs.geodetic_crs.to_wkt(), ds.crs.to_wkt())
        with _RASTER_LOCK:
            tf = _TRANSFORMER_CACHE.get(key)
            if tf is None:
                tf = pyproj.Transformer.from_crs(ds.crs.geodetic_crs, ds.crs, always_xy=True)
                _TRANSFORMER_CACHE[key] = tf
        lon_w = ((lon_flat + 180.0) % 360.0) - 180.0
        x_proj, y_proj = tf.transform(lon_w, lat_c)
        col_f, row_f = (~ds.transform) @ (np.asarray(x_proj, dtype=np.float64), np.asarray(y_proj, dtype=np.float64))
        return np.asarray(col_f, dtype=np.float64) - 0.5, np.asarray(row_f, dtype=np.float64) - 0.5, False

    bounds = ds.bounds
    if bounds.right > 180.5 and bounds.left >= -0.5:
        lon_coord = np.mod(lon_flat, 360.0)
    else:
        lon_coord = ((lon_flat + 180.0) % 360.0) - 180.0

    col_f, row_f = (~ds.transform) @ (lon_coord, lat_c)
    wrap_x = abs(abs(float(ds.transform.a) * float(W)) - 360.0) < 1e-2
    return np.asarray(col_f, dtype=np.float64) - 0.5, np.asarray(row_f, dtype=np.float64) - 0.5, wrap_x


def _sample_global_dem_bilinear(
    ds: rasterio.DatasetReader,
    lat_deg: np.ndarray,
    lon_deg: np.ndarray,
) -> np.ndarray:
    """Vectorized bilinear interpolation in meters over a global LOLA DEM (handles +/-180 wrapping)."""
    lat_arr = np.asarray(lat_deg, dtype=np.float64)
    lon_arr = np.asarray(lon_deg, dtype=np.float64)
    orig_shape = lat_arr.shape
    lat_flat = lat_arr.ravel()
    lon_flat = lon_arr.ravel()

    H, W = ds.height, ds.width
    u, v, wrap_x = _global_latlon_to_uv(ds, lat_flat, lon_flat)

    if wrap_x:
        valid = np.isfinite(u) & np.isfinite(v) & (v >= -0.5) & (v <= H - 0.5)
    else:
        valid = (
            np.isfinite(u)
            & np.isfinite(v)
            & (u >= -0.5)
            & (u <= W - 0.5)
            & (v >= -0.5)
            & (v <= H - 0.5)
        )

    result = np.full(lat_flat.shape, np.nan, dtype=np.float64)
    if not np.any(valid):
        return result.reshape(orig_shape)

    u_v = u[valid]
    v_v = np.clip(v[valid], 0.0, max(0.0, float(H - 1)))

    if wrap_x:
        u_mod = np.mod(u_v, float(W))
        u_floor = np.floor(u_mod)
        c0 = u_floor.astype(np.int64) % W
        c1 = (c0 + 1) % W
        wc = u_mod - u_floor
    else:
        u_clip = np.clip(u_v, 0.0, max(0.0, float(W - 1)))
        c0 = np.floor(u_clip).astype(np.int64)
        c1 = np.minimum(c0 + 1, W - 1)
        wc = u_clip - c0

    r0 = np.floor(v_v).astype(np.int64)
    r1 = np.minimum(r0 + 1, H - 1)
    wr = v_v - r0

    scale, offset = _scale_and_offset(ds, is_global=True)
    win_r0 = int(r0.min())
    win_r1 = int(r1.max()) + 1
    win_h = win_r1 - win_r0

    all_c = np.concatenate((c0, c1))
    c_min = int(all_c.min())
    c_max = int(all_c.max())
    span_w = c_max - c_min + 1

    lr0 = r0 - win_r0
    lr1 = r1 - win_r0

    if span_w <= max(2048, W // 2) or (win_h * span_w) <= 4_000_000:
        patch = _read_scaled_window(ds, c_min, win_r0, span_w, win_h, scale, offset)
        v00 = patch[lr0, c0 - c_min]
        v01 = patch[lr0, c1 - c_min]
        v10 = patch[lr1, c0 - c_min]
        v11 = patch[lr1, c1 - c_min]
    else:
        # Query crosses the +/-180 antimeridian seam: read left and right strips separately
        half_w = W // 2
        left_cols = all_c[all_c < half_w]
        right_cols = all_c[all_c >= half_w]
        if left_cols.size > 0 and right_cols.size > 0:
            left_end = int(left_cols.max()) + 1
            right_start = int(right_cols.min())
            left_patch = _read_scaled_window(ds, 0, win_r0, left_end, win_h, scale, offset)
            right_patch = _read_scaled_window(ds, right_start, win_r0, W - right_start, win_h, scale, offset)

            def _pick(rr: np.ndarray, cc: np.ndarray) -> np.ndarray:
                in_left = cc < half_w
                vals = np.empty(cc.shape, dtype=np.float64)
                if np.any(in_left):
                    vals[in_left] = left_patch[rr[in_left], cc[in_left]]
                if np.any(~in_left):
                    vals[~in_left] = right_patch[rr[~in_left], cc[~in_left] - right_start]
                return vals

            v00 = _pick(lr0, c0)
            v01 = _pick(lr0, c1)
            v10 = _pick(lr1, c0)
            v11 = _pick(lr1, c1)
        else:
            patch = _read_scaled_window(ds, c_min, win_r0, span_w, win_h, scale, offset)
            v00 = patch[lr0, c0 - c_min]
            v01 = patch[lr0, c1 - c_min]
            v10 = patch[lr1, c0 - c_min]
            v11 = patch[lr1, c1 - c_min]

    result[valid] = _bilinear_blend(v00, v01, v10, v11, wc, wr)
    return result.reshape(orig_shape)


def _compute_point_slope_from_dem(dem_ds: rasterio.DatasetReader, x_m: float, y_m: float) -> float | None:
    """Estimate terrain slope (degrees) from 3x3 DEM finite differences when LDSM is absent."""
    col_f, row_f = (~dem_ds.transform) @ (x_m, y_m)
    c = int(round(col_f - 0.5))
    r = int(round(row_f - 0.5))
    if c < 1 or c >= dem_ds.width - 1 or r < 1 or r >= dem_ds.height - 1:
        return None
    z = _read_scaled_window(dem_ds, c - 1, r - 1, 3, 3)
    if not np.all(np.isfinite(z)):
        return None

    dx = abs(float(dem_ds.transform.a))
    dy = abs(float(dem_ds.transform.e))
    dz_dx = ((z[0, 2] + 2.0 * z[1, 2] + z[2, 2]) - (z[0, 0] + 2.0 * z[1, 0] + z[2, 0])) / (8.0 * dx)
    dz_dy = ((z[2, 0] + 2.0 * z[2, 1] + z[2, 2]) - (z[0, 0] + 2.0 * z[0, 1] + z[0, 2])) / (8.0 * dy)
    return float(math.degrees(math.atan(math.hypot(dz_dx, dz_dy))))


def _compute_global_point_slope_from_dem(
    ds: rasterio.DatasetReader,
    lat_deg: float,
    lon_deg: float,
) -> float | None:
    """Compute metric 3x3 Horn slope on global equirectangular DEM using lunar radius R and cos(lat)."""
    H, W = ds.height, ds.width
    if H < 2 or W < 2:
        return None

    u_arr, v_arr, wrap_x = _global_latlon_to_uv(
        ds,
        np.array([lat_deg], dtype=np.float64),
        np.array([lon_deg], dtype=np.float64),
    )
    u0, v0 = float(u_arr[0]), float(v_arr[0])
    if not (math.isfinite(u0) and math.isfinite(v0)):
        return None

    r = min(max(int(round(v0)), 0), H - 1)
    if wrap_x:
        c = int(round(u0)) % W
        cols = [(c - 1) % W, c, (c + 1) % W]
        col_span = 2
    else:
        if u0 < -0.5 or u0 > W - 0.5 or v0 < -0.5 or v0 > H - 0.5:
            return None
        c = min(max(int(round(u0)), 0), W - 1)
        cols = [max(0, c - 1), c, min(W - 1, c + 1)]
        col_span = cols[2] - cols[0]
        if col_span == 0:
            return None

    rows = [max(0, r - 1), r, min(H - 1, r + 1)]
    row_span = rows[2] - rows[0]
    if row_span == 0:
        return None

    scale, offset = _scale_and_offset(ds, is_global=True)
    r_min, r_max = rows[0], rows[2]
    r_h = r_max - r_min + 1

    z = np.empty((3, 3), dtype=np.float64)
    if cols[0] < cols[1] < cols[2]:
        patch = _read_scaled_window(ds, cols[0], r_min, 3, r_h, scale, offset)
        for ri, rr in enumerate(rows):
            z[ri, :] = patch[rr - r_min, :]
    else:
        # Seam wrap at col 0 or W-1: fetch column by column across 3 rows
        for ci, cc in enumerate(cols):
            col_patch = _read_scaled_window(ds, cc, r_min, 1, r_h, scale, offset)
            for ri, rr in enumerate(rows):
                z[ri, ci] = col_patch[rr - r_min, 0]

    if not np.all(np.isfinite(z)):
        return None

    if ds.crs is not None and ds.crs.is_projected:
        dx_m = abs(float(ds.transform.a))
        dy_m = abs(float(ds.transform.e))
    else:
        if ds.crs is None and ds.transform.is_identity:
            dlon_deg = 360.0 / float(W)
            dlat_deg = 180.0 / float(H)
        else:
            dlon_deg = abs(float(ds.transform.a))
            dlat_deg = abs(float(ds.transform.e))
        cos_lat = max(math.cos(math.radians(lat_deg)), 1e-4)
        dx_m = MOON_RADIUS_M * math.radians(dlon_deg) * cos_lat
        dy_m = MOON_RADIUS_M * math.radians(dlat_deg)

    dz_dx = ((z[0, 2] + 2.0 * z[1, 2] + z[2, 2]) - (z[0, 0] + 2.0 * z[1, 0] + z[2, 0])) / (
        4.0 * float(col_span) * dx_m
    )
    dz_dy = ((z[2, 0] + 2.0 * z[2, 1] + z[2, 2]) - (z[0, 0] + 2.0 * z[0, 1] + z[0, 2])) / (
        4.0 * float(row_span) * dy_m
    )
    return float(math.degrees(math.atan(math.hypot(dz_dx, dz_dy))))


def _local_tangent_latlon_grid(
    lat_c: float,
    lon_norm: float,
    gx_m: np.ndarray,
    gy_m: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Project local tangent East (gx_m) and North (gy_m) meters onto spherical lunar (lat, lon)."""
    phi0 = math.radians(lat_c)
    lam0 = math.radians(lon_norm)
    cos_phi0, sin_phi0 = math.cos(phi0), math.sin(phi0)
    cos_lam0, sin_lam0 = math.cos(lam0), math.sin(lam0)

    ux, uy, uz = cos_phi0 * cos_lam0, cos_phi0 * sin_lam0, sin_phi0
    ex, ey, ez = -sin_lam0, cos_lam0, 0.0
    nx, ny, nz = -sin_phi0 * cos_lam0, -sin_phi0 * sin_lam0, cos_phi0

    d = np.hypot(gx_m, gy_m)
    d_safe = np.where(d > 0.0, d, 1.0)
    hx = np.where(d > 0.0, (gx_m * ex + gy_m * nx) / d_safe, 0.0)
    hy = np.where(d > 0.0, (gx_m * ey + gy_m * ny) / d_safe, 0.0)
    hz = np.where(d > 0.0, (gx_m * ez + gy_m * nz) / d_safe, 0.0)

    theta = d / MOON_RADIUS_M
    cos_th = np.cos(theta)
    sin_th = np.sin(theta)

    px3 = cos_th * ux + sin_th * hx
    py3 = cos_th * uy + sin_th * hy
    pz3 = cos_th * uz + sin_th * hz

    lat_grid = np.degrees(np.arcsin(np.clip(pz3, -1.0, 1.0)))
    lon_grid = np.degrees(np.arctan2(py3, px3))
    return lat_grid, lon_grid


def _synthetic_elevation_and_slope(
    x_m: float | np.ndarray,
    y_m: float | np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Deterministic smooth lunar South Pole synthetic terrain fallback.

    Models a Shackleton-like polar crater around (0, 0) plus regional ridges so
    unit tests run reproducibly when GeoTIFF files are absent or out of bounds.
    """
    x = np.asarray(x_m, dtype=np.float64)
    y = np.asarray(y_m, dtype=np.float64)

    r = np.hypot(x, y)
    r_safe = np.where(r > 1.0, r, 1.0)
    r_rim = 10_500.0
    sigma_rim = 3_200.0
    bowl = -2_100.0 * np.exp(-((r / 6_200.0) ** 2))
    rim = 2_100.0 * np.exp(-(((r - r_rim) / sigma_rim) ** 2))

    k1 = 2.0 * math.pi / 42_000.0
    k2 = 2.0 * math.pi / 17_000.0
    ridge = 650.0 * np.sin(k1 * x + 0.4) * np.cos(k1 * y - 0.3) + 220.0 * np.cos(k2 * (x - y))
    elev = 400.0 + bowl + rim + ridge

    dbowl_dr = -2_100.0 * np.exp(-((r / 6_200.0) ** 2)) * (-2.0 * r / (6_200.0 ** 2))
    drim_dr = 2_100.0 * np.exp(-(((r - r_rim) / sigma_rim) ** 2)) * (-2.0 * (r - r_rim) / (sigma_rim ** 2))
    d_radial = np.where(r > 1.0, (dbowl_dr + drim_dr) / r_safe, 0.0)

    dz_dx = (
        d_radial * x
        + 650.0 * k1 * np.cos(k1 * x + 0.4) * np.cos(k1 * y - 0.3)
        - 220.0 * k2 * np.sin(k2 * (x - y))
    )
    dz_dy = (
        d_radial * y
        - 650.0 * k1 * np.sin(k1 * x + 0.4) * np.sin(k1 * y - 0.3)
        + 220.0 * k2 * np.sin(k2 * (x - y))
    )
    slope = np.degrees(np.arctan(np.hypot(dz_dx, dz_dy)))
    return elev, slope


def sample_point_terrain(lat: float, lon: float) -> dict[str, Any]:
    """Sample LOLA elevation_m and slope_deg at (lat, lon), prioritizing 20MPP -> 40MPP -> 80MPP -> global."""
    lat_c, lon_norm = validate_lat_lon(lat, lon)
    polar_x_m, polar_y_m = _safe_polar_coords(lat_c, lon_norm)

    dem_ds, slope_ds, _ = _get_best_polar_datasets()

    elev_val: float | None = None
    slope_val: float | None = None
    source = "fallback"

    if dem_ds is not None:
        sampled_e = float(_sample_raster_bilinear(dem_ds, np.array([polar_x_m]), np.array([polar_y_m]))[0])
        if math.isfinite(sampled_e):
            elev_val = sampled_e
            source = "lola"

    if slope_ds is not None and source == "lola":
        sampled_s = float(_sample_raster_bilinear(slope_ds, np.array([polar_x_m]), np.array([polar_y_m]))[0])
        if math.isfinite(sampled_s):
            slope_val = max(0.0, sampled_s)

    if source == "lola" and slope_val is None and dem_ds is not None:
        dem_slope = _compute_point_slope_from_dem(dem_ds, polar_x_m, polar_y_m)
        if dem_slope is not None and math.isfinite(dem_slope):
            slope_val = max(0.0, dem_slope)

    if source != "lola":
        global_ds = _get_global_dataset()
        if global_ds is not None:
            sampled_ge = float(
                _sample_global_dem_bilinear(
                    global_ds,
                    np.array([lat_c], dtype=np.float64),
                    np.array([lon_norm], dtype=np.float64),
                )[0]
            )
            if math.isfinite(sampled_ge):
                elev_val = sampled_ge
                source = "lola_global"
                g_slope = _compute_global_point_slope_from_dem(global_ds, lat_c, lon_norm)
                if g_slope is not None and math.isfinite(g_slope):
                    slope_val = max(0.0, g_slope)

    if elev_val is None or slope_val is None:
        syn_e, syn_s = _synthetic_elevation_and_slope(polar_x_m, polar_y_m)
        if elev_val is None:
            elev_val = float(syn_e)
            source = "fallback"
        if slope_val is None:
            slope_val = float(syn_s)

    return {
        "elevation_m": float(elev_val),
        "slope_deg": float(slope_val),
        "polar_x_m": float(polar_x_m),
        "polar_y_m": float(polar_y_m),
        "source": source,
    }


def extract_terrain_patch(
    lat: float,
    lon: float,
    size_km: float = 12.0,
    resolution: int = 128,
) -> dict[str, Any]:
    """Extract a centered local East-North N x N terrain grid ready for Three.js PlaneGeometry.

    Grid orientation (row-major, resolution x resolution):
        rows 0 .. resolution-1 run North (-Z) to South (+Z)
        cols 0 .. resolution-1 run West (-X) to East (+X)
    """
    lat_c, lon_norm = validate_lat_lon(lat, lon)
    size_km_f = float(size_km)
    res_i = int(resolution)
    if not math.isfinite(size_km_f) or size_km_f <= 0.0:
        raise ValueError(f"size_km must be positive, got {size_km!r}")
    if res_i < 2 or res_i > 1024:
        raise ValueError(f"resolution must be between 2 and 1024, got {resolution!r}")

    center = sample_point_terrain(lat_c, lon_norm)
    cx = center["polar_x_m"]
    cy = center["polar_y_m"]

    size_m = size_km_f * 1000.0
    half_m = 0.5 * size_m
    cell_size_m = size_m / float(res_i - 1)

    xs_local = np.linspace(-half_m, half_m, res_i, dtype=np.float64)
    ys_local = np.linspace(half_m, -half_m, res_i, dtype=np.float64)
    gx, gy = np.meshgrid(xs_local, ys_local)

    dem_ds, slope_ds, _ = _get_best_polar_datasets()
    global_ds = _get_global_dataset()

    if center["source"] == "lola" and dem_ds is not None:
        east_xy, north_xy = local_enu_basis_in_polar(lon_norm)
        scale_k = polar_stereographic_scale_factor(lat_c)
        px = cx + scale_k * (gx * east_xy[0] + gy * north_xy[0])
        py = cy + scale_k * (gx * east_xy[1] + gy * north_xy[1])

        elev_grid = _sample_raster_bilinear(dem_ds, px, py)
        slope_grid = (
            _sample_raster_bilinear(slope_ds, px, py)
            if slope_ds is not None
            else np.full_like(px, np.nan)
        )

        # Fill edge pixels extending beyond 80S from global LOLA DEM if present
        missing_e = ~np.isfinite(elev_grid)
        if np.any(missing_e) and global_ds is not None:
            lat_g, lon_g = _local_tangent_latlon_grid(lat_c, lon_norm, gx, gy)
            g_elev = _sample_global_dem_bilinear(global_ds, lat_g, lon_g)
            elev_grid = np.where(missing_e, g_elev, elev_grid)
    elif center["source"] == "lola_global" and global_ds is not None:
        lat_g, lon_g = _local_tangent_latlon_grid(lat_c, lon_norm, gx, gy)
        elev_grid = _sample_global_dem_bilinear(global_ds, lat_g, lon_g)
        slope_grid = np.full_like(elev_grid, np.nan)
        px = cx + gx
        py = cy + gy
    else:
        px = cx + gx
        py = cy + gy
        elev_grid = np.full_like(gx, np.nan)
        slope_grid = np.full_like(gx, np.nan)

    if not np.all(np.isfinite(elev_grid)) or not np.all(np.isfinite(slope_grid)):
        syn_e, syn_s = _synthetic_elevation_and_slope(px, py)
        missing_e = ~np.isfinite(elev_grid)
        if np.any(missing_e):
            elev_grid = np.where(missing_e, syn_e, elev_grid)

        if np.any(~np.isfinite(slope_grid)):
            if center["source"] in {"lola", "lola_global"} and np.all(~missing_e):
                dz_dy_n, dz_dx_e = np.gradient(elev_grid, -cell_size_m, cell_size_m)
                dem_slope_grid = np.degrees(np.arctan(np.hypot(dz_dx_e, dz_dy_n)))
                slope_grid = np.where(np.isfinite(slope_grid), slope_grid, dem_slope_grid)
            else:
                slope_grid = np.where(np.isfinite(slope_grid), slope_grid, syn_s)

    slope_grid = np.maximum(0.0, slope_grid)

    return {
        "center": {
            "lat": float(lat_c),
            "lon": float(lon_norm),
            "polar_x_m": float(center["polar_x_m"]),
            "polar_y_m": float(center["polar_y_m"]),
            "elevation_m": float(center["elevation_m"]),
            "slope_deg": float(center["slope_deg"]),
        },
        "grid": {
            "size_km": float(size_km_f),
            "resolution": int(res_i),
            "cell_size_m": float(cell_size_m),
            "min_elevation_m": float(np.min(elev_grid)),
            "max_elevation_m": float(np.max(elev_grid)),
            "elevations_m": [float(v) for v in elev_grid.ravel()],
            "slopes_deg": [float(v) for v in slope_grid.ravel()],
        },
    }


def _ray_distances_m(max_r_m: float, near_step: float, far_step: float) -> tuple[np.ndarray, int]:
    near_end = min(2000.0, max_r_m)
    d_near = np.arange(near_step, near_end + 1e-6, near_step, dtype=np.float64)
    if max_r_m > 2000.0:
        d_far = np.arange(2000.0 + far_step, max_r_m + 1e-6, far_step, dtype=np.float64)
        return np.concatenate((d_near, d_far)), int(d_near.size)
    d_m = d_near if d_near.size > 0 else np.array([max_r_m], dtype=np.float64)
    return d_m, int(d_m.size)


def compute_horizon_profile(
    lat: float,
    lon: float,
    max_radius_km: float = 60.0,
) -> list[dict[str, Any]]:
    """Trace 360 azimuth rays (0..359 deg) across the lunar surface with spherical curvature.

    Azimuth convention: 0=North, 90=East, 180=South, 270=West.
    Accounts for Moon spherical curvature (R = 1,737,400 m) using exact 3D observer-to-target
    elevation angle: atan2((R + h1)*cos(d/R) - (R + h0), (R + h1)*sin(d/R)).
    """
    lat_c, lon_norm = validate_lat_lon(lat, lon)
    max_r_km = float(max_radius_km)
    if not math.isfinite(max_r_km) or max_r_km <= 0.0:
        raise ValueError(f"max_radius_km must be positive, got {max_radius_km!r}")

    center = sample_point_terrain(lat_c, lon_norm)
    h0 = center["elevation_m"]
    max_r_m = max_r_km * 1000.0

    global_ds = _get_global_dataset()
    if center["source"] == "lola_global" and global_ds is not None:
        arc_step_m = MOON_RADIUS_M * math.pi / float(max(global_ds.height, 180))
        near_step = max(40.0, min(200.0, 0.5 * arc_step_m))
        far_step_base = max(80.0, min(300.0, 0.5 * arc_step_m))
        far_step = far_step_base if max_r_m <= 120_000.0 else max(far_step_base, max_r_m / 1500.0)
        d_m, n_near = _ray_distances_m(max_r_m, near_step, far_step)
    else:
        # Dense 40 m sampling within 2 km, 80 m LOLA-matched sampling beyond 2 km
        far_step = 80.0 if max_r_m <= 120_000.0 else max_r_m / 1500.0
        d_m, n_near = _ray_distances_m(max_r_m, 40.0, far_step)

    phi0 = math.radians(lat_c)
    lam0 = math.radians(lon_norm)
    cos_phi0 = math.cos(phi0)
    sin_phi0 = math.sin(phi0)
    cos_lam0 = math.cos(lam0)
    sin_lam0 = math.sin(lam0)

    ux, uy, uz = cos_phi0 * cos_lam0, cos_phi0 * sin_lam0, sin_phi0
    ex, ey, ez = -sin_lam0, cos_lam0, 0.0
    nx, ny, nz = -sin_phi0 * cos_lam0, -sin_phi0 * sin_lam0, cos_phi0

    az_rad = np.radians(np.arange(360, dtype=np.float64))[:, None]
    sin_az = np.sin(az_rad)
    cos_az = np.cos(az_rad)

    dx = sin_az * ex + cos_az * nx
    dy = sin_az * ey + cos_az * ny
    dz = sin_az * ez + cos_az * nz

    theta = (d_m / MOON_RADIUS_M)[None, :]
    cos_th = np.cos(theta)
    sin_th = np.sin(theta)

    px3 = cos_th * ux + sin_th * dx
    py3 = cos_th * uy + sin_th * dy
    pz3 = cos_th * uz + sin_th * dz

    denom = np.maximum(1.0 - pz3, 1e-12)
    x_polar = (2.0 * MOON_RADIUS_M * py3) / denom
    y_polar = (2.0 * MOON_RADIUS_M * px3) / denom

    if center["source"] == "lola":
        near_dem_ds, _, _ = _get_best_polar_datasets()
        far_dem_ds = _get_dataset(LDEM_FILENAME) or near_dem_ds

        h1 = np.full(x_polar.shape, np.nan, dtype=np.float64)
        if near_dem_ds is not None and far_dem_ds is not None:
            if near_dem_ds is far_dem_ds or n_near >= d_m.size:
                h1 = _sample_raster_bilinear(far_dem_ds, x_polar, y_polar)
            else:
                h1[:, :n_near] = _sample_raster_bilinear(near_dem_ds, x_polar[:, :n_near], y_polar[:, :n_near])
                h1[:, n_near:] = _sample_raster_bilinear(far_dem_ds, x_polar[:, n_near:], y_polar[:, n_near:])

        missing = ~np.isfinite(h1)
        if np.any(missing) and global_ds is not None:
            lat_miss = np.degrees(np.arcsin(np.clip(pz3[missing], -1.0, 1.0)))
            lon_miss = np.degrees(np.arctan2(py3[missing], px3[missing]))
            h1[missing] = _sample_global_dem_bilinear(global_ds, lat_miss, lon_miss)

        if not np.any(np.isfinite(h1)):
            h1, _ = _synthetic_elevation_and_slope(x_polar, y_polar)
    elif center["source"] == "lola_global" and global_ds is not None:
        lat_ray = np.degrees(np.arcsin(np.clip(pz3, -1.0, 1.0)))
        lon_ray = np.degrees(np.arctan2(py3, px3))
        h1 = _sample_global_dem_bilinear(global_ds, lat_ray, lon_ray)
        if not np.any(np.isfinite(h1)):
            h1, _ = _synthetic_elevation_and_slope(x_polar, y_polar)
    else:
        h1, _ = _synthetic_elevation_and_slope(x_polar, y_polar)

    r0 = MOON_RADIUS_M + h0
    r1 = MOON_RADIUS_M + h1
    v_up = r1 * cos_th - r0
    v_horiz = r1 * sin_th
    el_deg = np.degrees(np.arctan2(v_up, v_horiz))

    valid_rays = np.any(np.isfinite(el_deg), axis=1)
    max_el = np.zeros(360, dtype=np.float64)
    if np.any(valid_rays):
        max_el[valid_rays] = np.nanmax(el_deg[valid_rays], axis=1)

    return [
        {"az_deg": int(az), "horizon_el_deg": float(max_el[az])}
        for az in range(360)
    ]


def _close_raster_cache() -> None:
    with _RASTER_LOCK:
        for _, (_, ds) in list(_RASTER_CACHE.items()):
            try:
                ds.close()
            except Exception:
                pass
        _RASTER_CACHE.clear()


atexit.register(_close_raster_cache)
