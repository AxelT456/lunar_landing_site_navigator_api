"""Operational evaluation domain logic (backward-compatible adapter)."""

from app.domain.services.evaluator import EvaluatorService
from app.schemas.evaluate import EvaluateRequest, EvaluateResponse

_default_evaluator = EvaluatorService()


def compute_site_evaluation(req: EvaluateRequest) -> EvaluateResponse:
    """Evaluate landing site operations, solar illumination, and DTE communication."""
    return _default_evaluator.evaluate_site(req)
