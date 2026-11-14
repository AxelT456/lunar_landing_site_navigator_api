"""Scenario comparison schemas."""

from datetime import datetime
from typing import List
from pydantic import Field

from app.core.constants import (
    DEFAULT_DURATION_DAYS,
    DEFAULT_PANEL_AREA_M2,
    DEFAULT_PANEL_EFFICIENCY,
    MAX_DURATION_DAYS,
    MAX_SCENARIOS,
    MAX_STEP_MINUTES,
    MIN_DURATION_DAYS,
    MIN_SCENARIOS,
    MIN_STEP_MINUTES,
)
from app.schemas.common import BaseSchema, LunarLat, LunarLon
from app.schemas.evaluate import EvaluationSummary, SiteEvaluation


class ScenarioInput(BaseSchema):
    """Single landing site scenario for comparative evaluation."""

    label: str = Field(..., description="Descriptive label for scenario comparison")
    lat: LunarLat
    lon: LunarLon
    start_utc: datetime = Field(..., description="Scenario mission start timestamp (ISO-8601 UTC)")


class CompareRequest(BaseSchema):
    """Request payload for multi-scenario comparison."""

    scenarios: List[ScenarioInput] = Field(
        ...,
        min_length=MIN_SCENARIOS,
        max_length=MAX_SCENARIOS,
        description=f"List of candidate scenarios to evaluate ({MIN_SCENARIOS} to {MAX_SCENARIOS} scenarios)",
    )
    duration_days: float = Field(
        default=DEFAULT_DURATION_DAYS,
        ge=MIN_DURATION_DAYS,
        le=MAX_DURATION_DAYS,
        description="Duration of evaluation window in days",
    )
    step_minutes: int = Field(
        default=120,
        ge=MIN_STEP_MINUTES,
        le=MAX_STEP_MINUTES,
        description="Time step resolution in minutes",
    )
    panel_efficiency: float = Field(
        default=DEFAULT_PANEL_EFFICIENCY,
        ge=0.0,
        le=1.0,
        description="Solar panel efficiency (0.0 to 1.0)",
    )
    panel_area_m2: float = Field(
        default=DEFAULT_PANEL_AREA_M2,
        gt=0.0,
        description="Solar panel array area in square meters (> 0.0)",
    )


class CompareResult(BaseSchema):
    """Comparative outcome for an individual scenario."""

    label: str = Field(..., description="Label identifying the evaluated scenario")
    site: SiteEvaluation
    summary: EvaluationSummary


class CompareResponse(BaseSchema):
    """Response containing comparative metrics across scenarios."""

    results: List[CompareResult] = Field(..., description="Results for each evaluated scenario")
