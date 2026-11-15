"""Shared router helpers for engine loading, coordinate normalization, and caching."""

from __future__ import annotations

import importlib
import logging
from typing import Any

from fastapi import HTTPException

from app import db
from app.models import normalize_lon

logger = logging.getLogger(__name__)

REQUIRED_ENGINE_SYMBOLS = (
    "polar_stereographic_from_latlon",
    "latlon_from_polar_stereographic",
    "sample_point_terrain",
    "extract_terrain_patch",
    "compute_horizon_profile",
    "evaluate_site_mission",
)


def check_engine_availability() -> dict[str, Any]:
    """Inspect whether app.engine is importable and exposes the full contract."""
    try:
        engine = importlib.import_module("app.engine")
    except ImportError as exc:
        return {
            "available": False,
            "missing_symbols": list(REQUIRED_ENGINE_SYMBOLS),
            "error": str(exc),
        }

    missing = [name for name in REQUIRED_ENGINE_SYMBOLS if not hasattr(engine, name)]
    return {
        "available": len(missing) == 0,
        "missing_symbols": missing,
        "error": f"Missing symbols: {', '.join(missing)}" if missing else None,
    }


def get_engine() -> Any:
    """Safely import app.engine and verify required contract symbols, or raise HTTP 503."""
    try:
        engine = importlib.import_module("app.engine")
    except ImportError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Physics engine (app.engine) is unavailable: {exc}",
        ) from exc

    missing = [name for name in REQUIRED_ENGINE_SYMBOLS if not hasattr(engine, name)]
    if missing:
        raise HTTPException(
            status_code=503,
            detail=f"Physics engine is missing contract symbols: {', '.join(missing)}",
        )
    return engine


def try_get_engine() -> Any | None:
    """Best-effort import of app.engine without raising HTTP errors."""
    try:
        return get_engine()
    except HTTPException:
        return None


def safe_normalize_lon(lon: float) -> float:
    """Normalize longitude and convert validation failures into HTTP 422."""
    try:
        return normalize_lon(lon)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def make_horizon_coord_key(lat: float, lon: float) -> str:
    """Build horizon_profiles cache key using round(lat, 4), round(lon, 4)."""
    return f"{round(lat, 4):.4f},{round(lon, 4):.4f}"


def make_ephemeris_cache_key(
    lat: float,
    lon: float,
    start_utc: str,
    duration_days: float,
    step_minutes: int,
    panel_efficiency: float,
    panel_area_m2: float,
) -> str:
    """Build deterministic ephemeris_cache key covering all mission inputs.

    The version prefix changes whenever the cached payload or its inputs change
    (v2: `checks`; v3: rise times in the summary, and results computed with the
    20 m polar / 118 m global DEMs instead of the coarser ones cached under v2).
    """
    return (
        f"v3|{round(lat, 4):.4f}|{round(lon, 4):.4f}|{start_utc}|"
        f"{duration_days:g}|{step_minutes}|{panel_efficiency:g}|{panel_area_m2:g}"
    )


def safe_nearest_psr_km(polar_x_m: float, polar_y_m: float) -> float | None:
    """Look up nearest PSR distance (km) from PostGIS, returning None if DB is unavailable."""
    try:
        return db.get_nearest_psr_km(polar_x_m, polar_y_m)
    except Exception as exc:
        logger.warning("Failed to query nearest_psr_km from PostGIS: %s", exc)
        return None


def run_site_evaluation(
    *,
    lat: float,
    lon: float,
    start_utc: str,
    duration_days: float,
    step_minutes: int,
    panel_efficiency: float,
    panel_area_m2: float,
    include_details: bool = True,
) -> dict[str, Any]:
    """Execute terrain sampling, cached horizon lookup, and mission evaluation."""
    engine = get_engine()

    try:
        terrain = engine.sample_point_terrain(lat, lon)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    use_persistent_cache = terrain.get("source", "lola") != "fallback"
    nearest_psr_km = safe_nearest_psr_km(terrain["polar_x_m"], terrain["polar_y_m"])

    coord_key = make_horizon_coord_key(lat, lon)
    horizon_profile: list[dict[str, Any]] | None = None
    if use_persistent_cache:
        try:
            horizon_profile = db.get_horizon_profile(coord_key)
        except Exception as exc:
            logger.warning("Failed reading horizon_profiles cache: %s", exc)

    if horizon_profile is None:
        try:
            horizon_profile = engine.compute_horizon_profile(lat, lon)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        if use_persistent_cache:
            try:
                db.save_horizon_profile(coord_key, round(lat, 4), round(lon, 4), horizon_profile)
            except Exception as exc:
                logger.warning("Failed writing horizon_profiles cache: %s", exc)

    cache_key = make_ephemeris_cache_key(
        lat=lat,
        lon=lon,
        start_utc=start_utc,
        duration_days=duration_days,
        step_minutes=step_minutes,
        panel_efficiency=panel_efficiency,
        panel_area_m2=panel_area_m2,
    )
    mission: dict[str, Any] | None = None
    if use_persistent_cache:
        try:
            mission = db.get_ephemeris_cache(cache_key)
        except Exception as exc:
            logger.warning("Failed reading ephemeris_cache: %s", exc)

    if mission is None:
        try:
            mission = engine.evaluate_site_mission(
                lat=lat,
                lon=lon,
                elevation_m=terrain["elevation_m"],
                slope_deg=terrain["slope_deg"],
                horizon_profile=horizon_profile,
                start_utc=start_utc,
                duration_days=duration_days,
                step_minutes=step_minutes,
                panel_efficiency=panel_efficiency,
                panel_area_m2=panel_area_m2,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        if use_persistent_cache:
            try:
                db.save_ephemeris_cache(cache_key, mission)
            except Exception as exc:
                logger.warning("Failed writing ephemeris_cache: %s", exc)

    site_payload = {
        "lat": lat,
        "lon": lon,
        "elevation_m": terrain["elevation_m"],
        "slope_deg": terrain["slope_deg"],
        "nearest_psr_km": nearest_psr_km,
        "status": mission["status"],
        "reasons": mission["reasons"],
        "checks": mission.get("checks", []),
    }

    if not include_details:
        return {
            "site": site_payload,
            "summary": mission["summary"],
        }

    return {
        "site": site_payload,
        "summary": mission["summary"],
        "horizon_profile": horizon_profile,
        "timeline": mission["timeline"],
    }
