"""3D terrain patch extraction router for Three.js visualization."""

from __future__ import annotations

import gzip
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

_TILE_HEADERS_DOC = (
    "Headers: X-Terrain-Source names the DEM used; X-Terrain-Resolution-M is the spacing in metres "
    "of the DEM data actually read (native pixel times the decimation factor, the coarsest one when "
    "the tile mixes sources)."
)
_TILE_ERRORS = {
    304: {"description": "Not modified (If-None-Match matches the tile ETag)"},
    422: {"description": "z, row, col or a parameter outside its range"},
    503: {"description": "The LOLA DEMs on the server do not cover the tile"},
}


def _z(max_level: int):
    return Path(..., ge=0, le=max_level, description=f"Zoom level (0 to {max_level}). Level 0 has 2 columns x 1 row")


_ROW = Path(..., ge=0, description="Tile row, 0 = north edge (0 to 2^z - 1)")
_COL = Path(..., ge=0, description="Tile column, 0 starts at -180 deg (0 to 2^(z+1) - 1)")


def _tile_response(request: Request, render, gzip_body: bool = False) -> Response:
    """Render a tile through the engine and answer with caching headers (or 304)."""
    engine = get_engine()
    try:
        tile = render(engine)
    except engine.TileOutOfRangeError as exc:
        raise HTTPException(status_code=422, detail=f"ERR_INVALID_PAYLOAD: Invalid tile request: {exc}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=f"Terrain DEM unavailable: {exc}") from exc

    body, etag = tile.body, tile.etag
    headers = {
        "Cache-Control": TILE_CACHE_CONTROL,
        "X-Terrain-Source": tile.source,
        "X-Terrain-Resolution-M": f"{round(tile.resolution_m, 1):g}",
    }
    if gzip_body:
        headers["Vary"] = "Accept-Encoding"
        if "gzip" in request.headers.get("accept-encoding", "").lower():
            body = tile.gzip_body if tile.gzip_body is not None else gzip.compress(body, compresslevel=6, mtime=0)
            headers["Content-Encoding"] = "gzip"
            etag = etag[:-1] + '-gz"'  # each encoding is a different representation
    headers["ETag"] = etag
    if etag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
        headers.pop("Content-Encoding", None)
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type=tile.media_type, headers=headers)


@router.get(
    "/terrain/tiles/{z}/{row}/{col}.png",
    response_class=Response,
    responses={
        200: {
            "content": {"image/png": {}},
            "description": (
                "256x256 Terrarium PNG (samples at pixel centers): elevation_m = R*256 + G + B/256 - 32768, "
                "relative to the 1,737,400 m lunar sphere. " + _TILE_HEADERS_DOC
            ),
        },
        **_TILE_ERRORS,
    },
)
def get_terrain_tile(
    request: Request,
    z: int = _z(10),
    row: int = _ROW,
    col: int = _COL,
) -> Response:
    """Lunar elevation tile for the globe in the Moon Trek / Cesium geographic scheme (handoff P-008).

    Covers the whole Moon so the globe can use a single terrain source: the LOLA south polar
    DEM (20/40/80 m, best fit for the zoom level) where it has data, the global LOLA DEM elsewhere.
    Level 10 (~21 m/px) reads the 20 m polar DEM.
    """
    return _tile_response(request, lambda engine: engine.render_tile("terrarium", z, row, col))


@router.get(
    "/terrain/tiles/heightmap/{z}/{row}/{col}.bin",
    response_class=Response,
    responses={
        200: {
            "content": {"application/octet-stream": {}},
            "description": (
                "65x65 little-endian float32 elevations in metres (16,900 bytes before gzip), row 0 = north "
                "edge, samples ON the tile edges and corners, quantized to 1/256 m. Relative to the "
                "1,737,400 m lunar sphere. Sent with Content-Encoding: gzip when the client accepts it. "
                + _TILE_HEADERS_DOC
            ),
        },
        **_TILE_ERRORS,
    },
)
def get_terrain_heightmap(
    request: Request,
    z: int = _z(12),
    row: int = _ROW,
    col: int = _COL,
) -> Response:
    """Terrain mesh heights for the globe, ready for Cesium's heightmap terrain (contract v1.5).

    Same tiling scheme and DEM sources as /terrain/tiles, but on a 65 x 65 vertex grid that
    includes the tile edges: neighbouring tiles share bit-identical edge values, so the globe
    passes the array to Cesium without resampling. Level 12 (~21 m between samples) reads the
    20 m polar DEM.
    """
    return _tile_response(request, lambda engine: engine.render_tile("heightmap", z, row, col), gzip_body=True)


@router.get(
    "/terrain/tiles/hillshade/{z}/{row}/{col}.png",
    response_class=Response,
    responses={
        200: {
            "content": {"image/png": {}},
            "description": (
                "256x256 8-bit gray PNG (pixel centers): gray = round(255 * cos(incidence)) of a distant "
                "light on the terrain, without cast shadows. Gray + alpha when fade_start_lat and "
                "fade_end_lat are given. " + _TILE_HEADERS_DOC
            ),
        },
        **_TILE_ERRORS,
    },
)
def get_terrain_hillshade(
    request: Request,
    z: int = _z(10),
    row: int = _ROW,
    col: int = _COL,
    light_lon: float = Query(
        135.0, ge=-180.0, le=360.0, description="East longitude where the light is overhead (degrees)"
    ),
    light_lat: float = Query(-22.0, ge=-90.0, le=90.0, description="Latitude where the light is overhead (degrees)"),
    exaggeration: float = Query(1.0, ge=0.1, le=20.0, description="Vertical exaggeration applied to the slopes"),
    fade_start_lat: float | None = Query(
        None, ge=-90.0, le=90.0, description="Latitude where the opacity is 0 (with fade_end_lat)"
    ),
    fade_end_lat: float | None = Query(
        None, ge=-90.0, le=90.0, description="Latitude where the opacity reaches 1 (with fade_start_lat)"
    ),
) -> Response:
    """Relief shading of the whole Moon, ready to paint as an imagery layer (contract v1.5).

    Lambert shading with a fixed, illustrative light: the light comes from the direction of
    (light_lat, light_lon) in the Moon body-fixed frame (defaults: the globe's light). Slopes
    are central differences on the DEM's own grid (polar stereographic with its scale factor,
    or latitude/longitude), read from the raster around each pixel, so tiles have no seams.
    Parameters are rounded to 0.01 for caching.
    """
    return _tile_response(
        request,
        lambda engine: engine.render_tile(
            "hillshade",
            z,
            row,
            col,
            light_lon=light_lon,
            light_lat=light_lat,
            exaggeration=exaggeration,
            fade_start_lat=fade_start_lat,
            fade_end_lat=fade_end_lat,
        ),
    )
