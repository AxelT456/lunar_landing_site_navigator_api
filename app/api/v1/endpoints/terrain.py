"""Terrain patch endpoint."""

from fastapi import APIRouter, Depends, Query

from app.core.constants import (
    DEFAULT_SIZE_KM,
    MAX_SIZE_KM,
    MIN_SIZE_KM,
    SOUTH_POLE_MAX_LAT,
    SOUTH_POLE_MIN_LAT,
)
from app.domain.services.terrain import TerrainService
from app.schemas.terrain import Resolution, TerrainPatchResponse

router = APIRouter()


def get_terrain_service() -> TerrainService:
    """Dependency provider for TerrainService."""
    return TerrainService()


@router.get(
    "/terrain/patch",
    response_model=TerrainPatchResponse,
    summary="Get Terrain Elevation Patch",
    description="Retrieves a 3D elevation and slope grid around the specified lunar coordinate.",
)
async def get_terrain_patch(
    lat: float = Query(
        ...,
        ge=SOUTH_POLE_MIN_LAT,
        le=SOUTH_POLE_MAX_LAT,
        description=f"Patch center latitude ({SOUTH_POLE_MIN_LAT} to {SOUTH_POLE_MAX_LAT})",
    ),
    lon: float = Query(
        ...,
        ge=-180.0,
        le=360.0,
        description="Patch center longitude (-180.0 to 360.0)",
    ),
    size_km: float = Query(
        default=DEFAULT_SIZE_KM,
        ge=MIN_SIZE_KM,
        le=MAX_SIZE_KM,
        description=f"Patch dimension in km ({MIN_SIZE_KM} to {MAX_SIZE_KM})",
    ),
    resolution: Resolution = Query(
        default=Resolution.MEDIUM,
        description="Grid vertices per side (64, 128, or 256)",
    ),
    terrain_service: TerrainService = Depends(get_terrain_service),
) -> TerrainPatchResponse:
    """Generate or retrieve terrain patch elevation grid."""
    return terrain_service.generate_patch(
        lat=lat,
        lon=lon,
        size_km=size_km,
        resolution=resolution.value,
    )
