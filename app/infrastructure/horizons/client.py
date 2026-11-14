"""NASA JPL Horizons API client with SQLite caching and analytical fallback.

Provides REST queries to NASA JPL Horizons system for high-precision lunar
ephemerides with transparent fallback to the domain analytical ephemeris engine
during network partitions or offline operations.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from app.domain.services.ephemeris import (
    CelestialPosition,
    EphemerisService,
    az_el_to_visor_vector,
)

logger = logging.getLogger(__name__)

HORIZONS_API_URL: str = "https://ssd.jpl.nasa.gov/api/horizons.api"


@dataclass
class HorizonsRecord:
    """Ephemeris record from Horizons or fallback engine."""

    timestamp_utc: str
    target: str
    azimuth_deg: float
    elevation_deg: float
    vector_3d: List[float]
    source: str  # "jpl_horizons" or "analytical_fallback"


class HorizonsClient:
    """Client for NASA JPL Horizons API with SQLite caching and analytical fallback."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        ephemeris_service: Optional[EphemerisService] = None,
        timeout_seconds: float = 4.0,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.ephemeris_service = ephemeris_service or EphemerisService()
        self.memory_cache: Dict[str, List[HorizonsRecord]] = {}

        if db_path is None:
            data_dir = Path("data")
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
                self.db_path = str(data_dir / "horizons_cache.sqlite")
            except OSError:
                self.db_path = ":memory:"
        else:
            self.db_path = db_path

        self._init_sqlite()

    def _init_sqlite(self) -> None:
        """Initialize SQLite caching schema."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS ephemeris_cache (
                        cache_key TEXT PRIMARY KEY,
                        target TEXT NOT NULL,
                        lat REAL NOT NULL,
                        lon REAL NOT NULL,
                        start_utc TEXT NOT NULL,
                        stop_utc TEXT NOT NULL,
                        step_minutes INTEGER NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.warning("Could not initialize SQLite Horizons cache (%s); using in-memory only", e)
            self.db_path = ":memory:"

    def _generate_cache_key(
        self,
        target: str,
        lat: float,
        lon: float,
        start_utc: datetime,
        stop_utc: datetime,
        step_minutes: int,
    ) -> str:
        """Generate deterministic cache key."""
        return (
            f"{target.lower()}:{lat:.3f}:{lon:.3f}:"
            f"{start_utc.isoformat()}:{stop_utc.isoformat()}:{step_minutes}"
        )

    def _get_from_cache(self, cache_key: str) -> Optional[List[HorizonsRecord]]:
        """Retrieve cached records from memory or SQLite."""
        if cache_key in self.memory_cache:
            return self.memory_cache[cache_key]

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT payload_json FROM ephemeris_cache WHERE cache_key = ?",
                    (cache_key,),
                )
                row = cursor.fetchone()
                if row:
                    raw_list = json.loads(row[0])
                    records = [HorizonsRecord(**item) for item in raw_list]
                    self.memory_cache[cache_key] = records
                    return records
        except (sqlite3.Error, json.JSONDecodeError) as e:
            logger.debug("Cache read error for key %s: %s", cache_key, e)

        return None

    def _save_to_cache(self, cache_key: str, records: List[HorizonsRecord], target: str, lat: float, lon: float, start_utc: datetime, stop_utc: datetime, step_minutes: int) -> None:
        """Store records in memory and SQLite cache."""
        self.memory_cache[cache_key] = records
        try:
            payload_json = json.dumps([asdict(r) for r in records])
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT OR REPLACE INTO ephemeris_cache
                    (cache_key, target, lat, lon, start_utc, stop_utc, step_minutes, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        cache_key,
                        target,
                        lat,
                        lon,
                        start_utc.isoformat(),
                        stop_utc.isoformat(),
                        step_minutes,
                        payload_json,
                    ),
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.debug("Cache save error for key %s: %s", cache_key, e)

    def _fallback_analytical(
        self,
        target: str,
        lat: float,
        lon: float,
        start_utc: datetime,
        stop_utc: datetime,
        step_minutes: int,
    ) -> List[HorizonsRecord]:
        """Generate ephemeris using analytical domain service."""
        from datetime import timedelta

        records: List[HorizonsRecord] = []
        duration_minutes = max(1, int((stop_utc - start_utc).total_seconds() / 60.0))
        steps = max(1, duration_minutes // step_minutes)

        for i in range(steps):
            current_time = start_utc + timedelta(minutes=i * step_minutes)
            snapshot = self.ephemeris_service.get_snapshot(lat, lon, current_time)
            pos: CelestialPosition = snapshot.sun if target.lower() in ["sun", "10"] else snapshot.earth

            records.append(
                HorizonsRecord(
                    timestamp_utc=current_time.isoformat(),
                    target=target,
                    azimuth_deg=pos.azimuth_deg,
                    elevation_deg=pos.elevation_deg,
                    vector_3d=pos.vector_3d,
                    source="analytical_fallback",
                )
            )
        return records

    async def get_ephemeris(
        self,
        target: str,
        lat: float,
        lon: float,
        start_utc: datetime,
        stop_utc: datetime,
        step_minutes: int = 60,
    ) -> List[HorizonsRecord]:
        """
        Query Horizons ephemeris for given target body with automatic fallback.

        Args:
            target: "sun" (or "10") or "earth" (or "399").
            lat: Lunar observer latitude (-90 to -80).
            lon: Lunar observer longitude (-180 to 360).
            start_utc: Window start UTC.
            stop_utc: Window end UTC.
            step_minutes: Sampling step size.

        Returns:
            List of HorizonsRecord objects.
        """
        if start_utc.tzinfo is None:
            start_utc = start_utc.replace(tzinfo=timezone.utc)
        if stop_utc.tzinfo is None:
            stop_utc = stop_utc.replace(tzinfo=timezone.utc)

        cache_key = self._generate_cache_key(
            target, lat, lon, start_utc, stop_utc, step_minutes
        )
        cached = self._get_from_cache(cache_key)
        if cached:
            return cached

        # Attempt query to NASA JPL Horizons REST API
        target_code = "10" if target.lower() in ["sun", "10"] else "399"
        params = {
            "format": "json",
            "COMMAND": f"'{target_code}'",
            "EPHEM_TYPE": "OBSERVER",
            "CENTER": "coord@301",
            "COORD_TYPE": "GEODETIC",
            "SITE_COORD": f"'{lon:.4f},{lat:.4f},0.0'",
            "START_TIME": f"'{start_utc.strftime('%Y-%m-%d %H:%M')}'",
            "STOP_TIME": f"'{stop_utc.strftime('%Y-%m-%d %H:%M')}'",
            "STEP_SIZE": f"'{step_minutes}m'",
            "QUANTITIES": "'4'",  # 4 = Apparent azimuth and elevation
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                resp = await client.get(HORIZONS_API_URL, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    parsed = self._parse_horizons_response(data, target)
                    if parsed:
                        self._save_to_cache(
                            cache_key, parsed, target, lat, lon, start_utc, stop_utc, step_minutes
                        )
                        return parsed
        except Exception as e:
            logger.info("Horizons API query unavailable (%s); using analytical fallback", e)

        # Fallback to analytical domain engine
        fallback_records = self._fallback_analytical(
            target, lat, lon, start_utc, stop_utc, step_minutes
        )
        self._save_to_cache(
            cache_key, fallback_records, target, lat, lon, start_utc, stop_utc, step_minutes
        )
        return fallback_records

    def get_ephemeris_sync(
        self,
        target: str,
        lat: float,
        lon: float,
        start_utc: datetime,
        stop_utc: datetime,
        step_minutes: int = 60,
    ) -> List[HorizonsRecord]:
        """Synchronous query with cache and analytical fallback."""
        if start_utc.tzinfo is None:
            start_utc = start_utc.replace(tzinfo=timezone.utc)
        if stop_utc.tzinfo is None:
            stop_utc = stop_utc.replace(tzinfo=timezone.utc)

        cache_key = self._generate_cache_key(
            target, lat, lon, start_utc, stop_utc, step_minutes
        )
        cached = self._get_from_cache(cache_key)
        if cached:
            return cached

        # Sync fallback
        fallback_records = self._fallback_analytical(
            target, lat, lon, start_utc, stop_utc, step_minutes
        )
        self._save_to_cache(
            cache_key, fallback_records, target, lat, lon, start_utc, stop_utc, step_minutes
        )
        return fallback_records

    def _parse_horizons_response(self, data: Dict[str, Any], target: str) -> Optional[List[HorizonsRecord]]:
        """Parse raw Horizons JSON output text between $$SOE and $$EOE markers."""
        try:
            result_text = data.get("result", "")
            if "$$SOE" not in result_text or "$$EOE" not in result_text:
                return None

            ephem_block = result_text.split("$$SOE")[1].split("$$EOE")[0].strip()
            lines = [line.strip() for line in ephem_block.splitlines() if line.strip()]

            records: List[HorizonsRecord] = []
            for line in lines:
                parts = line.split()
                if len(parts) >= 4:
                    # Format: Date, Time, Azimuth, Elevation
                    date_str = parts[0]
                    time_str = parts[1]
                    az = float(parts[2])
                    el = float(parts[3])
                    v3d = az_el_to_visor_vector(az, el, round_digits=3)
                    ts = f"{date_str}T{time_str}:00Z"
                    records.append(
                        HorizonsRecord(
                            timestamp_utc=ts,
                            target=target,
                            azimuth_deg=round(az, 2),
                            elevation_deg=round(el, 2),
                            vector_3d=v3d,
                            source="jpl_horizons",
                        )
                    )
            return records if records else None
        except Exception as e:
            logger.debug("Failed parsing Horizons response: %s", e)
            return None
