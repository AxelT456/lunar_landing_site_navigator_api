"""Pydantic v2 schemas matching API_CONTRACT.md for Lunar Landing Site Navigator."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MissionStatus = Literal["GO", "CAUTION", "NO-GO"]


def normalize_lon(lon: float) -> float:
    """Normalize East longitude from [-180, 360] into [0, 360)."""
    if lon < -180.0 or lon > 360.0:
        raise ValueError("lon must be between -180.0 and 360.0 degrees")
    normalized = lon % 360.0
    if normalized == 360.0 or normalized == -0.0:
        normalized = 0.0
    return float(normalized)


def normalize_utc_iso(timestamp: str) -> str:
    """Validate and normalize an ISO-8601 timestamp string to UTC with 'Z' suffix."""
    raw = timestamp.strip()
    if not raw:
        raise ValueError("ERR_INVALID_PAYLOAD: start_utc must not be empty")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"ERR_INVALID_PAYLOAD: Invalid ISO-8601 UTC timestamp: {timestamp}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    if parsed.year < 1900 or parsed.year > 2050:
        raise ValueError(
            f"ERR_DATE_OUT_OF_RANGE: Timestamp year {parsed.year} is outside supported SPICE coverage (1900-2050)"
        )
    return parsed.isoformat().replace("+00:00", "Z")


class LandingSite(BaseModel):
    """Curated lunar landing site catalog entry."""

    id: str
    name: str
    category: str
    lat: float
    lon: float
    polar_x_m: float
    polar_y_m: float
    elevation_m: float | None = None
    slope_deg: float | None = None
    nearest_psr_km: float | None = None
    description: str | None = None


class SitesResponse(BaseModel):
    """Response payload for GET /api/v1/sites."""

    count: int
    sites: list[LandingSite]


class TerrainCenter(BaseModel):
    """Center point metadata for a 3D terrain patch."""

    lat: float
    lon: float
    polar_x_m: float
    polar_y_m: float
    elevation_m: float
    slope_deg: float


class TerrainGrid(BaseModel):
    """Row-major 3D elevation and slope grid for Three.js rendering."""

    size_km: float
    resolution: int
    cell_size_m: float
    min_elevation_m: float
    max_elevation_m: float
    elevations_m: list[float]
    slopes_deg: list[float]


class NearbyPsrSummary(BaseModel):
    """Lightweight PSR proximity summary attached to terrain patches."""

    gid: int
    name: str | None = None
    area_km2: float | None = None
    distance_km: float


class TerrainPatchResponse(BaseModel):
    """Response payload for GET /api/v1/terrain/patch."""

    center: TerrainCenter
    grid: TerrainGrid
    nearby_psrs: list[NearbyPsrSummary] = Field(default_factory=list)


class EvaluateRequest(BaseModel):
    """Request payload for POST /api/v1/evaluate."""

    lat: float = Field(..., ge=-90.0, le=90.0, description="Planetocentric latitude in degrees (-90 to 90)")
    lon: float = Field(..., ge=-180.0, le=360.0, description="East longitude in degrees (0..360 or -180..180)")
    start_utc: str = Field(..., description="Mission start UTC timestamp in ISO-8601 format")
    duration_days: float = Field(default=14.0, gt=0.0, le=366.0)
    step_minutes: int = Field(default=60, ge=5, le=1440)
    panel_efficiency: float = Field(default=0.28, gt=0.0, le=1.0)
    panel_area_m2: float = Field(default=4.0, gt=0.0, le=1000.0)

    @field_validator("lon")
    @classmethod
    def _validate_lon(cls, value: float) -> float:
        return normalize_lon(value)

    @field_validator("start_utc")
    @classmethod
    def _validate_start_utc(cls, value: str) -> str:
        return normalize_utc_iso(value)


class EvaluateSiteInfo(BaseModel):
    """Evaluated site parameters and Go/No-Go verdict."""

    lat: float
    lon: float
    elevation_m: float
    slope_deg: float
    nearest_psr_km: float | None = None
    status: MissionStatus
    reasons: list[str]


class EvaluateSummary(BaseModel):
    """Aggregated solar power and Direct-to-Earth communication metrics."""

    sun_visible_pct: float
    earth_visible_pct: float
    max_continuous_sun_hours: float
    max_continuous_dte_hours: float
    next_sunset_utc: str | None = None
    hours_to_next_sunset: float | None = None
    next_earthset_utc: str | None = None
    hours_to_next_earthset: float | None = None
    avg_power_factor: float
    estimated_peak_power_w: float


class HorizonPoint(BaseModel):
    """Topographic horizon elevation angle at a given azimuth degree."""

    az_deg: int = Field(..., ge=0, le=359)
    horizon_el_deg: float


class TimelinePoint(BaseModel):
    """Single ephemeris + horizon visibility sample along the mission timeline."""

    timestamp_utc: str
    sun_az_deg: float
    sun_el_deg: float
    sun_horizon_el_deg: float
    sun_visible: bool
    sun_vector_3d: list[float] = Field(..., min_length=3, max_length=3)
    earth_az_deg: float
    earth_el_deg: float
    earth_horizon_el_deg: float
    earth_visible: bool
    earth_vector_3d: list[float] = Field(..., min_length=3, max_length=3)
    power_factor: float


class EvaluateResponse(BaseModel):
    """Response payload for POST /api/v1/evaluate."""

    site: EvaluateSiteInfo
    summary: EvaluateSummary
    horizon_profile: list[HorizonPoint]
    timeline: list[TimelinePoint]


class CompareScenario(BaseModel):
    """Single scenario entry for side-by-side comparison."""

    label: str = Field(..., min_length=1, max_length=120)
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=360.0)
    start_utc: str

    @field_validator("lon")
    @classmethod
    def _validate_lon(cls, value: float) -> float:
        return normalize_lon(value)

    @field_validator("start_utc")
    @classmethod
    def _validate_start_utc(cls, value: str) -> str:
        return normalize_utc_iso(value)


class CompareRequest(BaseModel):
    """Request payload for POST /api/v1/compare (2 to 5 scenarios)."""

    scenarios: list[CompareScenario] = Field(..., min_length=2, max_length=5)
    duration_days: float = Field(default=14.0, gt=0.0, le=366.0)
    step_minutes: int = Field(default=120, ge=5, le=1440)
    panel_efficiency: float = Field(default=0.28, gt=0.0, le=1.0)
    panel_area_m2: float = Field(default=4.0, gt=0.0, le=1000.0)

    @model_validator(mode="after")
    def _validate_scenarios_bounds_and_uniqueness(self) -> "CompareRequest":
        if len(self.scenarios) < 2 or len(self.scenarios) > 5:
            raise ValueError("ERR_SCENARIO_LIMIT_EXCEEDED: Compare requires between 2 and 5 scenarios")
        seen: set[tuple[float, float, str]] = set()
        for scenario in self.scenarios:
            key = (round(scenario.lat, 4), round(scenario.lon, 4), scenario.start_utc)
            if key in seen:
                raise ValueError(
                    f"ERR_DUPLICATE_SCENARIOS: Duplicate scenario detected (lat={scenario.lat}, lon={scenario.lon}, start_utc={scenario.start_utc})"
                )
            seen.add(key)
        return self


class CompareResultItem(BaseModel):
    """Single evaluated scenario entry in CompareResponse."""

    label: str
    site: EvaluateSiteInfo
    summary: EvaluateSummary


class CompareResponse(BaseModel):
    """Response payload for POST /api/v1/compare."""

    results: list[CompareResultItem]


class PsrQueryCenter(BaseModel):
    """Query center in planetocentric and south polar stereographic coordinates."""

    lat: float
    lon: float
    polar_x_m: float
    polar_y_m: float


class PsrGeoJsonGeometry(BaseModel):
    """Simplified GeoJSON geometry in south polar stereographic meters."""

    type: str
    coordinates: list[Any]


class PsrNearbyItem(BaseModel):
    """Permanently Shadowed Region record with simplified polar geometry."""

    gid: int
    name: str | None = None
    area_km2: float | None = None
    distance_km: float
    geometry: PsrGeoJsonGeometry


class PsrNearbyResponse(BaseModel):
    """Response payload for GET /api/v1/psr/nearby."""

    center: PsrQueryCenter
    radius_km: float
    count: int
    psrs: list[PsrNearbyItem]


class FileHealth(BaseModel):
    """On-disk dataset file presence and size."""

    exists: bool
    path: str
    size_bytes: int | None = None


class DatabaseHealth(BaseModel):
    """PostgreSQL / PostGIS connectivity and row counts."""

    connected: bool
    postgis: bool = False
    postgis_version: str | None = None
    landing_sites_count: int = 0
    psr_regions_count: int = 0
    error: str | None = None


class EngineHealth(BaseModel):
    """Physics engine availability and contract check."""

    available: bool
    missing_symbols: list[str] = Field(default_factory=list)
    error: str | None = None


class HealthResponse(BaseModel):
    """Response payload for GET /api/v1/health."""

    status: Literal["ok", "degraded"]
    timestamp_utc: str
    data_dir: str
    database: DatabaseHealth
    engine: EngineHealth
    lola_dems: dict[str, FileHealth]
    spice_kernels: dict[str, FileHealth]


class ErrorDetail(BaseModel):
    """Contractual error payload matching docs/06-contrato-y-errores.md."""

    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    """Unified error response envelope."""

    error: ErrorDetail
    detail: Any | None = None
