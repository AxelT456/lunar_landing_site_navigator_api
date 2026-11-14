"""3D terrain patch extraction router for Three.js visualization."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Path, Query, Request, Response

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


# Same policy as the other reference GETs (app.main): an hour, then a cheap revalidation
# with If-None-Match. The ETag changes when a DEM file on the server is replaced.
TILE_CACHE_CONTROL = "public, max-age=3600"


@router.get(
    "/terrain/tiles/{z}/{row}/{col}.png",
    response_class=Response,
    responses={
        200: {
            "content": {"image/png": {}},
            "description": (
                "256x256 Terrarium PNG: elevation_m = R*256 + G + B/256 - 32768, relative to the "
                "1,737,400 m lunar sphere. Header X-Terrain-Source names the DEM used."
            ),
        },
        304: {"description": "Not modified (If-None-Match matches the tile ETag)"},
        422: {"description": "z, row or col outside the tiling scheme"},
        503: {"description": "The LOLA DEMs on the server do not cover the tile"},
    },
)
def get_terrain_tile(
    request: Request,
    z: int = Path(..., ge=0, description="Zoom level (0 to 9). Level 0 has 2 columns x 1 row"),
    row: int = Path(..., ge=0, description="Tile row, 0 = north edge (0 to 2^z - 1)"),
    col: int = Path(..., ge=0, description="Tile column, 0 starts at -180 deg (0 to 2^(z+1) - 1)"),
) -> Response:
    """Lunar elevation tile for the globe in the Moon Trek / Cesium geographic scheme (handoff P-008).

    Covers the whole Moon so the globe can use a single terrain source: the LOLA south polar
    DEM (20/40/80 m, best fit for the zoom level) where it has data, the global LOLA DEM elsewhere.
    """
    engine = get_engine()
    try:
        png, source, etag = engine.render_terrain_tile(z, row, col)
    except engine.TileOutOfRangeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid tile index: {exc}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=f"Terrain DEM unavailable: {exc}") from exc

    headers = {"Cache-Control": TILE_CACHE_CONTROL, "ETag": etag, "X-Terrain-Source": source}
    if etag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    return Response(content=png, media_type="image/png", headers=headers)
