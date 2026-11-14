"""Site operational evaluation endpoint."""

from fastapi import APIRouter
from app.domain.evaluation import compute_site_evaluation
from app.schemas.evaluate import EvaluateRequest, EvaluateResponse

router = APIRouter()


@router.post(
    "/evaluate",
    response_model=EvaluateResponse,
    summary="Evaluate Landing Site Operations",
    description="Calculates local 360-degree horizon obstruction, solar and Earth ephemeris timeline, and landing safety status.",
)
async def evaluate_site(request: EvaluateRequest) -> EvaluateResponse:
    """Execute operational site evaluation."""
    return compute_site_evaluation(request)
