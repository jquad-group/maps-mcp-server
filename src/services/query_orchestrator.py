"""High-level query orchestration for the maps MCP tools.

The MCP tool functions in :mod:`src.server` are thin wrappers around the
functions defined here, which compose :class:`NominatimClient`,
:class:`ValhallaClient`, :class:`POIStore` and the pure helpers in
:mod:`src.tools.geo`. Keeping the orchestration separate from the FastMCP
plumbing makes it directly unit-testable with fake clients.

Key correctness point — ``find_within`` distinguishes two budget kinds:

* **time** ("1 hour"): Valhalla isochrone polygon + point-in-polygon, with
  a parallel one-to-many matrix call so each match also reports its actual
  driving duration.
* **distance** ("50 km"): a single one-to-many Valhalla matrix (road
  distance — what users mean), with a ``metric="crow"`` escape hatch that
  falls back to great-circle :func:`haversine_km`.

Every ``find_within`` query therefore costs at most **one** Valhalla round
trip (matrix) plus optionally one isochrone round trip, regardless of how
many POIs are evaluated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from src.services.nominatim_client import GeocodeResult, NominatimClient
from src.services.poi_store import POI, POIStore
from src.services.valhalla_client import MatrixCell, ValhallaClient, ValhallaError
from src.tools.geo import (
    Budget,
    haversine_km,
    parse_within,
    polygon_contains_point,
)

logger = logging.getLogger(__name__)

LocationLike = str | dict[str, float]
Metric = Literal["road", "crow"]


@dataclass(frozen=True)
class Match:
    """A POI that satisfied a proximity query, with its measured cost."""

    poi: POI
    distance_km: float | None
    time_seconds: float | None
    inside_isochrone: bool | None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.poi.name,
            "lat": self.poi.lat,
            "lon": self.poi.lon,
            "address": self.poi.address,
            "source_url": self.poi.source_url,
        }
        if self.distance_km is not None:
            d["distance_km"] = round(self.distance_km, 1)
        if self.time_seconds is not None:
            d["time_seconds"] = int(self.time_seconds)
            d["time_minutes"] = round(self.time_seconds / 60.0, 1)
        if self.inside_isochrone is not None:
            d["inside_isochrone"] = self.inside_isochrone
        return d


async def aresolve_location(
    loc: LocationLike, *, nominatim: NominatimClient
) -> GeocodeResult:
    """Resolve a place name or a ``{lat, lon}`` dict to coordinates.

    Dicts (``{"lat": .., "lon": ..}``) pass through unchanged; strings are
    geocoded via Nominatim.
    """
    if isinstance(loc, dict):
        lat = float(loc["lat"])
        lon = float(loc["lon"])
        return GeocodeResult(lat=lat, lon=lon, display_name=f"{lat}, {lon}")
    return await nominatim.geocode(loc)


async def find_within(
    center: GeocodeResult,
    within: str,
    candidates: list[POI],
    *,
    valhalla: ValhallaClient,
    metric: Metric = "road",
    costing: str = "auto",
    max_matrix_size: int = 50,
) -> dict[str, Any]:
    """Filter ``candidates`` to those within ``within`` of ``center``.

    Args:
        center: Already-resolved center coordinates.
        within: Natural-language budget ("50 km", "1 hour", ...).
        candidates: POIs to evaluate.
        valhalla: Valhalla client for matrix / isochrone calls.
        metric: ``"road"`` (default, Valhalla distance) or ``"crow"``
            (great-circle). Only affects distance budgets.
        costing: Valhalla costing mode (auto, bicycle, pedestrian, ...).
        max_matrix_size: Max destinations per matrix call; larger candidate
            lists are chunked.

    Returns:
        Dict with ``budget``, ``center``, ``matches`` (sorted nearest-first),
        ``total_evaluated``, and ``metric``.
    """
    budget: Budget = parse_within(within)
    result: dict[str, Any] = {
        "budget": {"kind": budget.kind, "human": budget.human, "raw": budget.raw},
        "center": center.to_dict(),
        "metric": metric if budget.kind == "distance" else "road",
        "total_evaluated": len(candidates),
    }
    if not candidates:
        result["matches"] = []
        return result

    if budget.kind == "distance":
        matches = await _filter_by_distance(
            center, budget, candidates, valhalla=valhalla,
            metric=metric, costing=costing, max_matrix_size=max_matrix_size,
        )
    else:
        matches = await _filter_by_time(
            center, budget, candidates, valhalla=valhalla, costing=costing,
            max_matrix_size=max_matrix_size,
        )

    # Sort: time budgets by duration, distance budgets by distance.
    if budget.kind == "time":
        matches.sort(key=lambda m: m.time_seconds or float("inf"))
    else:
        matches.sort(key=lambda m: m.distance_km or float("inf"))

    result["matches"] = [m.to_dict() for m in matches]
    result["match_count"] = len(matches)
    return result


async def _filter_by_distance(
    center: GeocodeResult,
    budget: Budget,
    candidates: list[POI],
    *,
    valhalla: ValhallaClient,
    metric: Metric,
    costing: str,
    max_matrix_size: int,
) -> list[Match]:
    """Distance budget: one matrix call (road) or haversine (crow)."""
    budget_km = budget.km or 0.0

    if metric == "crow":
        out: list[Match] = []
        for poi in candidates:
            d = haversine_km(center.lat, center.lon, poi.lat, poi.lon)
            if d <= budget_km:
                out.append(Match(poi, distance_km=d, time_seconds=None, inside_isochrone=None))
        return out

    # Road distance via chunked Valhalla matrix — fall back to crow if
    # Valhalla is unavailable (not deployed, timeout, etc.).
    try:
        out = []
        for chunk in _chunked(candidates, max_matrix_size):
            matrix = await valhalla.sources_to_targets(
                sources=[(center.lat, center.lon)],
                targets=[p.coord for p in chunk],
                costing=costing,
            )
            for poi, row in zip(chunk, matrix):
                if not row:
                    continue
                cell: MatrixCell = row[0]
                if cell.distance_km <= budget_km:
                    out.append(
                        Match(
                            poi,
                            distance_km=cell.distance_km,
                            time_seconds=cell.time_seconds,
                            inside_isochrone=None,
                        )
                    )
        return out
    except Exception as exc:
        # Valhalla unavailable — fall back to great-circle (crow) distance.
        import logging
        logging.getLogger(__name__).warning(
            "Valhalla road-distance failed (%s) — falling back to crow distance", exc)
        out = []
        for poi in candidates:
            d = haversine_km(center.lat, center.lon, poi.lat, poi.lon)
            if d <= budget_km:
                out.append(Match(poi, distance_km=d, time_seconds=None, inside_isochrone=None))
        return out


async def _filter_by_time(
    center: GeocodeResult,
    budget: Budget,
    candidates: list[POI],
    *,
    valhalla: ValhallaClient,
    costing: str,
    max_matrix_size: int,
) -> list[Match]:
    """Time budget: isochrone polygon membership + matrix-reported duration."""
    minutes = int(round((budget.time_seconds or 0.0) / 60.0))
    if minutes <= 0:
        return []

    iso = await valhalla.isochrone(
        center.lat, center.lon, minutes=minutes, costing=costing
    )
    rings_latlon = _isochrone_rings_to_latlon(iso)

    # Point-in-polygon pass first (cheap, no extra requests).
    inside_pois = [
        p for p in candidates
        if polygon_contains_point(p.lat, p.lon, rings_latlon)
    ]

    if not inside_pois:
        return []

    # Matrix call for actual durations (so we can say "43 min away").
    durations: dict[str, float] = {}
    for chunk in _chunked(inside_pois, max_matrix_size):
        matrix = await valhalla.sources_to_targets(
            sources=[(center.lat, center.lon)],
            targets=[p.coord for p in chunk],
            costing=costing,
        )
        for poi, row in zip(chunk, matrix):
            if row:
                durations[poi.place_id] = row[0].time_seconds

    out: list[Match] = []
    for poi in inside_pois:
        out.append(
            Match(
                poi,
                distance_km=None,
                time_seconds=durations.get(poi.place_id),
                inside_isochrone=True,
            )
        )
    return out


def _isochrone_rings_to_latlon(iso_json: dict[str, Any]) -> list[list[tuple[float, float]]]:
    """Extract polygon rings from a Valhalla isochrone response.

    Valhalla (polygons=true) returns GeoJSON FeatureCollection; each
    feature geometry is a Polygon with ``coordinates`` = list of rings,
    each ring a list of ``[lon, lat]``. We convert to ``(lat, lon)`` tuples
    for :func:`polygon_contains_point`.
    """
    rings: list[list[tuple[float, float]]] = []
    for feature in iso_json.get("features", []):
        geom = feature.get("geometry", {})
        coords = geom.get("coordinates", [])
        if geom.get("type") == "Polygon":
            for ring in coords:
                rings.append([(pt[1], pt[0]) for pt in ring])  # lon,lat -> lat,lon
        elif geom.get("type") == "MultiPolygon":
            for poly in coords:
                for ring in poly:
                    rings.append([(pt[1], pt[0]) for pt in ring])
    return rings


def _chunked(items: list[POI], size: int):
    """Yield successive ``size``-sized chunks of ``items``."""
    if size <= 0:
        size = len(items) or 1
    for i in range(0, len(items), size):
        yield items[i : i + size]
