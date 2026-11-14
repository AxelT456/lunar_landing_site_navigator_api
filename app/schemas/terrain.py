"""Terrain patch schemas."""

from enum import IntEnum
from typing import List
from pydantic import Field

from app.core.constants import (
    DEFAULT_RESOLUTION,
    DEFAULT_SIZE_KM,
    MAX_SIZE_KM,
    MIN_SIZE_KM,
)
from app.schemas.common import BaseSchema, LunarLat, LunarLon


class Resolution(IntEnum):
    """Permitted grid resolutions (vertices per side)."""

    LOW = 64
    MEDIUM = 128
    HIGH = 256


class TerrainCenter(BaseSchema):
    """Terrain patch center point coordinates and topographical metrics."""

    lat: LunarLat
    lon: LunarLon
    polar_x_m: float = Field(..., description="Center X in polar stereographic projection (meters)")
    polar_y_m: float = Field(..., description="Center Y in polar stereographic projection (meters)")
    elevation_m: float = Field(..., description="Surface elevation at center point (meters)")
    slope_deg: float = Field(..., description="Surface slope at center point (degrees)")


class TerrainGrid(BaseSchema):
    """Elevation and slope grid arrays for 3D terrain rendering."""

    size_km: float = Field(
        default=DEFAULT_SIZE_KM,
        ge=MIN_SIZE_KM,
        le=MAX_SIZE_KM,
        description="Physical dimension of the terrain patch (km)",
    )
    resolution: Resolution = Field(
        default=Resolution.MEDIUM,
        description="Grid resolution (vertices per side: 64, 128, or 256)",
    )
    cell_size_m: float = Field(..., description="Spatial cell spacing between vertices (meters)")
    min_elevation_m: float = Field(..., description="Minimum elevation value in patch (meters)")
    max_elevation_m: float = Field(..., description="Maximum elevation value in patch (meters)")
    elevations_m: List[float] = Field(
        ...,
        description="Flat 1D array of elevation values (length = resolution^2, row-major North-to-South, West-to-East)",
    )
    slopes_deg: List[float] = Field(
        ...,
        description="Flat 1D array of slope values (length = resolution^2, row-major North-to-South, West-to-East)",
    )


class NearbyPSR(BaseSchema):
    """Summary of nearby permanently shadowed region."""

    gid: int = Field(..., description="Identifier of the PSR feature")
    name: str = Field(..., description="Name of the crater or region")
    area_km2: float = Field(..., description="Total area of the PSR (square kilometers)")
    distance_km: float = Field(..., description="Distance from the center point (kilometers)")


class TerrainPatchResponse(BaseSchema):
    """Response containing 3D terrain patch elevation mesh and nearby features."""

    center: TerrainCenter
    grid: TerrainGrid
    nearby_psrs: List[NearbyPSR] = Field(default_factory=list)
