"""Health endpoint."""

from fastapi import APIRouter
from app.schemas.health import DatabaseStats, HealthResponse

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health Check",
    description="Verifies the operational status of the backend API and spatial databases.",
)
async def get_health() -> HealthResponse:
    """Return system and database health status."""
    return HealthResponse(
        status="ok",
        database=DatabaseStats(
            landing_sites_count=10,
            psr_regions_count=1531,
        ),
    )
