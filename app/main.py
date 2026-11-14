"""FastAPI application entry point for Lunar Landing Site Navigator."""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db
from app.models import ErrorDetail, ErrorResponse
from app.routers import compare, evaluate, health, psr, sites, terrain

logger = logging.getLogger(__name__)

_ERR_CODE_PATTERN = re.compile(r"\b(ERR_[A-Z0-9_]+)\b(?::\s*)?")


def _build_error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    detail: Any | None = None,
) -> JSONResponse:
    """Serialize unified ErrorResponse matching docs/06 and OpenAPI schema."""
    payload = ErrorResponse(
        error=ErrorDetail(code=code, message=message, details=details),
        detail=detail if detail is not None else message,
    )
    return JSONResponse(status_code=status_code, content=payload.model_dump())


def _classify_validation_errors(errors: list[dict[str, Any]]) -> tuple[int, str, str, dict[str, Any]]:
    """Map Pydantic RequestValidationError items to HTTP status and ERR_* codes."""
    messages: list[str] = []
    chosen_status = 422
    chosen_code: str | None = None
    first_field: str | None = None
    first_value: Any = None

    for err in errors:
        raw_msg = str(err.get("msg", "Invalid request"))
        err_type = str(err.get("type", ""))
        loc = [str(part) for part in err.get("loc", ())]
        field = loc[-1] if loc else None
        if first_field is None and field:
            first_field = field
            first_value = err.get("input")

        clean_msg = raw_msg.removeprefix("Value error, ").strip()
        match = _ERR_CODE_PATTERN.search(clean_msg)
        explicit_code = match.group(1) if match else None
        if match:
            clean_msg = _ERR_CODE_PATTERN.sub("", clean_msg, count=1).strip()
        messages.append(f"{field}: {clean_msg}" if field else clean_msg)

        if chosen_code is not None:
            continue

        if err_type == "json_invalid":
            chosen_status = 400
            chosen_code = "ERR_INVALID_PAYLOAD"
        elif err_type == "missing" and "body" in loc:
            chosen_status = 400
            chosen_code = "ERR_INVALID_PAYLOAD"
        elif explicit_code:
            chosen_code = explicit_code
        elif "scenarios" in loc and err_type in {"too_short", "too_long"}:
            chosen_code = "ERR_SCENARIO_LIMIT_EXCEEDED"
        elif any(part in {"lat", "lon"} for part in loc):
            chosen_code = "ERR_INVALID_COORDINATES"
        elif "start_utc" in loc:
            chosen_code = "ERR_DATE_OUT_OF_RANGE" if ("range" in clean_msg.lower() or "1900" in clean_msg or "2050" in clean_msg) else "ERR_INVALID_PAYLOAD"
        else:
            chosen_code = "ERR_INVALID_PAYLOAD"

    if chosen_code is None:
        chosen_code = "ERR_INVALID_PAYLOAD"

    combined_message = "; ".join(messages) if messages else "Invalid request parameters"
    details: dict[str, Any] = {
        "errors": errors,
    }
    if first_field is not None:
        details["field"] = first_field
        if first_value is not None:
            details["value"] = first_value
    if chosen_code == "ERR_INVALID_COORDINATES" and first_field == "lat":
        details["allowed_range"] = [-90.0, 90.0]
    elif chosen_code == "ERR_INVALID_COORDINATES" and first_field == "lon":
        details["allowed_range"] = [-180.0, 360.0]

    return chosen_status, chosen_code, combined_message, details


