"""Lunar surface Sun/Earth ephemerides, horizon intersection, solar power, and Go/No-Go evaluation.

Primary backend: Skyfield using local NAIF SPICE kernels (DE421/DE440 + moon_080317.tf + moon_pa_*.bpc)
in the MOON_ME_DE421 Mean-Earth/Polar-Axis frame (<10 ms vectorized offline).
Fallback 1: NASA JPL Horizons REST API (https://ssd.jpl.nasa.gov/api/horizons.api).
Fallback 2: Offline analytical lunar ephemeris model.
"""

from __future__ import annotations

import atexit
import logging
import math
import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, IO

import httpx
import numpy as np
from skyfield.api import Loader, PlanetaryConstants

from app.engine.coordinates import MOON_RADIUS_M, az_el_to_vector_3d, validate_lat_lon

logger = logging.getLogger(__name__)

SOLAR_CONSTANT_W_M2 = 1361.0
SOLAR_ANGULAR_RADIUS_DEG = 0.2666
HORIZONS_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"

_SPICE_LOCK = threading.RLock()
_SPICE_CACHE: dict[tuple[Any, ...], "_SpiceBundle"] = {}


@dataclass
class _SpiceBundle:
    eph: Any
    ts: Any
    pc: PlanetaryConstants
    frame: Any
    frame_name: str
    bpc_handle: IO[bytes]


def _get_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def _pick_file(directory: Path, preferred: tuple[str, ...], suffix: str, min_bytes: int) -> Path | None:
    if not directory.is_dir():
        return None
    for name in preferred:
        candidate = directory / name
        try:
            if candidate.is_file() and candidate.stat().st_size >= min_bytes:
                return candidate
        except OSError:
            continue
    for entry in sorted(directory.iterdir()):
        if entry.name.endswith(".tmp"):
            continue
        if entry.suffix.lower() == suffix:
            try:
                if entry.is_file() and entry.stat().st_size >= min_bytes:
                    return entry
            except OSError:
                continue
    return None


def _get_spice_bundle() -> _SpiceBundle | None:
    data_dir = _get_data_dir()
    spice_dir = data_dir / "raw" / "spice"
    if not spice_dir.is_dir():
        spice_dir = data_dir / "spice"
    if not spice_dir.is_dir():
        return None

    bsp_path = _pick_file(spice_dir, ("de421.bsp", "de440s.bsp", "de440.bsp"), ".bsp", 100_000)
    bpc_path = _pick_file(spice_dir, ("moon_pa_de421_1900-2050.bpc",), ".bpc", 10_000)
    tf_path = _pick_file(spice_dir, ("moon_080317.tf",), ".tf", 500)
    if bsp_path is None or bpc_path is None or tf_path is None:
        return None

    try:
        bsp_st = bsp_path.stat()
        bpc_st = bpc_path.stat()
        tf_st = tf_path.stat()
    except OSError:
        return None

    cache_key = (
        str(bsp_path),
        bsp_st.st_mtime_ns,
        bsp_st.st_size,
        str(bpc_path),
        bpc_st.st_mtime_ns,
        bpc_st.st_size,
        str(tf_path),
        tf_st.st_mtime_ns,
        tf_st.st_size,
    )

    with _SPICE_LOCK:
        cached = _SPICE_CACHE.get(cache_key)
        if cached is not None:
            return cached

        # Close old cached binary handles before loading a new bundle
        for old_key in list(_SPICE_CACHE.keys()):
            old_bundle = _SPICE_CACHE.pop(old_key)
            try:
                old_bundle.bpc_handle.close()
            except Exception:
                pass

        loader = Loader(str(spice_dir), expire=False)
        ts = loader.timescale(builtin=True)
        eph = loader(bsp_path.name)

        pc = PlanetaryConstants()
        with open(tf_path, "rb") as tf_f:
            pc.read_text(tf_f)

        for tpc_path in sorted(spice_dir.glob("*.tpc")):
            try:
                if tpc_path.is_file() and tpc_path.stat().st_size > 100:
                    with open(tpc_path, "rb") as tpc_f:
                        pc.read_text(tpc_f)
            except Exception:
                pass

        # Keep the .bpc file handle open; Skyfield reads Chebyshev records lazily
        bpc_handle = open(bpc_path, "rb")
        try:
            pc.read_binary(bpc_handle)
        except Exception:
            bpc_handle.close()
            raise

        pc.variables.setdefault("BODY301_RADII", (1737.4, 1737.4, 1737.4))

        frame = None
        frame_name = ""
        for candidate_name in ("MOON_ME_DE421", "MOON_ME", "MOON_PA_DE421", "MOON_PA"):
            try:
                frame = pc.build_frame_named(candidate_name)
                frame_name = candidate_name
                break
            except Exception:
                continue

        if frame is None:
            bpc_handle.close()
            raise RuntimeError("Unable to build lunar orientation frame from SPICE kernels")

        bundle = _SpiceBundle(
            eph=eph,
            ts=ts,
            pc=pc,
            frame=frame,
            frame_name=frame_name,
            bpc_handle=bpc_handle,
        )
        _SPICE_CACHE[cache_key] = bundle
        return bundle


