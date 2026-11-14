"""Multi-scenario comparison router."""

from __future__ import annotations

from fastapi import APIRouter

from app.models import CompareRequest, CompareResponse, CompareResultItem
from app.routers._common import run_site_evaluation

router = APIRouter(tags=["compare"])


@router.post("/compare", response_model=CompareResponse)
def compare_scenarios(payload: CompareRequest) -> CompareResponse:
    """Compare 2 to 5 landing site/date scenarios side-by-side."""
    results: list[CompareResultItem] = []
    for scenario in payload.scenarios:
        evaluated = run_site_evaluation(
            lat=scenario.lat,
            lon=scenario.lon,
            start_utc=scenario.start_utc,
            duration_days=payload.duration_days,
            step_minutes=payload.step_minutes,
            panel_efficiency=payload.panel_efficiency,
            panel_area_m2=payload.panel_area_m2,
            include_details=False,
        )
        results.append(
            CompareResultItem(
                label=scenario.label,
                site=evaluated["site"],
                summary=evaluated["summary"],
            )
        )
    return CompareResponse(results=results)
