"""Scenario comparison endpoint."""

from fastapi import APIRouter
from app.domain.evaluation import compute_site_evaluation
from app.schemas.compare import CompareRequest, CompareResponse, CompareResult
from app.schemas.evaluate import EvaluateRequest

router = APIRouter()


@router.post(
    "/compare",
    response_model=CompareResponse,
    summary="Compare Multiple Landing Scenarios",
    description="Evaluates 2 to 5 site and date combinations concurrently for trade studies.",
)
async def compare_scenarios(request: CompareRequest) -> CompareResponse:
    """Run comparative analysis across given scenarios."""
    results: list[CompareResult] = []

    for sc in request.scenarios:
        eval_req = EvaluateRequest(
            lat=sc.lat,
            lon=sc.lon,
            start_utc=sc.start_utc,
            duration_days=request.duration_days,
            step_minutes=request.step_minutes,
            panel_efficiency=request.panel_efficiency,
            panel_area_m2=request.panel_area_m2,
        )
        eval_resp = compute_site_evaluation(eval_req)
        results.append(
            CompareResult(
                label=sc.label,
                site=eval_resp.site,
                summary=eval_resp.summary,
            )
        )

    return CompareResponse(results=results)
