"""Lunar South Pole coordinate transformations and 3D vector utilities.

Projection CRS matches NASA LOLA PGDA South Polar products (e.g. LDEM_80S_80MPP_ADJ.TIF):
    PROJCS["Moon (2015) - Sphere / Ocentric / South Polar",
        GEOGCS["Moon (2015) - Sphere / Ocentric",
            DATUM["Moon (2015) - Sphere", SPHEROID["Moon (2015) - Sphere", 1737400, 0]],
            PRIMEM["Reference Meridian", 0]],
        PROJECTION["Polar_Stereographic"],
        PARAMETER["latitude_of_origin", -90],
        PARAMETER["central_meridian", 0],
        PARAMETER["false_easting", 0],
        PARAMETER["false_northing", 0],
        UNIT["metre", 1]]

Axis conventions:
- South Polar Stereographic (polar_x_m, polar_y_m):
    Origin (0, 0) is the Lunar South Pole (-90 deg).
    +X points toward 90 deg E longitude.
    +Y points toward 0 deg longitude (mean sub-Earth prime meridian).
- Local 3D Three.js tangent frame returned by az_el_to_vector_3d(az_deg, el_deg):
    Right-handed frame centered at the landing site:
    +X = local East (az = 90 deg)
    +Y = local Zenith / Up (el = +90 deg)
    -Z = local North (az = 0 deg), so +Z = local South (az = 180 deg)
    Formula:
        x = cos(el) * sin(az)
        y = sin(el)
        z = -cos(el) * cos(az)
"""

from __future__ import annotations

import math

MOON_RADIUS_M = 1_737_400.0


def normalize_lon_deg(lon: float) -> float:
    """Normalize planetocentric East longitude in degrees into [0.0, 360.0)."""
    val = float(lon)
    if not math.isfinite(val):
        raise ValueError(f"Longitude must be finite, got {lon!r}")
    norm = val % 360.0
    if norm >= 360.0 or norm == -0.0 or abs(norm - 360.0) < 1e-12:
        norm = 0.0
    if abs(norm) < 1e-12:
        norm = 0.0
    return float(norm)


def validate_lat_lon(lat: float, lon: float) -> tuple[float, float]:
    """Validate planetocentric latitude in [-90, 90] and normalize longitude."""
    lat_f = float(lat)
    if not math.isfinite(lat_f):
        raise ValueError(f"Latitude must be finite, got {lat!r}")
    if lat_f < -90.0 - 1e-9 or lat_f > 90.0 + 1e-9:
        raise ValueError(f"Latitude must be within [-90, 90] degrees, got {lat!r}")
    lat_clamped = max(-90.0, min(90.0, lat_f))
    return lat_clamped, normalize_lon_deg(lon)


def polar_stereographic_from_latlon(lat: float, lon: float) -> tuple[float, float]:
    """Convert lunar planetocentric (lat, lon) to South Polar Stereographic (polar_x_m, polar_y_m).

    Uses spherical Moon radius R = 1,737,400.0 m, lat_0 = -90 deg, lon_0 = 0 deg, k_0 = 1.0,
    where +X is 90 deg E and +Y is 0 deg prime meridian.
    """
    lat_c, lon_norm = validate_lat_lon(lat, lon)
    if lat_c >= 90.0:
        raise ValueError("North Pole (lat = +90 deg) is singular in South Polar Stereographic projection")

    half_colat_rad = math.radians((90.0 + lat_c) * 0.5)
    rho = 2.0 * MOON_RADIUS_M * math.tan(half_colat_rad)
    lon_rad = math.radians(lon_norm)
    x_m = rho * math.sin(lon_rad)
    y_m = rho * math.cos(lon_rad)
    return (0.0 if x_m == 0.0 else float(x_m), 0.0 if y_m == 0.0 else float(y_m))


def latlon_from_polar_stereographic(x_m: float, y_m: float) -> tuple[float, float]:
    """Convert South Polar Stereographic (x_m, y_m) to (lat, lon_east_0_360)."""
    x = float(x_m)
    y = float(y_m)
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError(f"Stereographic coordinates must be finite, got ({x_m!r}, {y_m!r})")

    rho = math.hypot(x, y)
    if rho == 0.0:
        return (-90.0, 0.0)

    lat_rad = -0.5 * math.pi + 2.0 * math.atan(rho / (2.0 * MOON_RADIUS_M))
    lat_deg = math.degrees(lat_rad)
    lon_deg = normalize_lon_deg(math.degrees(math.atan2(x, y)))
    return (float(lat_deg), float(lon_deg))


def polar_stereographic_scale_factor(lat: float) -> float:
    """Return conformal scale factor k >= 1.0 (map meters per true ground meter) at latitude."""
    lat_c, _ = validate_lat_lon(lat, 0.0)
    if lat_c >= 90.0:
        raise ValueError("Scale factor is undefined at +90 deg")
    cos_half = math.cos(math.radians((90.0 + lat_c) * 0.5))
    return 1.0 / (cos_half * cos_half)


def local_enu_basis_in_polar(lon: float) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return local (East, North) 2D unit vectors in South Polar Stereographic (X, Y) coordinates."""
    lon_rad = math.radians(normalize_lon_deg(lon))
    east_xy = (math.cos(lon_rad), -math.sin(lon_rad))
    north_xy = (math.sin(lon_rad), math.cos(lon_rad))
    return east_xy, north_xy


def az_el_to_vector_3d(az_deg: float, el_deg: float) -> list[float]:
    """Convert topocentric (az_deg, el_deg) to a Three.js unit vector [x, y, z].

    Convention:
        az_deg: azimuth clockwise from local North (0=N, 90=E, 180=S, 270=W)
        el_deg: elevation above flat reference plane (-90..+90)
        Three.js output frame: +X = East, +Y = Up (Zenith), -Z = North (+Z = South).
    """
    az_f = float(az_deg)
    el_f = float(el_deg)
    if not (math.isfinite(az_f) and math.isfinite(el_f)):
        raise ValueError(f"az_deg and el_deg must be finite, got ({az_deg!r}, {el_deg!r})")

    az_rad = math.radians(az_f)
    el_rad = math.radians(el_f)
    cos_el = math.cos(el_rad)
    x = cos_el * math.sin(az_rad)
    y = math.sin(el_rad)
    z = -cos_el * math.cos(az_rad)

    norm = math.sqrt(x * x + y * y + z * z)
    if norm > 0.0:
        x /= norm
        y /= norm
        z /= norm

    return [
        0.0 if abs(x) < 1e-15 else float(x),
        0.0 if abs(y) < 1e-15 else float(y),
        0.0 if abs(z) < 1e-15 else float(z),
    ]
