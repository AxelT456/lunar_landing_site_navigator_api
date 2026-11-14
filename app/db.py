"""PostgreSQL + PostGIS connection pool and spatial/cache data access helpers."""

from __future__ import annotations

import json
import os
from typing import Any

from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

# Required at runtime; credentials are injected by the environment, never committed.
# Local development: see .env.example and db/README.md.

_pool: ConnectionPool | None = None


def get_database_url() -> str:
    """Return configured PostgreSQL connection string."""
    url = os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (see .env.example)")
    return url


def init_pool() -> ConnectionPool:
    """Create and open the shared psycopg connection pool without blocking if DB is down."""
    global _pool
    if _pool is None or _pool.closed:
        _pool = ConnectionPool(
            conninfo=get_database_url(),
            min_size=0,
            max_size=10,
            timeout=5.0,
            open=False,
            kwargs={"row_factory": dict_row, "connect_timeout": 3},
        )
        _pool.open(wait=False)
    return _pool


def close_pool() -> None:
    """Close the shared connection pool gracefully."""
    global _pool
    if _pool is not None and not _pool.closed:
        _pool.close()
    _pool = None


def get_pool() -> ConnectionPool:
    """Return the active connection pool, initializing it lazily if necessary."""
    return init_pool()


def _decode_jsonb(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _json_dumps(value: Any) -> str:
    """Serialize Python/NumPy values cleanly to JSON for JSONB persistence."""
    return json.dumps(value, default=lambda obj: obj.item() if hasattr(obj, "item") else str(obj))


def check_db_health() -> dict[str, Any]:
    """Verify PostgreSQL + PostGIS connectivity and report catalog table counts."""
    try:
        pool = get_pool()
        with pool.connection(timeout=3.0) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT PostGIS_Version() AS postgis_version;")
                pg_row = cur.fetchone()
                postgis_version = pg_row["postgis_version"] if pg_row else None

                landing_sites_count = 0
                psr_regions_count = 0
                try:
                    cur.execute("SELECT COUNT(*) AS cnt FROM landing_sites;")
                    row = cur.fetchone()
                    landing_sites_count = int(row["cnt"]) if row else 0
                except pg_errors.UndefinedTable:
                    conn.rollback()

                try:
                    cur.execute("SELECT COUNT(*) AS cnt FROM psr_regions;")
                    row = cur.fetchone()
                    psr_regions_count = int(row["cnt"]) if row else 0
                except pg_errors.UndefinedTable:
                    conn.rollback()

        return {
            "connected": True,
            "postgis": bool(postgis_version),
            "postgis_version": postgis_version,
            "landing_sites_count": landing_sites_count,
            "psr_regions_count": psr_regions_count,
            "error": None,
        }
    except Exception as exc:
        return {
            "connected": False,
            "postgis": False,
            "postgis_version": None,
            "landing_sites_count": 0,
            "psr_regions_count": 0,
            "error": str(exc),
        }


def list_landing_sites() -> list[dict[str, Any]]:
    """Fetch curated landing sites ordered by catalog primary key."""
    pool = get_pool()
    with pool.connection(timeout=5.0) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    slug AS id,
                    name,
                    category,
                    lat,
                    lon,
                    polar_x_m,
                    polar_y_m,
                    elevation_m,
                    slope_deg,
                    nearest_psr_km,
                    description
                FROM landing_sites
                WHERE is_curated = TRUE
                ORDER BY landing_sites.id ASC;
                """
            )
            return list(cur.fetchall())


def get_nearest_psr_km(polar_x_m: float, polar_y_m: float) -> float | None:
    """Compute exact shortest distance (km) from south polar coordinates to psr_regions."""
    pool = get_pool()
    with pool.connection(timeout=5.0) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH pt AS (
                    SELECT ST_SetSRID(ST_MakePoint(%s, %s), 0) AS geom
                ),
                candidates AS (
                    SELECT p.geom
                    FROM psr_regions p, pt
                    ORDER BY p.geom <-> pt.geom
                    LIMIT 20
                )
                SELECT
                    ROUND((ST_Distance(c.geom, pt.geom) / 1000.0)::numeric, 3)::double precision AS distance_km
                FROM candidates c, pt
                ORDER BY ST_Distance(c.geom, pt.geom) ASC
                LIMIT 1;
                """,
                (polar_x_m, polar_y_m),
            )
            row = cur.fetchone()
            return float(row["distance_km"]) if row and row["distance_km"] is not None else None


