"""Background warm-up of the terrain tile cache after the API starts (TILE_WARMUP=1).

Every deploy or restart empties the in-process tile cache, so the first visitor of the globe
pays for rendering every tile. With TILE_WARMUP=1 each worker renders, in one low-priority
background thread, the tiles the globe asks for first: rings around the south pole at
every level up to TILE_WARMUP_MAX_LEVEL, heightmap and hillshade (with the globe's
hillshade parameters; hillshade only south of its fade_start_lat, where the globe shows
it). The thread starts after the app is up, never blocks requests or
/health, uses one render slot at a time and skips tiles that fail.

Settings (environment):
  TILE_WARMUP             1 to enable (default off)
  TILE_WARMUP_MAX_LEVEL   deepest level to warm (default 7)
  TILE_WARMUP_PER_LEVEL   most tiles per level and product, closest to the pole first (default 512)
  TILE_WARMUP_LAT         only tiles south of this latitude (default -60)
  TILE_WARMUP_KINDS       products, comma separated (default "heightmap,hillshade")
  TILE_WARMUP_HILLSHADE   hillshade query the globe uses
                          (default "exaggeration=1.6&fade_start_lat=-83&fade_end_lat=-85")
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any
from urllib.parse import parse_qsl

from app.engine import tiles

# Under uvicorn.error so the start/finish lines show in the server log at INFO.
logger = logging.getLogger("uvicorn.error").getChild("tile_warmup")

DEFAULT_HILLSHADE_QUERY = "exaggeration=1.6&fade_start_lat=-83&fade_end_lat=-85"
_HILLSHADE_KEYS = {"light_lon", "light_lat", "exaggeration", "fade_start_lat", "fade_end_lat"}
_MAX_LEVEL = {"terrarium": tiles.MAX_TILE_LEVEL, "hillshade": tiles.MAX_TILE_LEVEL, "heightmap": tiles.MAX_HEIGHTMAP_LEVEL}

Job = tuple[str, int, int, int, dict[str, float]]


def hillshade_params(query: str) -> dict[str, float]:
    """Parse and validate a hillshade query string ("exaggeration=1.6&fade_start_lat=-83...")."""
    params: dict[str, float] = {}
    for key, value in parse_qsl(query, strict_parsing=bool(query)):
        if key not in _HILLSHADE_KEYS:
            raise ValueError(f"unknown hillshade parameter {key!r}")
        params[key] = float(value)
    defaults = {"light_lon": tiles.DEFAULT_LIGHT_LON_DEG, "light_lat": tiles.DEFAULT_LIGHT_LAT_DEG, "exaggeration": 1.0}
    merged = {**defaults, **params}
    tiles._hillshade_params(
        merged["light_lon"], merged["light_lat"], merged["exaggeration"],
        merged.get("fade_start_lat"), merged.get("fade_end_lat"),
    )  # raises ValueError like the endpoint would answer 422
    return params


def _polar_rows(z: int, lat_limit: float, per_level: int) -> list[int]:
    """Whole rows of level z from the south pole up to lat_limit, as many as fit in
    per_level tiles (always at least the polar row)."""
    n_rows, n_cols = 2**z, 2 ** (z + 1)
    span = 180.0 / n_rows
    rows: list[int] = []
    for row in range(n_rows - 1, -1, -1):  # the polar row first
        south_edge = 90.0 - (row + 1) * span
        if south_edge > lat_limit or (rows and (len(rows) + 1) * n_cols > per_level):
            break
        rows.append(row)
    return rows


def hillshade_lat_limit(lat_limit: float, hillshade: dict[str, float] | None) -> float:
    """Hillshade that fades out toward the north (fade_end_lat south of fade_start_lat) is
    transparent north of fade_start_lat, and the globe does not request it there: warm it
    only south of that latitude."""
    start, end = (hillshade or {}).get("fade_start_lat"), (hillshade or {}).get("fade_end_lat")
    if start is not None and end is not None and end < start:
        return min(lat_limit, start)
    return lat_limit


def warmup_jobs(
    max_level: int = 7,
    per_level: int = 512,
    lat_limit: float = -60.0,
    kinds: tuple[str, ...] = ("heightmap", "hillshade"),
    hillshade: dict[str, float] | None = None,
) -> list[Job]:
    """Tiles to warm, coarse levels first: rings around the south pole up to lat_limit
    (hillshade only where it is visible, see hillshade_lat_limit), at most per_level tiles
    per level and product."""
    jobs: list[Job] = []
    for z in range(0, max_level + 1):
        n_cols = 2 ** (z + 1)
        for kind in kinds:
            if z > _MAX_LEVEL[kind]:
                continue
            limit = hillshade_lat_limit(lat_limit, hillshade) if kind == "hillshade" else lat_limit
            extra = dict(hillshade or {}) if kind == "hillshade" else {}
            jobs.extend((kind, z, row, col, extra) for row in _polar_rows(z, limit, per_level) for col in range(n_cols))
    return jobs


def run_warmup(jobs: list[Job], stop: threading.Event | None = None) -> dict[str, Any]:
    """Render the jobs through the regular cache, skipping failures. Returns counters."""
    started = time.perf_counter()
    done = failed = 0
    for kind, z, row, col, extra in jobs:
        if stop is not None and stop.is_set():
            break
        try:
            tiles.render_tile(kind, z, row, col, **extra)
            done += 1
        except Exception as exc:  # a tile the DEMs do not cover must not stop the rest
            failed += 1
            if failed <= 5:
                logger.warning("Tile warm-up skipped %s %s/%s/%s: %s", kind, z, row, col, exc)
    return {"tiles": done, "failed": failed, "seconds": round(time.perf_counter() - started, 1)}


def _lower_priority() -> None:
    """Nice this thread on Linux (setpriority on a thread id affects only that thread)."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 10)
    except (AttributeError, OSError):
        pass


def start_from_env() -> tuple[threading.Thread, threading.Event] | None:
    """Start the warm-up thread when TILE_WARMUP=1; None when disabled or misconfigured."""
    if os.environ.get("TILE_WARMUP", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    try:
        kinds = tuple(k.strip() for k in os.environ.get("TILE_WARMUP_KINDS", "heightmap,hillshade").split(",") if k.strip())
        unknown = [k for k in kinds if k not in _MAX_LEVEL]
        if unknown:
            raise ValueError(f"unknown tile products {unknown}")
        jobs = warmup_jobs(
            max_level=int(os.environ.get("TILE_WARMUP_MAX_LEVEL", 7)),
            per_level=int(os.environ.get("TILE_WARMUP_PER_LEVEL", 512)),
            lat_limit=float(os.environ.get("TILE_WARMUP_LAT", -60)),
            kinds=kinds,
            hillshade=hillshade_params(os.environ.get("TILE_WARMUP_HILLSHADE", DEFAULT_HILLSHADE_QUERY)),
        )
    except ValueError as exc:
        logger.warning("Tile warm-up disabled: invalid settings (%s)", exc)
        return None

    stop = threading.Event()

    def worker() -> None:
        _lower_priority()
        logger.info("Tile warm-up started: %d tiles", len(jobs))
        stats = run_warmup(jobs, stop)
        logger.info("Tile warm-up finished: %(tiles)d tiles, %(failed)d skipped, %(seconds)s s", stats)

    thread = threading.Thread(target=worker, name="tile-warmup", daemon=True)
    thread.start()
    return thread, stop
