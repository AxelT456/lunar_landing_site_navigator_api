"""Health check router reporting DB, physics engine, LOLA DEMs, and SPICE kernels."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from app import db
from app.models import DatabaseHealth, EngineHealth, FileHealth, HealthResponse
from app.routers._common import check_engine_availability

router = APIRouter(tags=["health"])

EXPECTED_LOLA_FILES = (
    "LDEM_80S_80MPP_ADJ.TIF",
    "LDSM_80S_80MPP_ADJ.TIF",
    "LDEM_80S_80MPP_ADJ_HILL.TIF",
    "LDEM_80S_40MPP_ADJ.TIF",
    "LDSM_80S_40MPP_ADJ.TIF",
    "LDEM_80S_40MPP_ADJ_HILL.TIF",
    "LDEM_80S_20MPP_ADJ.TIF",
    "LDSM_80S_20MPP_ADJ.TIF",
    "LPSR_80S_20MPP_ADJ_1km2.CSV",
)

EXPECTED_LOLA_GLOBAL_FILES = (
    "ldem_64.tif",
)

EXPECTED_SPICE_FILES = (
    "de421.bsp",
    "de440s.bsp",
    "naif0012.tls",
    "moon_080317.tf",
    "moon_pa_de421_1900-2050.bpc",
)


def _inspect_directory(
    directory: Path,
    expected_names: tuple[str, ...],
    key_prefix: str = "",
) -> dict[str, FileHealth]:
    """Check expected dataset files plus any discovered extra complete files in directory."""
    statuses: dict[str, FileHealth] = {}
    seen_names: set[str] = set()
    for name in expected_names:
        file_path = directory / name
        exists = file_path.is_file() and file_path.stat().st_size > 0
        statuses[f"{key_prefix}{name}"] = FileHealth(
            exists=exists,
            path=str(file_path),
            size_bytes=file_path.stat().st_size if exists else None,
        )
        seen_names.add(name)

    if directory.is_dir():
        for entry in sorted(directory.iterdir()):
            if (
                entry.is_file()
                and entry.name not in seen_names
                and not entry.name.endswith(".tmp")
                and not entry.name.startswith(".")
            ):
                statuses[f"{key_prefix}{entry.name}"] = FileHealth(
                    exists=True,
                    path=str(entry),
                    size_bytes=entry.stat().st_size,
                )
    return statuses


@router.get("/health", response_model=HealthResponse)
def health_check() -> HealthResponse:
    """Report operational status of PostgreSQL/PostGIS, physics engine, LOLA DEMs, and SPICE kernels."""
    data_dir = Path(os.getenv("DATA_DIR", "/data"))
    db_status = DatabaseHealth(**db.check_db_health())
    engine_status = EngineHealth(**check_engine_availability())

    lola_dems = _inspect_directory(data_dir / "raw" / "lola", EXPECTED_LOLA_FILES)
    lola_global_dems = _inspect_directory(
        data_dir / "raw" / "lola_global",
        EXPECTED_LOLA_GLOBAL_FILES,
        key_prefix="lola_global/",
    )
    lola_dems.update(lola_global_dems)

    spice_kernels = _inspect_directory(data_dir / "raw" / "spice", EXPECTED_SPICE_FILES)

    has_core_dem = any(
        lola_dems[f"LDEM_80S_{tier}_ADJ.TIF"].exists
        and lola_dems[f"LDSM_80S_{tier}_ADJ.TIF"].exists
        for tier in ("20MPP", "40MPP", "80MPP")
    )
    has_bsp = any(name.endswith(".bsp") and info.exists for name, info in spice_kernels.items())
    has_tls = any(name.endswith(".tls") and info.exists for name, info in spice_kernels.items())
    has_bpc = any(name.endswith(".bpc") and info.exists for name, info in spice_kernels.items())

    overall_ok = (
        db_status.connected
        and db_status.postgis
        and engine_status.available
        and has_core_dem
        and has_bsp
        and has_tls
        and has_bpc
    )

    return HealthResponse(
        status="ok" if overall_ok else "degraded",
        timestamp_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        data_dir=str(data_dir),
        database=db_status,
        engine=engine_status,
        lola_dems=lola_dems,
        spice_kernels=spice_kernels,
    )
