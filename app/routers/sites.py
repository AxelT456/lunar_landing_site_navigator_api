"""Curated landing sites catalog router."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from app import db
from app.models import LandingSite, SitesResponse
from app.routers._common import safe_nearest_psr_km, try_get_engine

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sites"])


@router.get("/sites", response_model=SitesResponse)
def list_sites() -> SitesResponse:
    """Return curated lunar landing sites enriched with LOLA terrain metrics if missing in DB."""
    try:
        rows = db.list_landing_sites()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Database error listing landing sites: {exc}") from exc

    engine = try_get_engine()
    sites: list[LandingSite] = []

    for row in rows:
        elevation_m = row.get("elevation_m")
        slope_deg = row.get("slope_deg")
        nearest_psr_km = row.get("nearest_psr_km")

        if (elevation_m is None or slope_deg is None) and engine is not None:
            try:
                sampled = engine.sample_point_terrain(row["lat"], row["lon"])
                if elevation_m is None:
                    elevation_m = sampled["elevation_m"]
                if slope_deg is None:
                    slope_deg = sampled["slope_deg"]
            except Exception as exc:
                logger.warning("Failed enriching site %s terrain from engine: %s", row.get("id"), exc)

        if nearest_psr_km is None:
            nearest_psr_km = safe_nearest_psr_km(row["polar_x_m"], row["polar_y_m"])

        sites.append(
            LandingSite(
                id=row["id"],
                name=row["name"],
                category=row["category"],
                lat=row["lat"],
                lon=row["lon"],
                polar_x_m=row["polar_x_m"],
                polar_y_m=row["polar_y_m"],
                elevation_m=elevation_m,
                slope_deg=slope_deg,
                nearest_psr_km=nearest_psr_km,
                description=row.get("description"),
            )
        )

    return SitesResponse(count=len(sites), sites=sites)
