"""Common schema types and coordinate validators."""

from typing import Annotated, List
from pydantic import BaseModel, ConfigDict, Field

from app.core.constants import (
    MAX_LON,
    MIN_LON,
    SOUTH_POLE_MAX_LAT,
    SOUTH_POLE_MIN_LAT,
)

LunarLat = Annotated[
    float,
    Field(
        ge=SOUTH_POLE_MIN_LAT,
        le=SOUTH_POLE_MAX_LAT,
        description=f"Lunar South Pole Latitude ({SOUTH_POLE_MIN_LAT} to {SOUTH_POLE_MAX_LAT})",
    ),
]

LunarLon = Annotated[
    float,
    Field(
        ge=MIN_LON,
        le=MAX_LON,
        description=f"Lunar Longitude ({MIN_LON} to {MAX_LON})",
    ),
]

Vector3D = Annotated[
    List[float],
    Field(
        min_length=3,
        max_length=3,
        description="3D Vector [X, Y, Z] in local coordinate frame",
    ),
]

Point2D = Annotated[
    List[float],
    Field(
        min_length=2,
        max_length=2,
        description="2D Point [X, Y] in polar coordinate frame",
    ),
]


class BaseSchema(BaseModel):
    """Base schema with standard configuration."""

    model_config = ConfigDict(
        populate_by_name=True,
        validate_assignment=True,
    )
