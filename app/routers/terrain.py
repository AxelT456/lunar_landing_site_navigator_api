"""3D terrain patch extraction router for Three.js visualization."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query

from app import db
from app.models import NearbyPsrSummary, TerrainPatchResponse
from app.routers._common import get_engine, safe_normalize_lon

logger = logging.getLogger(__name__)

router = APIRouter(tags=["terrain"])

ALLOWED_RESOLUTIONS = (64, 128, 256)


@router.get("/terrain/patch", response_model=TerrainPatchResponse)
def get_terrain_patch(
    lat: float = Query(..., ge=-90.0, le=90.0, description="Planetocentric latitude in degrees"),
    lon: float = Query(..., ge=-180.0, le=360.0, description="East longitude in degrees (0..360 or -180..180)"),
    size_km: float = Query(12.0, ge=2.0, le=40.0, description="Patch side length in kilometers"),
    resolution: int = Query(128, ge=64, le=256, description="Grid side resolution (64, 128, or 256)"),
) -> TerrainPatchResponse:
    """Extract an N x N LOLA terrain patch centered on (lat, lon) with nearby PSR metadata."""
    if resolution not in ALLOWED_RESOLUTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"resolution must be one of {ALLOWED_RESOLUTIONS}",
        )
    norm_lon = safe_normalize_lon(lon)
    engine = get_engine()

    try:
        patch = engine.extract_terrain_patch(
            lat=lat,
            lon=norm_lon,
            size_km=size_km,
            resolution=resolution,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    center = patch["center"]
    search_radius_km = max(size_km * 1.5, 25.0)
    nearby_psrs: list[NearbyPsrSummary] = []
    try:
        psr_rows = db.get_nearby_psrs(
            polar_x_m=center["polar_x_m"],
            polar_y_m=center["polar_y_m"],
            radius_km=search_radius_km,
            limit=25,
            include_geometry=False,
        )
        nearby_psrs = [NearbyPsrSummary(**row) for row in psr_rows]
    except Exception as exc:
        logger.warning("Failed fetching nearby PSRs for terrain patch: %s", exc)

    return TerrainPatchResponse(
        center=center,
        grid=patch["grid"],
        nearby_psrs=nearby_psrs,
    )
