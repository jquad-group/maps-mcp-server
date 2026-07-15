"""Async client for a self-hosted Valhalla routing engine.

Valhalla exposes JSON actions over HTTP POST at ``/`` (e.g. ``route``,
``isochrone``, ``sources_to_targets``). This client wraps the three actions
we need for the maps MCP tools.

Reference: https://valhalla.github.io/valhalla/api/
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

import httpx

logger = logging.getLogger(__name__)


class ValhallaError(RuntimeError):
    """Raised when Valhalla is unreachable or returns an error."""


@dataclass(frozen=True)
class MatrixCell:
    """One cell of a time-distance matrix."""

    distance_km: float
    time_seconds: float


def _ll(lat: float, lon: float) -> dict[str, float]:
    """Valhalla coordinate dict."""
    return {"lat": lat, "lon": lon}


class ValhallaClient:
    """Thin async wrapper around the Valhalla HTTP API.

    Args:
        base_url: Valhalla base URL, no trailing slash (e.g.
            ``http://valhalla:8002``).
        timeout: Total request timeout in seconds.
        connect_timeout: Connect timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout, connect=connect_timeout)

    async def _call(self, action: str, json_body: dict[str, Any]) -> dict[str, Any]:
        """POST a JSON payload to ``/{action}`` and return the parsed body."""
        url = f"{self.base_url}/{action}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(url, json=json_body)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise ValhallaError(
                    f"Valhalla {action} request failed: {exc}"
                ) from exc
            try:
                return resp.json()
            except ValueError as exc:
                raise ValhallaError(
                    f"Valhalla {action} returned non-JSON: {exc}"
                ) from exc

    async def isochrone(
        self,
        lat: float,
        lon: float,
        *,
        minutes: int,
        costing: str = "auto",
    ) -> dict[str, Any]:
        """Compute a single isochrone contour of ``minutes`` around a point.

        Returns the raw Valhalla ``isochrone`` GeoJSON-shaped response. The
        relevant data lives under ``["features"][0]["geometry"]["coordinates"]``
        (a list of rings, each a list of ``[lon, lat]`` pairs).
        """
        body = {
            "costing": costing,
            "costing_options": {"auto": {"use_highways": 1}},
            "polygons": True,
            "generalize": 50,
            "show": "contours",
            "contours": [{"time": minutes, "color": "ff0000"}],
            "sources": [_ll(lat, lon)],
        }
        data = await self._call("isochrone", body)
        if data.get("status") and data["status"] != 0:
            raise ValhallaError(
                f"Valhalla isochrone error: {data.get('status_message', data)}"
            )
        return data

    async def route(
        self,
        origin: tuple[float, float],
        destination: tuple[float, float],
        *,
        costing: str = "auto",
    ) -> dict[str, Any]:
        """Compute a turn-by-turn route between two coordinates.

        Returns the raw Valhalla ``route`` response. Summary distance/time
        live under ``["trip"]["summary"]`` (``length`` in km, ``time`` in s).
        """
        body = {
            "costing": costing,
            "costing_options": {"auto": {"use_highways": 1}},
            "locations": [
                {**_ll(*origin), "type": "break"},
                {**_ll(*destination), "type": "break"},
            ],
        }
        data = await self._call("route", body)
        if data.get("status") and data["status"] != 0:
            raise ValhallaError(
                f"Valhalla route error: {data.get('status_message', data)}"
            )
        return data

    async def sources_to_targets(
        self,
        sources: Sequence[tuple[float, float]],
        targets: Sequence[tuple[float, float]],
        *,
        costing: str = "auto",
    ) -> list[list[MatrixCell]]:
        """Compute a one-to-many or many-to-many time/distance matrix.

        Returns a 2D list ``matrix[i][j]`` of :class:`MatrixCell` giving the
        Valhalla-reported road distance (km) and time (s) from ``sources[i]``
        to ``targets[j]``.
        """
        if not sources or not targets:
            return []
        body = {
            "costing": costing,
            "costing_options": {"auto": {"use_highways": 1}},
            "sources": [{"lat": la, "lon": lo} for la, lo in sources],
            "targets": [{"lat": la, "lon": lo} for la, lo in targets],
        }
        data = await self._call("sources_to_targets", body)
        if data.get("status") and data["status"] != 0:
            raise ValhallaError(
                f"Valhalla matrix error: {data.get('status_message', data)}"
            )

        raw: list[list[dict[str, Any]]] = data.get("sources_to_targets", [])
        matrix: list[list[MatrixCell]] = []
        for row in raw:
            matrix.append(
                [
                    MatrixCell(
                        distance_km=cell.get("distance", 0.0),
                        time_seconds=cell.get("time", 0.0),
                    )
                    for cell in row
                ]
            )
        return matrix

    async def health(self) -> bool:
        """Return True if Valhalla answers a trivial route."""
        try:
            # A 1-point-to-itself sources_to_targets is the cheapest health
            # probe that actually exercises routing (status endpoint is only
            # available on Loki/valhalla builds that expose it).
            matrix = await self.sources_to_targets(
                [(52.517, 13.388)], [(52.517, 13.388)], costing="auto"
            )
            return bool(matrix)
        except ValhallaError:
            return False
