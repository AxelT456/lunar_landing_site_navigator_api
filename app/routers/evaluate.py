"""Landing site mission evaluation router."""

from __future__ import annotations

from fastapi import APIRouter

from app.models import EvaluateRequest, EvaluateResponse
from app.routers._common import run_site_evaluation

router = APIRouter(tags=["evaluate"])


@router.post("/evaluate", response_model=EvaluateResponse)
def evaluate_site(payload: EvaluateRequest) -> EvaluateResponse:
    """Evaluate 360-deg horizon, Sun/Earth ephemerides, power, DTE, and Go/No-Go verdict."""
    result = run_site_evaluation(
        lat=payload.lat,
        lon=payload.lon,
        start_utc=payload.start_utc,
        duration_days=payload.duration_days,
        step_minutes=payload.step_minutes,
        panel_efficiency=payload.panel_efficiency,
        panel_area_m2=payload.panel_area_m2,
        include_details=True,
    )
    return EvaluateResponse(**result)
