from math import radians, sin, cos, sqrt, atan2

MAX_RADIUS = 500  # meters

def haversine_meters(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Return great-circle distance in meters between two WGS-84 coordinates."""
    R = 6_371_000
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lng2 - lng1)
    a = sin(dphi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(dlambda / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))
