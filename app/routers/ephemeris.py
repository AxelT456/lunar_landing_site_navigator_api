"""Moon-centered Sun/Earth ephemeris router (global view, no site needed)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.models import EphemerisResponse, normalize_utc_iso
from app.routers._common import get_engine

router = APIRouter(tags=["ephemeris"])


@router.get("/ephemeris", response_model=EphemerisResponse)
def moon_center_ephemeris(
    utc: str = Query(..., description="UTC timestamp in ISO-8601 (1900-2050)", examples=["2026-12-01T00:00:00Z"]),
) -> EphemerisResponse:
    """Sun and Earth seen from the Moon's center in MOON_ME (sub-solar and sub-Earth points).

    For drawing the sky of the whole globe before any site is evaluated (handoff P-007).
    Site-local vectors stay in `/evaluate` (`sun_vector_3d`, `earth_vector_3d`).
    """
    try:
        utc_norm = normalize_utc_iso(utc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    engine = get_engine()
    try:
        result = engine.moon_center_ephemeris(utc_norm)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return EphemerisResponse(**result)
