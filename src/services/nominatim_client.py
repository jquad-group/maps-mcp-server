"""Async client for a self-hosted (or public) Nominatim geocoder.

Self-hosted Nominatim has no rate limit, but the public instance enforces
<= 1 req/s by usage policy. We throttle defensively either way and expose a
simple ``geocode`` / ``reverse_geocode`` surface used by the MCP tools.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class GeocodeResult:
    """A single geocoding hit."""

    lat: float
    lon: float
    display_name: str
    raw: dict[str, Any] | None = None

    @property
    def coord(self) -> tuple[float, float]:
        """Return coordinates as a ``(lat, lon)`` tuple.

        Matches :attr:`POI.coord` so callers can treat both types uniformly
        (e.g. ``valhalla.route(o.coord, d.coord)`` works whether ``o`` is a
        ``GeocodeResult`` or a ``POI``).
        """
        return (self.lat, self.lon)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lat": self.lat,
            "lon": self.lon,
            "display_name": self.display_name,
        }


class NominatimError(RuntimeError):
    """Raised when Nominatim is unreachable or returns no usable result."""


class NominatimClient:
    """Thin async wrapper around the Nominatim Search/Reverse endpoints.

    Args:
        base_url: Nominatim base URL, no trailing slash.
        user_agent: ``User-Agent`` header (required by the public instance).
        rate_limit_rps: Min interval between calls = 1/rps. A module-level
            lock enforces this across concurrent callers.
        timeout: Total request timeout in seconds.
        connect_timeout: Connect timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        *,
        user_agent: str = "jquad-maps-mcp",
        rate_limit_rps: float = 5.0,
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self._min_interval = 1.0 / rate_limit_rps if rate_limit_rps > 0 else 0.0
        self._timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self._lock = asyncio.Lock()
        self._last_call_at: float = 0.0

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._timeout,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json",
            },
        )

    async def _throttle(self) -> None:
        """Block until at least ``_min_interval`` has elapsed since last call."""
        if self._min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._last_call_at + self._min_interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call_at = time.monotonic()

    async def geocode(
        self, query: str, *, countrycodes: str | None = None
    ) -> GeocodeResult:
        """Geocode a free-text query to a single best result.

        Args:
            query: Place name or postal address.
            countrycodes: Optional ISO-3166 country code(s), e.g. ``"de"``.

        Raises:
            NominatimError: if Nominatim returns no results or an HTTP error.
        """
        await self._throttle()
        params: dict[str, str] = {
            "q": query,
            "format": "jsonv2",
            "addressdetails": "0",
            "limit": "1",
        }
        if countrycodes:
            params["countrycodes"] = countrycodes

        async with self._client() as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/search", params=params
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise NominatimError(
                    f"Nominatim geocode request failed: {exc}"
                ) from exc
            data = resp.json()

        if not data:
            raise NominatimError(f"Nominatim returned no results for {query!r}")

        hit = data[0]
        try:
            return GeocodeResult(
                lat=float(hit["lat"]),
                lon=float(hit["lon"]),
                display_name=hit.get("display_name", query),
                raw=hit,
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise NominatimError(
                f"Nominatim returned an unparseable result for {query!r}: {hit}"
            ) from exc

    async def reverse_geocode(self, lat: float, lon: float) -> GeocodeResult:
        """Reverse-geocode coordinates to the nearest address."""
        await self._throttle()
        params = {
            "lat": str(lat),
            "lon": str(lon),
            "format": "jsonv2",
            "addressdetails": "0",
        }
        async with self._client() as client:
            try:
                resp = await client.get(
                    f"{self.base_url}/reverse", params=params
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise NominatimError(
                    f"Nominatim reverse request failed: {exc}"
                ) from exc
            hit = resp.json()

        if not hit or "error" in hit:
            raise NominatimError(
                f"Nominatim reverse returned no result for ({lat}, {lon})"
            )
        try:
            return GeocodeResult(
                lat=float(hit["lat"]),
                lon=float(hit["lon"]),
                display_name=hit.get("display_name", f"{lat}, {lon}"),
                raw=hit,
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise NominatimError(
                f"Nominatim returned an unparseable reverse result: {hit}"
            ) from exc

    async def health(self) -> bool:
        """Return True if Nominatim's status endpoint responds."""
        try:
            async with self._client() as client:
                resp = await client.get(f"{self.base_url}/status", timeout=5.0)
                return resp.status_code == 200
        except httpx.HTTPError:
            return False
