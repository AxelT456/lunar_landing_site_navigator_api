"""Permanently Shadowed Regions (PSR) spatial query router."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app import db
from app.models import (
    PsrLabelPoint,
    PsrListItem,
    PsrListResponse,
    PsrNearbyItem,
    PsrNearbyResponse,
    PsrQueryCenter,
)
from app.routers._common import get_engine, safe_normalize_lon

router = APIRouter(tags=["psr"])


@router.get("/psr/nearby", response_model=PsrNearbyResponse)
def nearby_psrs(
    lat: float = Query(..., ge=-90.0, le=90.0, description="Planetocentric latitude in degrees"),
    lon: float = Query(..., ge=-180.0, le=360.0, description="East longitude in degrees (0..360 or -180..180)"),
    radius_km: float = Query(25.0, gt=0.0, le=300.0, description="Search radius in kilometers"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of PSR polygons to return"),
    simplify_m: float = Query(150.0, ge=0.0, le=2000.0, description="Geometry simplification tolerance in meters"),
) -> PsrNearbyResponse:
    """Return PSR polygons within radius_km in south polar stereographic coordinates."""
    norm_lon = safe_normalize_lon(lon)
    engine = get_engine()

    try:
        polar_x_m, polar_y_m = engine.polar_stereographic_from_latlon(lat, norm_lon)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        rows = db.get_nearby_psrs(
            polar_x_m=polar_x_m,
            polar_y_m=polar_y_m,
            radius_km=radius_km,
            limit=limit,
            simplify_m=simplify_m,
            include_geometry=True,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database error querying psr_regions: {exc}") from exc

    psrs = [PsrNearbyItem(**row) for row in rows]
    return PsrNearbyResponse(
        center=PsrQueryCenter(
            lat=lat,
            lon=norm_lon,
            polar_x_m=polar_x_m,
            polar_y_m=polar_y_m,
        ),
        radius_km=radius_km,
        count=len(psrs),
        psrs=psrs,
    )


@router.get("/psr", response_model=PsrListResponse)
def list_psrs(
    simplify_m: float = Query(500.0, ge=0.0, le=5000.0, description="Geometry simplification tolerance in meters"),
    limit: int = Query(500, ge=1, le=500, description="Page size"),
    offset: int = Query(0, ge=0, description="Number of PSRs to skip (ordered by gid)"),
    min_area_km2: float = Query(0.0, ge=0.0, description="Only PSRs at least this large"),
) -> PsrListResponse:
    """Whole PSR catalog, paginated, for views of the entire pole (the globe).

    `/psr/nearby` stays the call for one site; this one lists every region once.
    """
    engine = get_engine()
    try:
        total, rows = db.list_psrs(simplify_m=simplify_m, limit=limit, offset=offset, min_area_km2=min_area_km2)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database error listing psr_regions: {exc}") from exc

    psrs: list[PsrListItem] = []
    for row in rows:
        x_m, y_m = float(row.pop("label_x_m")), float(row.pop("label_y_m"))
        lat, lon = engine.latlon_from_polar_stereographic(x_m, y_m)
        psrs.append(
            PsrListItem(
                **row,
                label_point=PsrLabelPoint(polar_x_m=x_m, polar_y_m=y_m, lat=lat, lon=lon),
            )
        )
    return PsrListResponse(
        total=total,
        offset=offset,
        limit=limit,
        count=len(psrs),
        simplify_m=simplify_m,
        psrs=psrs,
    )
