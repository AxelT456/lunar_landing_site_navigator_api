"""Domain layer for lunar calculations and spatial projections."""

import math
from app.core.constants import MOON_RADIUS_M


def normalize_longitude(lon: float) -> float:
    """Normalize longitude to [-180, 180) degrees."""
    lon = lon % 360.0
    if lon >= 180.0:
        lon -= 360.0
    return lon


def lat_lon_to_polar_xy(lat: float, lon: float) -> tuple[float, float]:
    """
    Project lunar latitude/longitude to South Pole stereographic (x, y) coordinates in meters.

    Matches standard Lunar South Pole Stereographic projection (center at -90 deg lat).
    """
    # Colatitude in radians: 90 - |lat| for southern hemisphere
    colat_deg = 90.0 + lat  # since lat is negative (-90 to -80)
    colat_rad = math.radians(colat_deg)

    # Stereographic projection radial distance
    # r = 2 * R * tan(colat / 2)
    r = 2.0 * MOON_RADIUS_M * math.tan(colat_rad / 2.0)

    # Standard lunar south pole orientation: X points east along 90 E, Y along 0 E
    lon_rad = math.radians(lon)
    # Using orientation matching contract sample: theta = lon - 90 deg
    theta = lon_rad - math.pi / 2.0
    x = round(r * math.cos(theta), 1)
    y = round(r * math.sin(theta), 1)
    return x, y
