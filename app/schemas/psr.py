"""Permanently Shadowed Regions (PSR) schemas."""

from typing import Any, Dict, List, Literal, Union
from pydantic import Field

from app.schemas.common import BaseSchema, LunarLat, LunarLon


class GeoJSONPolygon(BaseSchema):
    """GeoJSON Polygon representation in polar coordinates."""

    type: Literal["Polygon"] = "Polygon"
    coordinates: List[List[List[float]]] = Field(
        ...,
        description="List of linear rings, each ring being a list of [polar_x_m, polar_y_m] points",
    )


class GeoJSONMultiPolygon(BaseSchema):
    """GeoJSON MultiPolygon representation in polar coordinates."""

    type: Literal["MultiPolygon"] = "MultiPolygon"
    coordinates: List[List[List[List[float]]]] = Field(
        ...,
        description="List of polygons, each having linear rings of [polar_x_m, polar_y_m] coordinates",
    )


GeoJSONGeometry = Union[GeoJSONMultiPolygon, GeoJSONPolygon, Dict[str, Any]]


class PSRCenter(BaseSchema):
    """Search center point in geographic and polar coordinates."""

    lat: LunarLat
    lon: LunarLon
    polar_x_m: float = Field(..., description="Center X in polar stereographic projection (meters)")
    polar_y_m: float = Field(..., description="Center Y in polar stereographic projection (meters)")


class PSRItem(BaseSchema):
    """Permanently shadowed region feature with polar geometry."""

    gid: int = Field(..., description="Unique PSR region identifier")
    name: str = Field(..., description="Name of the crater or feature")
    area_km2: float = Field(..., description="Total surface area in square kilometers")
    distance_km: float = Field(..., description="Distance from the search center in kilometers")
    geometry: GeoJSONGeometry = Field(..., description="Polar stereographic GeoJSON geometry")


class PSRNearbyResponse(BaseSchema):
    """Response containing nearby permanently shadowed regions."""

    center: PSRCenter
    radius_km: float = Field(..., description="Search radius in kilometers")
    count: int = Field(..., description="Number of PSR features returned")
    psrs: List[PSRItem] = Field(default_factory=list, description="List of nearby PSR features")