def _parse_utc_datetime(start_utc: str) -> datetime:
    raw = str(start_utc).strip()
    if not raw:
        raise ValueError("start_utc must not be empty")
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def _format_utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compute_ephemeris_skyfield(
    bundle: _SpiceBundle,
    lat: float,
    lon: float,
    elevation_m: float,
    dts_utc: list[datetime],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    with _SPICE_LOCK:
        t = bundle.ts.from_datetimes(dts_utc)
        moon = bundle.eph["moon"]
        sun = bundle.eph["sun"]
        earth = bundle.eph["earth"]
        site = moon + bundle.pc.build_latlon_degrees(
            bundle.frame,
            latitude_degrees=lat,
            longitude_degrees=lon,
            elevation_m=elevation_m,
        )
        obs = site.at(t)
        s_alt, s_az, s_dist = obs.observe(sun).apparent().altaz()
        e_alt, e_az, _ = obs.observe(earth).apparent().altaz()

    return (
        np.mod(np.asarray(s_az.degrees, dtype=np.float64), 360.0),
        np.asarray(s_alt.degrees, dtype=np.float64),
        np.asarray(s_dist.au, dtype=np.float64),
        np.mod(np.asarray(e_az.degrees, dtype=np.float64), 360.0),
        np.asarray(e_alt.degrees, dtype=np.float64),
    )


_HORIZONS_LINE_RE = re.compile(
    r"^\s*(\d{4}-[A-Za-z]{3}-\d{2}\s+\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s+.*?\s+([-+]?\d+(?:\.\d+)?)\s+([-+]?\d+(?:\.\d+)?)(?:\s+([-+]?\d+(?:\.\d+)?))?\s*$"
)


def _query_horizons_body(
    command: str,
    lat: float,
    lon: float,
    elevation_m: float,
    dts_utc: list[datetime],
    step_minutes: int,
    quantities: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray | None]:
    start_s = dts_utc[0].strftime("%Y-%m-%d %H:%M")
    stop_dt = dts_utc[-1] if len(dts_utc) > 1 else dts_utc[0] + timedelta(minutes=step_minutes)
    stop_s = stop_dt.strftime("%Y-%m-%d %H:%M")
    elev_km = elevation_m / 1000.0

    params = {
        "format": "json",
        "COMMAND": repr(command),
        "OBJ_DATA": "NO",
        "MAKE_EPHEM": "YES",
        "EPHEM_TYPE": "OBSERVER",
        "CENTER": repr("coord@301"),
        "COORD_TYPE": repr("GEODETIC"),
        "SITE_COORD": repr(f"{lon:.6f},{lat:.6f},{elev_km:.4f}"),
        "START_TIME": repr(start_s),
        "STOP_TIME": repr(stop_s),
        "STEP_SIZE": repr(f"{step_minutes} m"),
        "QUANTITIES": repr(quantities),
    }

    resp = httpx.get(HORIZONS_URL, params=params, timeout=15.0)
    resp.raise_for_status()
    payload = resp.json()
    text = payload.get("result", "")
    soe = text.find("56076SOE")
    eoe = text.find("56076EOE")
    if soe == -1 or eoe == -1:
        raise ValueError(f"JPL Horizons missing SOE/EOE block for command={command}")

    az_list: list[float] = []
    el_list: list[float] = []
    dist_list: list[float] = []
    for line in text[soe + 5 : eoe].splitlines():
        line_s = line.strip()
        if not line_s:
            continue
        tokens = line_s.split()
        # Last numeric fields: (az, el) for Q='4', or (az, el, delta, deldot) for Q='4,20'
        nums: list[float] = []
        for tok in reversed(tokens):
            try:
                nums.append(float(tok))
            except ValueError:
                break
        nums.reverse()
        if quantities == "4,20" and len(nums) >= 4:
            az_list.append(nums[0] % 360.0)
            el_list.append(nums[1])
            dist_list.append(nums[2])
        elif len(nums) >= 2:
            az_list.append(nums[0] % 360.0)
            el_list.append(nums[1])

    if len(az_list) < len(dts_utc):
        raise ValueError(
            f"JPL Horizons returned {len(az_list)} rows, expected {len(dts_utc)}"
        )

    n = len(dts_utc)
    return (
        np.asarray(az_list[:n], dtype=np.float64),
        np.asarray(el_list[:n], dtype=np.float64),
        np.asarray(dist_list[:n], dtype=np.float64) if dist_list else None,
    )


def _compute_ephemeris_horizons(
    lat: float,
    lon: float,
    elevation_m: float,
    dts_utc: list[datetime],
    step_minutes: int,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    sun_az, sun_el, sun_dist = _query_horizons_body(
        "10", lat, lon, elevation_m, dts_utc, step_minutes, "4,20"
    )
    earth_az, earth_el, _ = _query_horizons_body(
        "399", lat, lon, elevation_m, dts_utc, step_minutes, "4"
    )
    if sun_dist is None:
        sun_dist = np.ones(len(dts_utc), dtype=np.float64)
    return sun_az, sun_el, sun_dist, earth_az, earth_el


def _subbody_to_topocentric(
    lat_deg: float,
    lon_deg: float,
    body_lat_deg: np.ndarray,
    body_lon_deg: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    phi0 = math.radians(lat_deg)
    lam0 = math.radians(lon_deg)

    ux = math.cos(phi0) * math.cos(lam0)
    uy = math.cos(phi0) * math.sin(lam0)
    uz = math.sin(phi0)

    ex = -math.sin(lam0)
    ey = math.cos(lam0)
    ez = 0.0

    nx = -math.sin(phi0) * math.cos(lam0)
    ny = -math.sin(phi0) * math.sin(lam0)
    nz = math.cos(phi0)

    blat = np.radians(body_lat_deg)
    blon = np.radians(body_lon_deg)
    bx = np.cos(blat) * np.cos(blon)
    by = np.cos(blat) * np.sin(blon)
    bz = np.sin(blat)

    east = bx * ex + by * ey + bz * ez
    north = bx * nx + by * ny + bz * nz
    up = np.clip(bx * ux + by * uy + bz * uz, -1.0, 1.0)

    az_deg = np.mod(np.degrees(np.arctan2(east, north)), 360.0)
    el_deg = np.degrees(np.arcsin(up))
    return az_deg, el_deg


def _compute_ephemeris_analytical(
    lat: float,
    lon: float,
    dts_utc: list[datetime],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Offline analytical lunar ephemeris fallback when SPICE kernels and Horizons are unavailable."""
    j2000 = datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    d = np.array([(dt - j2000).total_seconds() / 86400.0 for dt in dts_utc], dtype=np.float64)

    # Solar ecliptic mean longitude & anomaly
    g_sun = np.radians((357.529 + 0.98560028 * d) % 360.0)
    l_sun = (280.459 + 0.98564736 * d + 1.915 * np.sin(g_sun) + 0.020 * np.sin(2.0 * g_sun)) % 360.0
    sun_dist_au = 1.00014 - 0.01671 * np.cos(g_sun) - 0.00014 * np.cos(2.0 * g_sun)

    # Lunar orbital mean longitude, mean anomaly, argument of latitude, ascending node
    l_moon = (218.316 + 13.176396 * d) % 360.0
    m_moon = np.radians((134.963 + 13.064993 * d) % 360.0)
    f_moon = np.radians((93.272 + 13.229350 * d) % 360.0)
    omega_moon = (125.045 - 0.052954 * d) % 360.0

    # Moon true ecliptic longitude and latitude (dominant terms)
    lam_moon = l_moon + 6.289 * np.sin(m_moon)
    beta_moon = 5.128 * np.sin(f_moon)

    # Sub-solar selenographic coords (Moon axial tilt ~1.5424 deg to ecliptic)
    subsun_lat = 1.5424 * np.sin(np.radians(l_sun - omega_moon))
    subsun_lon = (l_sun - lam_moon + 180.0) % 360.0

    # Sub-Earth optical libration + lunar radius parallax (~0.26 deg)
    subearth_lat = -beta_moon - 1.5424 * np.sin(f_moon)
    subearth_lon = -(lam_moon - l_moon)

    sun_az, sun_el = _subbody_to_topocentric(lat, lon, subsun_lat, subsun_lon)
    earth_az, earth_el = _subbody_to_topocentric(lat, lon, subearth_lat, subearth_lon)

    earth_dist_m = 384_400_000.0
    parallax_deg = math.degrees(MOON_RADIUS_M / earth_dist_m)
    earth_el = earth_el - parallax_deg * np.cos(np.radians(earth_el))

    return sun_az, sun_el, sun_dist_au, earth_az, earth_el


def _interpolate_horizon(horizon_profile: list[dict[str, Any]], az_deg: np.ndarray) -> np.ndarray:
    if not horizon_profile:
        raise ValueError("horizon_profile must not be empty")

    pairs = sorted(
        ((float(pt["az_deg"]) % 360.0, float(pt["horizon_el_deg"])) for pt in horizon_profile),
        key=lambda item: item[0],
    )
    az_pts = np.array([p[0] for p in pairs], dtype=np.float64)
    el_pts = np.array([p[1] for p in pairs], dtype=np.float64)

    # Deduplicate any repeated azimuth entries
    unique_az, unique_idx = np.unique(az_pts, return_index=True)
    unique_el = el_pts[unique_idx]

    az_wrapped = np.concatenate(([unique_az[-1] - 360.0], unique_az, [unique_az[0] + 360.0]))
    el_wrapped = np.concatenate(([unique_el[-1]], unique_el, [unique_el[0]]))
    return np.interp(np.mod(az_deg, 360.0), az_wrapped, el_wrapped)


def _max_continuous_hours(flags: list[bool], step_hours: float) -> float:
    best = 0
    cur = 0
    for flag in flags:
        if flag:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return float(best * step_hours)


def _find_next_set(
    timestamps: list[str],
    flags: list[bool],
    step_hours: float,
) -> tuple[str | None, float | None]:
    for idx in range(1, len(flags)):
        if flags[idx - 1] and not flags[idx]:
            return timestamps[idx], float(idx * step_hours)
    return None, None


def _compute_status_and_reasons(
    slope_deg: float,
    sun_visible_pct: float,
    earth_visible_pct: float,
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    nogo = False
    caution = False

    if slope_deg > 15.0:
        nogo = True
        reasons.append(f"Pendiente crítica ({slope_deg:.1f}° > 15.0° límite máximo de alunizaje)")
    elif slope_deg > 10.0:
        caution = True
        reasons.append(f"Pendiente moderada ({slope_deg:.1f}° entre 10° y 15°, requiere control de descenso)")
    else:
        reasons.append(f"Pendiente segura ({slope_deg:.1f}° <= 10.0°)")

    if sun_visible_pct < 20.0:
        nogo = True
        reasons.append(f"Iluminación solar crítica ({sun_visible_pct:.1f}% < 20% en la ventana)")
    elif sun_visible_pct < 50.0:
        caution = True
        reasons.append(f"Iluminación solar limitada ({sun_visible_pct:.1f}%) en la ventana seleccionada")
    else:
        reasons.append(f"Iluminación solar alta ({sun_visible_pct:.1f}%) en la ventana seleccionada")

    if earth_visible_pct <= 0.0:
        caution = True
        reasons.append("Sin línea de vista Direct-to-Earth (0.0%); requiere satélite relay")
    elif earth_visible_pct < 15.0:
        caution = True
        reasons.append(f"Ventana Direct-to-Earth reducida ({earth_visible_pct:.1f}% < 15%); considerar relay")
    else:
        reasons.append(f"Comunicación Direct-to-Earth disponible ({earth_visible_pct:.1f}%)")

    if nogo:
        status = "NO-GO"
    elif caution:
        status = "CAUTION"
    else:
        status = "GO"

    return status, reasons


def evaluate_site_mission(
    lat: float,
    lon: float,
    elevation_m: float,
    slope_deg: float,
    horizon_profile: list[dict],
    start_utc: str,
    duration_days: float = 14.0,
    step_minutes: int = 60,
    panel_efficiency: float = 0.28,
    panel_area_m2: float = 4.0,
) -> dict[str, Any]:
    """Compute Sun/Earth ephemerides, intersect with local 360-deg horizon, and evaluate mission status."""
    lat_c, lon_norm = validate_lat_lon(lat, lon)
    elev_f = float(elevation_m)
    slope_f = float(slope_deg)
    dur_days = float(duration_days)
    step_min = int(step_minutes)
    eff = float(panel_efficiency)
    area = float(panel_area_m2)

    if not math.isfinite(elev_f):
        raise ValueError(f"elevation_m must be finite, got {elevation_m!r}")
    if not math.isfinite(slope_f) or slope_f < 0.0:
        raise ValueError(f"slope_deg must be non-negative and finite, got {slope_deg!r}")
    if not math.isfinite(dur_days) or dur_days <= 0.0:
        raise ValueError(f"duration_days must be positive, got {duration_days!r}")
    if step_min < 1:
        raise ValueError(f"step_minutes must be >= 1, got {step_minutes!r}")
    if not math.isfinite(eff) or eff <= 0.0 or eff > 1.0:
        raise ValueError(f"panel_efficiency must be in (0, 1], got {panel_efficiency!r}")
    if not math.isfinite(area) or area <= 0.0:
        raise ValueError(f"panel_area_m2 must be positive, got {panel_area_m2!r}")

    start_dt = _parse_utc_datetime(start_utc)
    n_steps = max(1, int(round(dur_days * 24.0 * 60.0 / float(step_min))))
    dts_utc = [start_dt + timedelta(minutes=i * step_min) for i in range(n_steps)]
    timestamps = [_format_utc_iso(dt) for dt in dts_utc]

    sun_az: np.ndarray
    sun_el: np.ndarray
    sun_dist_au: np.ndarray
    earth_az: np.ndarray
    earth_el: np.ndarray

    bundle = _get_spice_bundle()
    ephem_computed = False
    if bundle is not None:
        try:
            sun_az, sun_el, sun_dist_au, earth_az, earth_el = _compute_ephemeris_skyfield(
                bundle, lat_c, lon_norm, elev_f, dts_utc
            )
            ephem_computed = True
        except Exception as exc:
            logger.warning(
                "Skyfield SPICE evaluation failed (%s); falling back to JPL Horizons API",
                exc,
            )

    if not ephem_computed:
        logger.warning(
            "Local SPICE kernels unavailable or out of coverage; querying JPL Horizons API fallback"
        )
        try:
            sun_az, sun_el, sun_dist_au, earth_az, earth_el = _compute_ephemeris_horizons(
                lat_c, lon_norm, elev_f, dts_utc, step_min
            )
            ephem_computed = True
        except Exception as exc:
            logger.warning(
                "JPL Horizons API fallback failed (%s); using offline analytical lunar ephemeris fallback",
                exc,
            )

    if not ephem_computed:
        sun_az, sun_el, sun_dist_au, earth_az, earth_el = _compute_ephemeris_analytical(
            lat_c, lon_norm, dts_utc
        )

    sun_horizon_el = _interpolate_horizon(horizon_profile, sun_az)
    earth_horizon_el = _interpolate_horizon(horizon_profile, earth_az)

    sun_vis_arr = sun_el > sun_horizon_el
    earth_vis_arr = earth_el > earth_horizon_el

    timeline: list[dict[str, Any]] = []
    sun_flags: list[bool] = []
    earth_flags: list[bool] = []
    power_factors: list[float] = []
    peak_power_w = 0.0

    for i in range(n_steps):
        s_az = float(sun_az[i])
        s_el = float(sun_el[i])
        s_hor = float(sun_horizon_el[i])
        s_vis = bool(sun_vis_arr[i])

        e_az = float(earth_az[i])
        e_el = float(earth_el[i])
        e_hor = float(earth_horizon_el[i])
        e_vis = bool(earth_vis_arr[i])

        if s_vis:
            clearance = s_el - s_hor
            disk_frac = min(1.0, 0.5 + 0.5 * (clearance / SOLAR_ANGULAR_RADIUS_DEG))
            # Vertical azimuth-tracking lunar South Pole array (VSAT) incidence factor
            cos_inc = math.cos(math.radians(max(0.0, min(90.0, s_el))))
            pf = max(0.0, min(1.0, disk_frac * cos_inc))
        else:
            pf = 0.0

        r_au = max(0.5, float(sun_dist_au[i]))
        irradiance = SOLAR_CONSTANT_W_M2 / (r_au * r_au)
        inst_power_w = irradiance * eff * area * pf
        if inst_power_w > peak_power_w:
            peak_power_w = inst_power_w

        sun_flags.append(s_vis)
        earth_flags.append(e_vis)
        power_factors.append(float(pf))

        timeline.append(
            {
                "timestamp_utc": timestamps[i],
                "sun_az_deg": s_az,
                "sun_el_deg": s_el,
                "sun_horizon_el_deg": s_hor,
                "sun_visible": s_vis,
                "sun_vector_3d": az_el_to_vector_3d(s_az, s_el),
                "earth_az_deg": e_az,
                "earth_el_deg": e_el,
                "earth_horizon_el_deg": e_hor,
                "earth_visible": e_vis,
                "earth_vector_3d": az_el_to_vector_3d(e_az, e_el),
                "power_factor": float(pf),
            }
        )

    step_hours = float(step_min) / 60.0
    sun_visible_pct = 100.0 * sum(1 for v in sun_flags if v) / float(n_steps)
    earth_visible_pct = 100.0 * sum(1 for v in earth_flags if v) / float(n_steps)
    avg_power_factor = sum(power_factors) / float(n_steps)

    next_sunset_utc, hours_to_next_sunset = _find_next_set(timestamps, sun_flags, step_hours)
    next_earthset_utc, hours_to_next_earthset = _find_next_set(timestamps, earth_flags, step_hours)

    status, reasons = _compute_status_and_reasons(
        slope_f, sun_visible_pct, earth_visible_pct
    )

    return {
        "status": status,
        "reasons": reasons,
        "summary": {
            "sun_visible_pct": round(float(sun_visible_pct), 2),
            "earth_visible_pct": round(float(earth_visible_pct), 2),
            "max_continuous_sun_hours": round(_max_continuous_hours(sun_flags, step_hours), 2),
            "max_continuous_dte_hours": round(_max_continuous_hours(earth_flags, step_hours), 2),
            "next_sunset_utc": next_sunset_utc,
            "hours_to_next_sunset": round(hours_to_next_sunset, 2) if hours_to_next_sunset is not None else None,
            "next_earthset_utc": next_earthset_utc,
            "hours_to_next_earthset": round(hours_to_next_earthset, 2) if hours_to_next_earthset is not None else None,
            "avg_power_factor": round(float(avg_power_factor), 4),
            "estimated_peak_power_w": round(float(peak_power_w), 2),
        },
        "timeline": timeline,
    }


def _close_spice_cache() -> None:
    with _SPICE_LOCK:
        for key in list(_SPICE_CACHE.keys()):
            bundle = _SPICE_CACHE.pop(key)
            try:
                bundle.bpc_handle.close()
            except Exception:
                pass
            try:
                bundle.eph.close()
            except Exception:
                pass


atexit.register(_close_spice_cache)
