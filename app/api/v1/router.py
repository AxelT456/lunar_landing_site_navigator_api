"""API v1 router aggregating all modular endpoints."""

from fastapi import APIRouter

from app.api.v1.endpoints import compare, evaluate, health, psr, sites, terrain

api_router = APIRouter()

api_router.include_router(health.router, tags=["health"])
api_router.include_router(sites.router, tags=["sites"])
api_router.include_router(terrain.router, tags=["terrain"])
api_router.include_router(evaluate.router, tags=["evaluate"])
api_router.include_router(compare.router, tags=["compare"])
api_router.include_router(psr.router, tags=["psr"])
