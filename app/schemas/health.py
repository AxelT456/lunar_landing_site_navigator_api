"""Health check schemas."""

from pydantic import Field
from app.schemas.common import BaseSchema


class DatabaseStats(BaseSchema):
    """Database statistics overview."""

    landing_sites_count: int = Field(..., description="Number of registered candidate landing sites")
    psr_regions_count: int = Field(..., description="Number of registered permanently shadowed regions (PSRs)")


class HealthResponse(BaseSchema):
    """System health check response."""

    status: str = Field(default="ok", description="Overall service status")
    database: DatabaseStats = Field(..., description="Database availability and statistics")
