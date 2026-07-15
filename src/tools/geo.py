"""Pure spatial helpers — no I/O, fully unit-testable.

Includes:
- ``haversine_km``: great-circle distance between two lat/lon points.
- ``point_in_polygon``: ray-casting point-in-polygon test used to check
  whether a POI lies inside a Valhalla isochrone polygon.
- ``parse_within``: parse a natural-language budget string such as
  ``"50 km"``, ``"1 hour"``, ``"45 minutes"`` into a typed budget.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence

# WGS-84 mean Earth radius, km.
_EARTH_RADIUS_KM = 6371.0088
_MILES_PER_KM = 0.621371

BudgetKind = Literal["distance", "time"]


@dataclass(frozen=True)
class Budget:
    """A parsed proximity budget.

    ``kind == "distance"`` → ``value`` is kilometres, ``time_seconds`` is None.
    ``kind == "time"`` → ``value`` is seconds, ``km`` is None.
    """

    kind: BudgetKind
    value: float
    raw: str

    @property
    def km(self) -> float | None:
        return self.value if self.kind == "distance" else None

    @property
    def time_seconds(self) -> float | None:
        return self.value if self.kind == "time" else None

    @property
    def human(self) -> str:
        if self.kind == "distance":
            return f"{self.value:g} km"
        minutes = self.value / 60.0
        if minutes >= 60:
            hours = minutes / 60.0
            return f"{hours:g} hour{'s' if hours != 1 else ''}"
        return f"{minutes:g} minute{'s' if minutes != 1 else ''}"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS-84 points in kilometres.

    Examples:
        >>> round(haversine_km(49.45, 11.08, 49.45, 11.08), 3)
        0.0
        >>> # Nuremberg -> Wiesloch (~230 km crow-flies)
        >>> 200 < haversine_km(49.45, 11.08, 49.30, 8.71) < 260
        True
    """
    rlat1, rlon1, rlat2, rlon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2
    )
    return 2 * _EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def point_in_polygon(lat: float, lon: float, ring: Sequence[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test.

    ``ring`` is an ordered sequence of ``(lat, lon)`` tuples forming a closed
    polygon (the closing point need not repeat the first). The classic
    even-odd algorithm operating on lon/lat coordinates directly — for the
    scales of isochrone polygons (typically a few hundred km across) this is
    more than accurate enough; we are not computing geodesic area.
    """
    if len(ring) < 3:
        return False

    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        yi, xi = ring[i][0], ring[i][1]  # lat, lon
        yj, xj = ring[j][0], ring[j][1]
        # Standard PIP test: does a horizontal ray from (lon, lat) cross
        # the edge (i, j)? We treat lon as the x-axis, lat as the y-axis.
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside


def polygon_contains_point(
    lat: float, lon: float, polygon: Iterable[Sequence[tuple[float, float]]]
) -> bool:
    """Point-in-polygon against a polygon that may have multiple rings.

    The first ring is the outer boundary; subsequent rings are holes. A point
    is inside the polygon iff it is inside ring 0 and not inside any hole.
    """
    rings = list(polygon)
    if not rings:
        return False
    if not point_in_polygon(lat, lon, rings[0]):
        return False
    for hole in rings[1:]:
        if point_in_polygon(lat, lon, hole):
            return False
    return True


# Regexes for parse_within. Order matters: longer/more-specific first.
_DIST_KM_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(km|kilometer[s]?|kilometre[s]?)\b",
    re.IGNORECASE,
)
_DIST_MI_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(mi|mile[s]?)\b", re.IGNORECASE
)
_TIME_H_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(h|hr|hour[s]?)\b", re.IGNORECASE
)
_TIME_MIN_RE = re.compile(
    r"^\s*([0-9]+(?:\.[0-9]+)?)\s*(m|min|minute[s]?)\b", re.IGNORECASE
)


def parse_within(within: str) -> Budget:
    """Parse a natural-language proximity budget into a :class:`Budget`.

    Accepted forms (case-insensitive, whitespace-tolerant):

    Distance: ``"50 km"``, ``"50km"``, ``"30 miles"``, ``"30 mi"``
    Time:     ``"1 hour"``, ``"1h"``, ``"45 minutes"``, ``"45 min"``, ``"30m"``

    Raises:
        ValueError: if the string cannot be parsed.

    Examples:
        >>> parse_within("50 km").km
        50.0
        >>> parse_within("1 hour").time_seconds
        3600.0
        >>> parse_within("45 min").human
        '45 minutes'
    """
    if not within or not within.strip():
        raise ValueError("empty budget string")

    if m := _DIST_KM_RE.match(within):
        return Budget(kind="distance", value=float(m.group(1)), raw=within)
    if m := _DIST_MI_RE.match(within):
        return Budget(kind="distance", value=float(m.group(1)) / _MILES_PER_KM, raw=within)
    if m := _TIME_H_RE.match(within):
        return Budget(kind="time", value=float(m.group(1)) * 3600.0, raw=within)
    if m := _TIME_MIN_RE.match(within):
        return Budget(kind="time", value=float(m.group(1)) * 60.0, raw=within)

    raise ValueError(
        f"could not parse proximity budget {within!r}; "
        "expected e.g. '50 km', '30 miles', '1 hour', '45 min'"
    )
