"""Core constants for lunar calculations and API constraints."""

# Lunar Physical Constants
MOON_RADIUS_KM: float = 1737.4
MOON_RADIUS_M: float = 1737400.0
SOLAR_CONSTANT_W_M2: float = 1361.0
MAX_SAFE_SLOPE_DEG: float = 10.0

# Geographic Coordinate Boundaries (Lunar South Pole)
SOUTH_POLE_MIN_LAT: float = -90.0
SOUTH_POLE_MAX_LAT: float = -80.0
MIN_LON: float = -180.0
MAX_LON: float = 360.0

# Terrain Patch Constraints
ALLOWED_RESOLUTIONS: tuple[int, ...] = (64, 128, 256)
DEFAULT_RESOLUTION: int = 128
MIN_SIZE_KM: float = 2.0
MAX_SIZE_KM: float = 40.0
DEFAULT_SIZE_KM: float = 12.0

# Operational Evaluation Constraints
DEFAULT_PANEL_EFFICIENCY: float = 0.28
DEFAULT_PANEL_AREA_M2: float = 4.0
MIN_DURATION_DAYS: float = 0.5
MAX_DURATION_DAYS: float = 30.0
DEFAULT_DURATION_DAYS: float = 14.0
MIN_STEP_MINUTES: int = 10
MAX_STEP_MINUTES: int = 360
DEFAULT_STEP_MINUTES: int = 60

# Scenario Comparison Constraints
MIN_SCENARIOS: int = 2
MAX_SCENARIOS: int = 5

# PSR Query Defaults
DEFAULT_PSR_RADIUS_KM: float = 25.0
MAX_PSR_RADIUS_KM: float = 200.0
DEFAULT_PSR_LIMIT: int = 50
MAX_PSR_LIMIT: int = 500
DEFAULT_PSR_SIMPLIFY_M: float = 150.0