def _classify_http_exception(status_code: int, detail: Any) -> tuple[str, str, dict[str, Any] | None]:
    """Map Starlette/FastAPI HTTPException status and detail to ERR_* codes."""
    if isinstance(detail, dict) and "error" in detail and isinstance(detail["error"], dict):
        err_obj = detail["error"]
        return (
            str(err_obj.get("code", "ERR_INVALID_PAYLOAD")),
            str(err_obj.get("message", detail)),
            err_obj.get("details"),
        )

    msg = str(detail) if detail is not None else "HTTP error"
    match = _ERR_CODE_PATTERN.search(msg)
    if match:
        code = match.group(1)
        clean_msg = _ERR_CODE_PATTERN.sub("", msg, count=1).strip()
        return code, clean_msg or msg, None

    low = msg.lower()
    if status_code == 404:
        return "ERR_NOT_FOUND", msg, None
    if status_code == 503:
        if any(tok in low for tok in ("lola", "dem", "raster", "tif", "terrain")):
            return "ERR_TERRAIN_UNAVAILABLE", msg, None
        return "ERR_UPSTREAM_SERVICE", msg, None
    if any(tok in low for tok in ("lat", "lon", "polar", "coordinate")):
        return "ERR_INVALID_COORDINATES", msg, None
    if any(tok in low for tok in ("date", "start_utc", "epoch", "1900", "2050")):
        return "ERR_DATE_OUT_OF_RANGE", msg, None
    if "duplicate" in low:
        return "ERR_DUPLICATE_SCENARIOS", msg, None
    if "scenario" in low:
        return "ERR_SCENARIO_LIMIT_EXCEEDED", msg, None
    if any(tok in low for tok in ("terrain", "dem", "lola")):
        return "ERR_TERRAIN_UNAVAILABLE", msg, None
    return "ERR_INVALID_PAYLOAD", msg, None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Initialize and tear down PostgreSQL connection pool around app lifespan."""
    try:
        db.init_pool()
    except RuntimeError as exc:
        # Without DATABASE_URL the API still starts: /health reports the database
        # as disconnected and DB-backed endpoints answer 503.
        logger.warning("Database pool not started: %s", exc)
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(
    title="Lunar Landing Site Navigator API",
    version="1.0.0",
    description=(
        "NASA Space Apps 2026 API for evaluating Lunar South Pole landing sites "
        "using LOLA DEM/slope/PSR datasets, 360-degree horizon raycasting, and SPICE ephemerides.\n\n"
        "- **Swagger UI:** `/docs`\n"
        "- **ReDoc:** `/redoc`\n"
        "- **OpenAPI JSON:** `/openapi.json`\n"
        "- **Health:** `/api/v1/health`"
    ),
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    # The public URL (Cloudflare tunnel) changes on restart: its single source is
    # docs/07 in the handoff repo, so it is not hardcoded here.
    servers=[{"url": "/", "description": "Current server"}],
    responses={
        400: {"model": ErrorResponse, "description": "Invalid JSON or missing required payload field"},
        404: {"model": ErrorResponse, "description": "Requested resource or route not found"},
        422: {"model": ErrorResponse, "description": "Validation or domain rule violation (ERR_*)"},
        503: {"model": ErrorResponse, "description": "Terrain dataset, SPICE kernel, or database unavailable"},
    },
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return canonical docs/06 error object alongside Pydantic detail list."""
    raw_errors = [dict(err) for err in exc.errors()]
    # Ensure non-JSON-serializable values like ValueError exceptions in ctx are stringified.
    sanitized_errors: list[dict[str, Any]] = []
    for err in raw_errors:
        clean = dict(err)
        if "ctx" in clean and isinstance(clean["ctx"], dict):
            clean["ctx"] = {k: str(v) for k, v in clean["ctx"].items()}
        sanitized_errors.append(clean)

    status_code, code, message, details = _classify_validation_errors(sanitized_errors)
    return _build_error_response(
        status_code=status_code,
        code=code,
        message=message,
        details=details,
        detail=sanitized_errors,
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Return canonical docs/06 error object alongside HTTPException detail."""
    code, message, details = _classify_http_exception(exc.status_code, exc.detail)
    return _build_error_response(
        status_code=exc.status_code,
        code=code,
        message=message,
        details=details,
        detail=exc.detail,
    )


API_PREFIX = "/api/v1"
app.include_router(health.router, prefix=API_PREFIX)
app.include_router(sites.router, prefix=API_PREFIX)
app.include_router(terrain.router, prefix=API_PREFIX)
app.include_router(evaluate.router, prefix=API_PREFIX)
app.include_router(compare.router, prefix=API_PREFIX)
app.include_router(psr.router, prefix=API_PREFIX)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Redirect root URL directly to interactive Swagger UI documentation."""
    return RedirectResponse(
        url="/docs",
        status_code=307,
        headers={
            "X-Docs-URL": "/docs",
            "X-Redoc-URL": "/redoc",
            "X-OpenAPI-URL": "/openapi.json",
        },
    )
