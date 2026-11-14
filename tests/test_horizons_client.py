"""Unit tests for NASA JPL Horizons client with caching and fallback."""

from datetime import datetime, timezone
import pytest

from app.infrastructure.horizons.client import HorizonsClient, HorizonsRecord


@pytest.mark.anyio
async def test_horizons_client_fallback_when_offline() -> None:
    """Test that Horizons client transparently falls back to analytical domain service."""
    client = HorizonsClient(db_path=":memory:", timeout_seconds=0.1)

    start_utc = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    stop_utc = datetime(2026, 12, 1, 6, 0, 0, tzinfo=timezone.utc)

    records = await client.get_ephemeris(
        target="sun",
        lat=-89.66,
        lon=222.7,
        start_utc=start_utc,
        stop_utc=stop_utc,
        step_minutes=60,
    )

    assert len(records) == 6
    assert all(isinstance(r, HorizonsRecord) for r in records)
    assert records[0].target == "sun"
    assert records[0].source == "analytical_fallback"
    assert len(records[0].vector_3d) == 3


@pytest.mark.anyio
async def test_horizons_client_sqlite_cache_hit() -> None:
    """Test that subsequent requests hit SQLite cache."""
    client = HorizonsClient(db_path=":memory:", timeout_seconds=0.1)

    start_utc = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    stop_utc = datetime(2026, 12, 1, 4, 0, 0, tzinfo=timezone.utc)

    # First call - populates cache
    rec1 = await client.get_ephemeris("earth", -89.66, 222.7, start_utc, stop_utc, 60)
    # Clear in-memory cache to force reading from SQLite
    client.memory_cache.clear()

    # Second call - retrieves from SQLite
    rec2 = await client.get_ephemeris("earth", -89.66, 222.7, start_utc, stop_utc, 60)

    assert len(rec1) == len(rec2)
    assert rec1[0].azimuth_deg == rec2[0].azimuth_deg
    assert rec1[0].elevation_deg == rec2[0].elevation_deg


def test_horizons_client_sync_fallback() -> None:
    """Test synchronous fallback method."""
    client = HorizonsClient(db_path=":memory:", timeout_seconds=0.1)
    start_utc = datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    stop_utc = datetime(2026, 12, 1, 3, 0, 0, tzinfo=timezone.utc)

    records = client.get_ephemeris_sync("sun", -89.0, 0.0, start_utc, stop_utc, 60)
    assert len(records) == 3
    assert records[0].source == "analytical_fallback"
