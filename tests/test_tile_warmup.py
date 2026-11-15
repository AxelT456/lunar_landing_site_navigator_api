"""Background warm-up of the terrain tile cache (TILE_WARMUP)."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.engine import tile_warmup, tiles
from app.main import app
from tests.test_tiles import data_dir, write_global_dem, write_polar_dem  # noqa: F401  (fixture)

FRONT = {"exaggeration": 1.6, "fade_start_lat": -83.0, "fade_end_lat": -85.0}


def test_jobs_are_polar_rings_coarse_levels_first() -> None:
    jobs = tile_warmup.warmup_jobs(max_level=7, per_level=512, lat_limit=-60.0, hillshade=FRONT)
    levels = [z for _, z, _, _, _ in jobs]
    assert levels == sorted(levels)
    for kind, z, row, col, extra in jobs:
        lon_w, lat_s, lon_e, lat_n = tiles.tile_bounds(z, row, col)
        assert lat_s <= -60.0  # every tile reaches south of the limit
        assert extra == (FRONT if kind == "hillshade" else {})
    per_level = {}
    for kind, z, row, _, _ in jobs:
        per_level.setdefault((kind, z), set()).add(row)
    # whole rows from the pole up, within 512 tiles per level and product
    assert per_level[("hillshade", 7)] == {126, 127} and per_level[("heightmap", 6)] == {60, 61, 62, 63}
    assert per_level[("heightmap", 3)] == {6, 7}  # 67.5..90 S and 45..67.5 S (crosses -60)
    count = sum(1 for kind, *_ in jobs if kind == "heightmap")
    assert count == 2 + 4 + 8 + 32 + 96 + 384 + 512 + 512
    assert len(jobs) == 2 * count


def test_jobs_respect_each_product_max_level() -> None:
    jobs = tile_warmup.warmup_jobs(max_level=12, per_level=8, kinds=("hillshade", "heightmap"))
    assert max(z for k, z, *_ in jobs if k == "hillshade") == tiles.MAX_TILE_LEVEL
    assert max(z for k, z, *_ in jobs if k == "heightmap") == tiles.MAX_HEIGHTMAP_LEVEL
    # a level wider than per_level still warms its polar row
    assert {r for k, z, r, *_ in jobs if k == "heightmap" and z == 12} == {2**12 - 1}


def test_hillshade_query_is_validated() -> None:
    assert tile_warmup.hillshade_params(tile_warmup.DEFAULT_HILLSHADE_QUERY) == FRONT
    assert tile_warmup.hillshade_params("") == {}
    for bad in ("exaggeration=50", "fade_start_lat=-83", "colour=red", "exaggeration=abc"):
        with pytest.raises(ValueError):
            tile_warmup.hillshade_params(bad)


def test_warmup_fills_the_cache_and_skips_failures(data_dir: Path) -> None:  # noqa: F811
    write_polar_dem(data_dir)  # no global DEM: tiles outside the polar raster fail
    jobs = [("heightmap", 8, 255, 300, {}), ("hillshade", 8, 255, 300, FRONT), ("heightmap", 2, 1, 0, {})]
    stats = tile_warmup.run_warmup(jobs)
    assert (stats["tiles"], stats["failed"]) == (2, 1)
    cached = {(k[0], k[1], k[2], k[3]) for k in tiles._tile_cache}
    assert {("heightmap", 8, 255, 300), ("hillshade", 8, 255, 300)} <= cached
    # the endpoint serves the warmed hillshade without rendering it again
    rendered = []
    real = tiles._render
    tiles._render = lambda *a: rendered.append(a) or real(*a)
    try:
        with TestClient(app) as client:
            r = client.get("/api/v1/terrain/tiles/hillshade/8/255/300.png?exaggeration=1.6&fade_start_lat=-83&fade_end_lat=-85")
    finally:
        tiles._render = real
    assert r.status_code == 200 and rendered == []


def test_warmup_stops_when_asked(data_dir: Path) -> None:  # noqa: F811
    write_polar_dem(data_dir)
    stop = threading.Event()
    stop.set()
    assert tile_warmup.run_warmup([("heightmap", 8, 255, 300, {})], stop)["tiles"] == 0


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TILE_WARMUP", raising=False)
    assert tile_warmup.start_from_env() is None


def test_invalid_settings_disable_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TILE_WARMUP", "1")
    monkeypatch.setenv("TILE_WARMUP_KINDS", "heightmap,clouds")
    assert tile_warmup.start_from_env() is None
    monkeypatch.setenv("TILE_WARMUP_KINDS", "heightmap")
    monkeypatch.setenv("TILE_WARMUP_HILLSHADE", "exaggeration=99")
    assert tile_warmup.start_from_env() is None


def test_app_answers_while_warming(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    write_polar_dem(data_dir)
    write_global_dem(data_dir)
    release, entered = threading.Event(), threading.Event()
    real = tile_warmup.run_warmup

    def slow_run(jobs, stop=None):
        entered.set()
        release.wait(5)  # keeps the warm-up busy while the app serves requests
        return real(jobs[:2], stop)

    monkeypatch.setattr(tile_warmup, "run_warmup", slow_run)
    monkeypatch.setenv("TILE_WARMUP", "1")
    monkeypatch.setenv("TILE_WARMUP_MAX_LEVEL", "2")
    started = time.perf_counter()
    with TestClient(app) as client:
        assert entered.wait(5)
        health = client.get("/api/v1/health")
        tile = client.get("/api/v1/terrain/tiles/heightmap/8/255/300.bin")
        assert time.perf_counter() - started < 4  # neither waited for the warm-up
        release.set()
    assert health.status_code == 200 and tile.status_code == 200
    threads = [t for t in threading.enumerate() if t.name == "tile-warmup"]
    for t in threads:
        t.join(5)
    assert not any(t.is_alive() for t in threads)
