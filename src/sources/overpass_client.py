"""Overpass API client for fetching brand-scoped POIs from OpenStreetMap.

Overpass is the read API for OSM. A single query returns every map feature
matching a tag predicate — e.g. all hotels with ``brand~"Best Western"``
inside Europe — with **exact coordinates already attached** (no geocoding
step needed). This is the recommended POI source for the maps server.

Usage:

    client = OverpassClient("https://overpass-api.de/api/interpreter")
    pois = await client.fetch_brand(
        brand="Best Western",
        area="europe",          # or "DE", ["DE","AT","CH"], a bbox,
                                # "name:München", "around:30000,48.137,11.575",
                                # or "raw:<QL>"
    )

The public Overpass instances are heavily rate-limited (and occasionally
throttled). Treat ``fetch_brand`` as a **one-shot cache-fill at deploy time**,
not a per-query call: the results are persisted in the POIStore, and
``find_within`` reads from there at query time with zero Overpass dependency.

Rate-limit etiquette: send a descriptive ``User-Agent`` and don't hammer the
public endpoints. For steady-state use, self-host an Overpass instance
(alongside Valhalla/Nominatim) and point ``OVERPASS_URL`` at it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable

import httpx

logger = logging.getLogger(__name__)

# Default public endpoint. For production, point OVERPASS_URL at a self-hosted
# instance or a paid mirror with higher rate limits.
DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"

# Europe (excluding overseas territories) — generous bbox covering all of
# continental Europe + British Isles + Scandinavia. Used when area="europe".
EUROPE_BBOX = (-35.0, 32.0, 35.0, 72.0)  # (south, west, north, east)

# ISO-3166-1 alpha-2 codes for the European region. Used to build per-country
# area queries, which are more reliable than the continent bbox for large
# brands. Europe = EU + EFTA + UK + minor territories.
EUROPE_COUNTRY_CODES = (
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK",
    "SI", "ES", "SE",
    # EFTA + UK + microstates
    "IS", "LI", "NO", "CH", "GB", "AL", "AD", "BY", "BA", "XK", "MD", "MC",
    "ME", "MK", "RU", "SM", "RS", "UA", "VA",
)


class OverpassError(RuntimeError):
    """Raised when Overpass is unreachable or returns a rate-limit/error."""


@dataclass(frozen=True)
class OverpassPOI:
    """A single POI extracted from an Overpass result element.

    Coordinates are always present (Overpass guarantees them). Address fields
    are best-effort: many OSM hotels lack ``addr:*`` tags, but the name +
    coordinates are enough for ``find_within``.
    """

    osm_id: str
    osm_type: str  # node | way | relation
    name: str
    lat: float
    lon: float
    street: str = ""
    housenumber: str = ""
    postcode: str = ""
    city: str = ""
    country: str = ""
    phone: str = ""
    website: str = ""
    brand: str = ""
    stars: str = ""
    raw_tags: dict[str, str] | None = None

    @property
    def address(self) -> str:
        parts: list[str] = []
        if self.street:
            street = f"{self.street} {self.housenumber}".strip()
            parts.append(street)
        if self.postcode or self.city:
            parts.append(f"{self.postcode} {self.city}".strip())
        if self.country:
            parts.append(self.country)
        return ", ".join(p for p in parts if p)


def _element_coords(el: dict[str, Any]) -> tuple[float, float] | None:
    """Extract (lat, lon) from a node, or the center of a way/relation."""
    if "lat" in el and "lon" in el:
        return float(el["lat"]), float(el["lon"])
    center = el.get("center")
    if center and "lat" in center and "lon" in center:
        return float(center["lat"]), float(center["lon"])
    return None


def _parse_element(el: dict[str, Any]) -> OverpassPOI | None:
    """Parse one Overpass element into an :class:`OverpassPOI`, or None."""
    tags: dict[str, Any] = el.get("tags", {}) or {}
    name = str(tags.get("name", "")).strip()
    if not name:
        # Skip unnamed features — they're useless as POIs.
        return None
    coords = _element_coords(el)
    if coords is None:
        return None
    lat, lon = coords
    return OverpassPOI(
        osm_id=f"{el.get('type')}:{el.get('id')}",
        osm_type=str(el.get("type", "")),
        name=name,
        lat=lat,
        lon=lon,
        street=str(tags.get("addr:street", "")).strip(),
        housenumber=str(tags.get("addr:housenumber", "")).strip(),
        postcode=str(tags.get("addr:postcode", "")).strip(),
        city=str(tags.get("addr:city", "")).strip(),
        country=str(tags.get("addr:country", "")).strip(),
        phone=str(tags.get("phone") or tags.get("contact:phone", "")).strip(),
        website=str(tags.get("website") or tags.get("contact:website", "")).strip(),
        brand=str(tags.get("brand", "")).strip(),
        stars=str(tags.get("stars", "")).strip(),
        raw_tags={k: str(v) for k, v in tags.items()},
    )


# A legal ISO 3166-1 alpha-2 country code (used to validate the
# single-country / country-list branches so a stray string like "München"
# can't fall through into a meaningless `area["ISO3166-1"="MÜNCHEN"]` query).
_ISO_CODE_RE = re.compile(r"^[A-Za-z]{2}$")

# Control characters (incl. NUL, newline, tab) — not valid inside an OSM
# name and dangerous inside an Overpass QL string literal, so they are
# stripped before a place name is embedded into a query.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _escape_ql_string(value: str) -> str:
    """Make a string safe to embed inside an Overpass QL double-quoted
    string literal.

    Strips control characters, then escapes ``\\`` and ``"`` (the only two
    characters with special meaning in an Overpass QL string literal).
    Returns the escaped value (without surrounding quotes).
    """
    cleaned = _CONTROL_CHARS_RE.sub("", value)
    return cleaned.replace("\\", "\\\\").replace('"', '\\"')


def _parse_around(spec: str) -> tuple[float, float, float] | None:
    """Parse an ``around:RADIUS,LAT,LON`` spec into a ``(radius, lat, lon)``
    tuple of floats, or return ``None`` if it is not a valid around-spec.

    Accepted forms (the ``around:`` prefix is part of the input):
      * ``around:30000,48.137,11.575``          (metres, the Overpass unit)
      * ``around:30 km,48.1372,11.5754``        (with optional unit suffix)
      * ``around:30km,48.1372,11.5754``
      * ``around:5 mi,48.1372,11.5754``

    The radius is returned in **metres** (Overpass's native unit). Supported
    suffixes: ``km`` (×1000), ``mi`` (×1609.344), ``m`` (no-op). No suffix is
    treated as metres.
    """
    body = spec[len("around:"):].strip()
    parts = [p.strip() for p in body.split(",")]
    if len(parts) != 3:
        return None
    raw_radius, lat_s, lon_s = parts
    try:
        lat = float(lat_s)
        lon = float(lon_s)
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return None

    # Radius may carry an optional unit suffix; default unit is metres.
    rm = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(km|mi|m)?$", raw_radius, re.IGNORECASE)
    if not rm:
        return None
    value = float(rm.group(1))
    unit = (rm.group(2) or "m").lower()
    radius = value * {"m": 1.0, "km": 1000.0, "mi": 1609.344}[unit]
    if radius <= 0:
        return None
    return radius, lat, lon


def build_brand_query(
    brand: str,
    *,
    area: str | Iterable[str] = "europe",
    timeout: int = 60,
) -> str:
    """Build an Overpass QL query for all features tagged with ``brand``.

    Args:
        brand: Brand name (regex-escaped). Case-insensitive, whitespace/
            punctuation tolerant (``Best Western`` matches ``best-western``).
        area: Geographic scope. Accepts any of the following:

            * ``"europe"`` — a bbox covering continental Europe (default).
            * a 2-letter ISO country code, e.g. ``"DE"``.
            * a list of country codes, e.g. ``["DE", "AT", "CH"]``.
            * a bbox tuple ``(south, west, north, east)``.
            * ``"name:<place>"`` — every OSM area whose name matches
              ``<place>``, e.g. ``"name:München"`` matches the city of
              Munich. Use the local name (``München``, not ``Munich``) for
              best results. Only *named areas* (city/county boundaries) are
              matched — this does **not** geocode a point.
            * ``"around:<radius>,<lat>,<lon>"`` — a radius search around a
              point, e.g. ``"around:30000,48.137,11.575"`` (30 km around
              central Munich). ``<radius>`` is in metres unless suffixed
              with ``km`` or ``mi`` (e.g. ``"around:30 km,48.137,11.575"``).
            * ``"raw:<QL>"`` — inject a raw Overpass QL area predicate
              verbatim, for cases the above don't cover.

            Any other string (e.g. ``"München"`` without a prefix) raises
            ``ValueError`` rather than silently building a no-match query.
        timeout: Overpass server-side timeout in seconds.
    """
    # Tolerant brand regex: collapse spaces to allow optional non-alnum
    # separators in the source tag ("Best Western", "Best-Western",
    # "Best_Western"). Already-regex-special chars are escaped.
    brand_token = re.escape(brand).replace(r"\ ", r"[\\s_-]?")
    brand_regex = brand_token  # case-insensitive flag added in QL with ',i'

    def _country_clause(code: str) -> str:
        return f'area["ISO3166-1"="{code}"]->.a_{code}'

    # --- Prefixed string forms: name:, around:, raw: ---------------------
    if isinstance(area, str):
        al = area.lower()
        if al == "europe":
            s, w, n, e = EUROPE_BBOX
            predicate = f"({brand_regex})({s},{w},{n},{e})"
            return f"[out:json][timeout:{timeout}];\nnwr[\"brand\"~\"{brand_regex}\",i]{predicate};\nout center tags;\n"
        if al.startswith("name:"):
            name = area[len("name:"):].strip()
            if not name:
                raise ValueError('area="name:" requires a place name, e.g. name:München')
            esc = _escape_ql_string(name)
            return (
                f"[out:json][timeout:{timeout}];\n"
                f'area["name"="{esc}"]->.search_area;\n'
                f'nwr["brand"~"{brand_regex}",i](area.search_area);\n'
                f"out center tags;\n"
            )
        if al.startswith("around:"):
            parsed = _parse_around(area)
            if parsed is None:
                raise ValueError(
                    f'Invalid area={area!r}: expected around:RADIUS,LAT,LON '
                    f'(e.g. "around:30000,48.137,11.575" or '
                    f'"around:30 km,48.137,11.575")'
                )
            radius, lat, lon = parsed
            predicate = f"(around:{radius:g},{lat:g},{lon:g})"
            return f"[out:json][timeout:{timeout}];\nnwr[\"brand\"~\"{brand_regex}\",i]{predicate};\nout center tags;\n"
        if al.startswith("raw:"):
            raw = area[len("raw:"):]
            return f"[out:json][timeout:{timeout}];\nnwr[\"brand\"~\"{brand_regex}\",i]{raw};\nout center tags;\n"
        # Bare string: only accept a 2-letter country code. Anything else
        # (e.g. "München") is almost certainly a mistake — raise rather than
        # build a meaningless ISO3166-1 query that returns nothing.
        if _ISO_CODE_RE.match(area) and area.isalpha():
            code = area.upper()
            return (
                f"[out:json][timeout:{timeout}];\n"
                f'area["ISO3166-1"="{code}"]->.a;\n'
                f'nwr["brand"~"{brand_regex}",i](area.a);\n'
                f"out center tags;\n"
            )
        raise ValueError(
            f'Cannot interpret area={area!r}. Use a 2-letter country code '
            f'(e.g. "DE"), a bbox, or one of the prefixed forms '
            f'name:<place> / around:<radius>,<lat>,<lon> / raw:<QL>.'
        )

    # --- Bbox tuple ------------------------------------------------------
    if isinstance(area, tuple) and len(area) == 4:
        s, w, n, e = area
        predicate = f"({s},{w},{n},{e})"
        return f"[out:json][timeout:{timeout}];\nnwr[\"brand\"~\"{brand_regex}\",i]{predicate};\nout center tags;\n"

    # --- Country list ----------------------------------------------------
    if isinstance(area, (list, tuple)):
        codes = list(area)
        area_decls = "\n".join(_country_clause(c) for c in codes)
        # Query each country area separately then union.
        matchers = "\n".join(
            f'nwr["brand"~"{brand_regex}",i](area.a_{c});' for c in codes
        )
        return (
            f"[out:json][timeout:{timeout}];\n"
            f"{area_decls}\n"
            f"(\n{matchers}\n);\n"
            f"out center tags;\n"
        )

    raise ValueError(f"Unsupported area type {type(area).__name__}: {area!r}")


class OverpassClient:
    """Async client for the Overpass API.

    Args:
        endpoint: Overpass ``/interpreter`` URL.
        user_agent: Descriptive User-Agent (OSM usage policy requests this).
        timeout: Request timeout in seconds (Overpass queries can be slow).
        connect_timeout: Connect timeout.
    """

    def __init__(
        self,
        endpoint: str = DEFAULT_OVERPASS_URL,
        *,
        user_agent: str = "jquad-maps-mcp",
        timeout: float = 90.0,
        connect_timeout: float = 15.0,
    ) -> None:
        self.endpoint = endpoint
        self.user_agent = user_agent
        self._timeout = httpx.Timeout(timeout, connect=connect_timeout)

    async def fetch_brand(
        self,
        brand: str,
        *,
        area: str | Iterable[str] = "europe",
        query_timeout: int = 60,
    ) -> list[OverpassPOI]:
        """Fetch every OSM feature tagged with ``brand`` in ``area``.

        Args:
            brand: Brand name, e.g. ``"Best Western"``.
            area: Geographic scope — see :func:`build_brand_query` for the
                full set of forms: ``"europe"``, a country code or list,
                a bbox tuple, ``"name:<place>"``, ``"around:<r>,<lat>,<lon>"``,
                or ``"raw:<QL>"``.
            query_timeout: Server-side Overpass timeout.

        Returns:
            List of :class:`OverpassPOI` (deduplicated by ``osm_id``).

        Raises:
            ValueError: if ``area`` cannot be interpreted (e.g. a bare city
                name with no ``name:`` prefix).
            OverpassError: on HTTP errors, rate-limiting (429), or a
                server-reported error in the response body.
        """
        query = build_brand_query(brand, area=area, timeout=query_timeout)
        logger.info(
            "Overpass fetch: brand=%r area=%r", brand, area,
        )
        async with httpx.AsyncClient(
            timeout=self._timeout,
            headers={"User-Agent": self.user_agent},
        ) as client:
            try:
                resp = await client.post(self.endpoint, data=query.encode("utf-8"))
            except httpx.HTTPError as exc:
                raise OverpassError(f"Overpass request failed: {exc}") from exc

            if resp.status_code == 429:
                raise OverpassError(
                    "Overpass rate limit exceeded (HTTP 429). Retry later, or "
                    "point OVERPASS_URL at a self-hosted instance / paid mirror."
                )
            if resp.status_code == 504:
                raise OverpassError(
                    "Overpass gateway timeout (HTTP 504). Try a smaller area "
                    "or a larger query_timeout."
                )
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise OverpassError(
                    f"Overpass HTTP {resp.status_code}: {exc}"
                ) from exc
            try:
                data = resp.json()
            except ValueError as exc:
                raise OverpassError(f"Overpass returned non-JSON: {exc}") from exc

        if isinstance(data, dict) and "remark" in data:
            remark = str(data["remark"])
            if remark and "error" in remark.lower():
                raise OverpassError(f"Overpass query error: {remark}")

        elements: list[dict[str, Any]] = data.get("elements", [])
        pois: list[OverpassPOI] = []
        seen: set[str] = set()
        for el in elements:
            poi = _parse_element(el)
            if poi is None or poi.osm_id in seen:
                continue
            seen.add(poi.osm_id)
            pois.append(poi)
        logger.info(
            "Overpass returned %d elements, %d named POIs (after dedup)",
            len(elements), len(pois),
        )
        return pois

    async def health(self) -> bool:
        """Return True if the Overpass endpoint responds."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(self.endpoint.rsplit("/", 1)[0] + "/status")
                return resp.status_code in (200, 418)  # 418 = "too many requests" but alive
        except httpx.HTTPError:
            return False