def get_nearby_psrs(
    polar_x_m: float,
    polar_y_m: float,
    radius_km: float,
    limit: int = 50,
    simplify_m: float = 150.0,
    include_geometry: bool = False,
) -> list[dict[str, Any]]:
    """Return PSRs within radius_km (planar meters, SRID 0), optionally with simplified GeoJSON."""
    radius_m = float(radius_km) * 1000.0
    pool = get_pool()
    with pool.connection(timeout=5.0) as conn:
        with conn.cursor() as cur:
            if include_geometry:
                cur.execute(
                    """
                    WITH pt AS (
                        SELECT ST_SetSRID(ST_MakePoint(%s, %s), 0) AS geom
                    )
                    SELECT
                        p.gid,
                        p.name,
                        p.area_km2,
                        ROUND((ST_Distance(p.geom, pt.geom) / 1000.0)::numeric, 3)::double precision AS distance_km,
                        ST_AsGeoJSON(
                            CASE
                                WHEN %s > 0 THEN COALESCE(ST_SimplifyPreserveTopology(p.geom, %s), p.geom)
                                ELSE p.geom
                            END
                        ) AS geojson
                    FROM psr_regions p, pt
                    WHERE ST_DWithin(p.geom, pt.geom, %s)
                    ORDER BY ST_Distance(p.geom, pt.geom) ASC
                    LIMIT %s;
                    """,
                    (polar_x_m, polar_y_m, simplify_m, simplify_m, radius_m, limit),
                )
                rows = cur.fetchall()
                results: list[dict[str, Any]] = []
                for row in rows:
                    geojson_raw = row.pop("geojson", None)
                    row["geometry"] = json.loads(geojson_raw) if isinstance(geojson_raw, str) else geojson_raw
                    results.append(row)
                return results

            cur.execute(
                """
                WITH pt AS (
                    SELECT ST_SetSRID(ST_MakePoint(%s, %s), 0) AS geom
                )
                SELECT
                    p.gid,
                    p.name,
                    p.area_km2,
                    ROUND((ST_Distance(p.geom, pt.geom) / 1000.0)::numeric, 3)::double precision AS distance_km
                FROM psr_regions p, pt
                WHERE ST_DWithin(p.geom, pt.geom, %s)
                ORDER BY ST_Distance(p.geom, pt.geom) ASC
                LIMIT %s;
                """,
                (polar_x_m, polar_y_m, radius_m, limit),
            )
            return list(cur.fetchall())


def get_horizon_profile(coord_key: str) -> list[dict[str, Any]] | None:
    """Retrieve cached 360-azimuth horizon profile from horizon_profiles."""
    pool = get_pool()
    with pool.connection(timeout=3.0) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT profile_json FROM horizon_profiles WHERE coord_key = %s;",
                (coord_key,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return _decode_jsonb(row["profile_json"])


def save_horizon_profile(
    coord_key: str,
    lat: float,
    lon: float,
    profile: list[dict[str, Any]],
) -> None:
    """Persist computed 360-azimuth horizon profile into horizon_profiles."""
    pool = get_pool()
    with pool.connection(timeout=3.0) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO horizon_profiles (coord_key, lat, lon, profile_json)
                VALUES (%s, %s, %s, %s::jsonb)
                ON CONFLICT (coord_key) DO UPDATE
                SET profile_json = EXCLUDED.profile_json,
                    created_at = NOW();
                """,
                (coord_key, lat, lon, _json_dumps(profile)),
            )


def get_ephemeris_cache(cache_key: str) -> dict[str, Any] | None:
    """Retrieve cached mission ephemeris/visibility evaluation from ephemeris_cache."""
    pool = get_pool()
    with pool.connection(timeout=3.0) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT result_json FROM ephemeris_cache WHERE cache_key = %s;",
                (cache_key,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return _decode_jsonb(row["result_json"])


def save_ephemeris_cache(cache_key: str, result: dict[str, Any]) -> None:
    """Persist computed mission evaluation payload into ephemeris_cache."""
    pool = get_pool()
    with pool.connection(timeout=3.0) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ephemeris_cache (cache_key, result_json)
                VALUES (%s, %s::jsonb)
                ON CONFLICT (cache_key) DO UPDATE
                SET result_json = EXCLUDED.result_json,
                    created_at = NOW();
                """,
                (cache_key, _json_dumps(result)),
            )


def list_psrs(
    simplify_m: float = 500.0,
    limit: int = 500,
    offset: int = 0,
    min_area_km2: float = 0.0,
) -> tuple[int, list[dict[str, Any]]]:
    """Return (total, page) of PSR polygons ordered by gid, simplified for whole-pole views."""
    pool = get_pool()
    with pool.connection(timeout=5.0) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM psr_regions WHERE COALESCE(area_km2, 0) >= %s;",
                (min_area_km2,),
            )
            row = cur.fetchone()
            total = int(row["cnt"]) if row else 0
            cur.execute(
                """
                SELECT
                    p.gid,
                    p.name,
                    p.area_km2,
                    ST_X(ST_PointOnSurface(p.geom)) AS label_x_m,
                    ST_Y(ST_PointOnSurface(p.geom)) AS label_y_m,
                    ST_AsGeoJSON(
                        CASE
                            WHEN %s > 0 THEN COALESCE(ST_SimplifyPreserveTopology(p.geom, %s), p.geom)
                            ELSE p.geom
                        END
                    ) AS geojson
                FROM psr_regions p
                WHERE COALESCE(p.area_km2, 0) >= %s
                ORDER BY p.gid ASC
                LIMIT %s OFFSET %s;
                """,
                (simplify_m, simplify_m, min_area_km2, limit, offset),
            )
            rows = cur.fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        geojson_raw = row.pop("geojson", None)
        row["geometry"] = json.loads(geojson_raw) if isinstance(geojson_raw, str) else geojson_raw
        results.append(row)
    return total, results
