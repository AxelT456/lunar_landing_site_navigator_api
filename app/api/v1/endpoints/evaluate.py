"""Site operational evaluation endpoint."""

from fastapi import APIRouter, Depends

from app.domain.services.evaluator import EvaluatorService
from app.schemas.evaluate import EvaluateRequest, EvaluateResponse

router = APIRouter()


def get_evaluator_service() -> EvaluatorService:
    """Dependency provider for EvaluatorService."""
    return EvaluatorService()


@router.post(
    "/evaluate",
    response_model=EvaluateResponse,
    summary="Evaluate Landing Site Operations",
    description="Calculates local 360-degree horizon obstruction, solar and Earth ephemeris timeline, and landing safety status.",
)
async def evaluate_site(
    request: EvaluateRequest,
    evaluator: EvaluatorService = Depends(get_evaluator_service),
) -> EvaluateResponse:
    """Execute operational site evaluation."""
    return evaluator.evaluate_site(request)
