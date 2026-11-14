"""Landing sites catalog schemas."""

from typing import List
from pydantic import Field

from app.schemas.common import BaseSchema, LunarLat, LunarLon


class Site(BaseSchema):
    """Candidate lunar landing site."""

    id: str = Field(..., description="Unique landing site identifier")
    name: str = Field(..., description="Display name of the site")
    category: str = Field(..., description="Mission or exploration program category (e.g., Artemis III, CLPS)")
    lat: LunarLat
    lon: LunarLon
    polar_x_m: float = Field(..., description="X coordinate in lunar polar stereographic projection (meters)")
    polar_y_m: float = Field(..., description="Y coordinate in lunar polar stereographic projection (meters)")
    elevation_m: float = Field(..., description="Site elevation relative to lunar reference radius (meters)")
    slope_deg: float = Field(..., description="Local terrain slope in degrees")
    nearest_psr_km: float = Field(..., description="Distance to nearest permanently shadowed region in kilometers")
    description: str = Field(..., description="Detailed description of the site")


class SitesResponse(BaseSchema):
    """List response of candidate landing sites."""

    count: int = Field(..., description="Total number of sites returned")
    sites: List[Site] = Field(..., description="List of candidate landing sites")
