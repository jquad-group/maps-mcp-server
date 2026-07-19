"""Maps MCP Server.

An MCP server providing geocoding, routing, isochrones and POI-by-proximity
queries on top of self-hosted Valhalla (routing) and Nominatim (geocoding).
POIs are populated from OpenStreetMap (via Overpass) or a curated CSV/JSON
file; any list of places can also be supplied to ``find_within``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.config import settings
from src.infrastructure.telemetry import setup_telemetry
from src.services.nominatim_client import NominatimClient, NominatimError
from src.services.poi_store import POI, POIStore
from src.services.query_orchestrator import (
    aresolve_location,
    find_within as run_find_within,
)
from src.services.valhalla_client import ValhallaClient, ValhallaError
from src.sources import (
    ImportError_,
    OverpassClient,
    OverpassError,
    overpass_to_poi,
    import_file,
    import_json,
)


def setup_logging() -> None:
    """Configure logging with a consistent format across all loggers."""
    log_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper())

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)
    root_logger.handlers.clear()

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    for logger_name in [
        "httpx", "httpcore", "fastmcp", "mcp",
        "mcp.server", "mcp.server.lowlevel", "mcp.server.streamable_http_manager",
        "uvicorn", "uvicorn.access", "uvicorn.error", "uvicorn.logging",
    ]:
        specific_logger = logging.getLogger(logger_name)
        specific_logger.setLevel(log_level)
        specific_logger.propagate = True


# Set up logging before importing other modules.
setup_logging()
setup_telemetry("maps-mcp")

logger = logging.getLogger(__name__)

SERVICE_NAME = "maps"

# Optional Keycloak JWT auth (mirrors the sibling servers). The agent
# forwards the user's bootstrap token as Bearer regardless; this only
# decides whether the maps server *validates* it.
keycloak_enabled = os.getenv("KEYCLOAK_ENABLED", "false").lower() == "true"
if keycloak_enabled:
    from fastmcp.server.auth.providers.jwt import JWTVerifier

    jwks_uri = (
        f"{os.getenv('KEYCLOAK_SERVER_URL', 'https://keycloak.jquad.rocks')}"
        f"/realms/{os.getenv('KEYCLOAK_REALM', 'master')}/protocol/openid-connect/certs"
    )
    issuer = (
        f"{os.getenv('KEYCLOAK_SERVER_URL', 'https://keycloak.jquad.rocks')}"
        f"/realms/{os.getenv('KEYCLOAK_REALM', 'master')}"
    )
    audience = os.getenv("KEYCLOAK_CLIENT_ID", "maps-mcp")
    logger.info("Keycloak authentication enabled for audience: %s", audience)
    auth = JWTVerifier(jwks_uri=jwks_uri, issuer=issuer, audience=audience)
    mcp = FastMCP(SERVICE_NAME, auth=auth)
else:
    logger.info("Keycloak authentication disabled - running in insecure mode")
    mcp = FastMCP(SERVICE_NAME)

# ---------------------------------------------------------------------------
# Service singletons — constructed once, reused across tool calls.
# ---------------------------------------------------------------------------
nominatim = NominatimClient(
    settings.nominatim_url,
    user_agent=settings.nominatim_user_agent,
    rate_limit_rps=settings.nominatim_rate_limit_rps,
    timeout=settings.http_timeout_seconds,
    connect_timeout=settings.http_connect_timeout,
)
valhalla = ValhallaClient(
    settings.valhalla_url,
    timeout=settings.http_timeout_seconds,
    connect_timeout=settings.http_connect_timeout,
)
poi_store = POIStore(settings.poi_data_path)
overpass = OverpassClient(
    settings.overpass_url,
    user_agent=settings.nominatim_user_agent,
    timeout=settings.overpass_timeout,
    connect_timeout=settings.http_connect_timeout,
)
logger.info(
    "Maps MCP initialised: valhalla=%s nominatim=%s overpass=%s poi_dir=%s",
    settings.valhalla_url, settings.nominatim_url, settings.overpass_url,
    settings.poi_data_path,
)

# ---------------------------------------------------------------------------
# Auto-seed: on startup, if the default Best Western collection is empty,
# import the pre-geocoded hotel coordinates bundled in the image (./seed/).
# This bypasses OSM/Overpass gaps (Austria/Switzerland) and ensures all
# 164 Best Western hotels from the scraped corpus are findable by distance
# immediately — no ingest_poi call needed.
# ---------------------------------------------------------------------------
_seed_path = Path(__file__).resolve().parent.parent / "seed" / "bestwestern-hotels.json"
if _seed_path.exists():
    _seed_coll = settings.poi_default_collection
    _existing = poi_store.load(_seed_coll)
    if not _existing:
        logger.info("Auto-seeding collection '%s' from %s ...", _seed_coll, _seed_path)
        try:
            _seed_pois = import_json(_seed_path, collection=_seed_coll)
            _written = poi_store.upsert_many(_seed_coll, _seed_pois)
            logger.info("Auto-seeded %d Best Western hotels into '%s'", _written, _seed_coll)
        except Exception as exc:
            logger.warning("Auto-seed failed (non-fatal): %s", exc)
    else:
        logger.info("Collection '%s' already has %d POIs — skipping auto-seed",
                     _seed_coll, len(_existing))
else:
    logger.debug("No seed file at %s — skipping auto-seed", _seed_path)


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def geocode(query: str) -> dict[str, Any]:
    """Turn a place name or postal address into coordinates.

    Use this as the first step for any spatial question whose input is a
    name rather than coordinates (e.g. "Nuremberg", "Messeplatz 3, 45131 Essen").

    Args:
        query: A place name ("Nuremberg") or postal address.

    Returns:
        ``{"lat": float, "lon": float, "display_name": str}``.
    """
    logger.info("geocode(%s)", query)
    try:
        result = await nominatim.geocode(query)
    except NominatimError as exc:
        return {"error": str(exc), "query": query}
    return result.to_dict()


@mcp.tool()
async def reverse_geocode(lat: float, lon: float) -> dict[str, Any]:
    """Reverse-geocode coordinates to the nearest address.

    Args:
        lat: Latitude.
        lon: Longitude.

    Returns:
        ``{"lat": float, "lon": float, "display_name": str}``.
    """
    logger.info("reverse_geocode(%s, %s)", lat, lon)
    try:
        result = await nominatim.reverse_geocode(lat, lon)
    except NominatimError as exc:
        return {"error": str(exc), "lat": lat, "lon": lon}
    return result.to_dict()


@mcp.tool()
async def isochrone(
    location: str | dict[str, float],
    minutes: int,
    costing: str = "auto",
) -> dict[str, Any]:
    """Compute the area reachable within ``minutes`` of driving (the "an hour
    away" primitive).

    Args:
        location: A place name (auto-geocoded) or a ``{"lat": .., "lon": ..}`` dict.
        minutes: Contour time in minutes (e.g. 60 for "an hour").
        costing: Valhalla costing mode: ``auto`` (default), ``bicycle``,
            ``pedestrian``, ``truck``.

    Returns:
        The Valhalla isochrone response (GeoJSON FeatureCollection) plus the
        resolved center under ``"center"``.
    """
    logger.info("isochrone(%s, %s min, %s)", location, minutes, costing)
    try:
        center = await aresolve_location(location, nominatim=nominatim)
        iso = await valhalla.isochrone(
            center.lat, center.lon, minutes=minutes, costing=costing
        )
    except (NominatimError, ValhallaError) as exc:
        return {"error": str(exc), "location": location, "minutes": minutes}
    return {"center": center.to_dict(), "minutes": minutes, "isochrone": iso}


@mcp.tool()
async def route(
    origin: str | dict[str, float],
    destination: str | dict[str, float],
    costing: str = "auto",
) -> dict[str, Any]:
    """Driving distance and duration between two places.

    Args:
        origin: Place name or ``{"lat": .., "lon": ..}`` dict.
        destination: Place name or ``{"lat": .., "lon": ..}`` dict.
        costing: Valhalla costing mode (default ``auto``).

    Returns:
        ``{"summary": {"distance_km": float, "time_seconds": int},
        "origin": ..., "destination": ...}``.
    """
    logger.info("route(%s -> %s, %s)", origin, destination, costing)
    try:
        o = await aresolve_location(origin, nominatim=nominatim)
        d = await aresolve_location(destination, nominatim=nominatim)
        resp = await valhalla.route(o.coord, d.coord, costing=costing)
    except (NominatimError, ValhallaError) as exc:
        return {"error": str(exc), "origin": origin, "destination": destination}

    trip = resp.get("trip", {})
    summary = trip.get("summary", {})
    return {
        "origin": o.to_dict(),
        "destination": d.to_dict(),
        "summary": {
            "distance_km": round(summary.get("length", 0.0), 2),
            "time_seconds": int(summary.get("time", 0)),
            "time_minutes": round(summary.get("time", 0) / 60.0, 1),
        },
    }


@mcp.tool()
async def distance_matrix(
    sources: list[str | dict[str, float]],
    destinations: list[str | dict[str, float]],
    costing: str = "auto",
) -> dict[str, Any]:
    """Batch one-to-many / many-to-many time+distance matrix.

    Cheaper than calling :meth:`route` N times when you have many POIs. Each
    source/destination is a place name or ``{"lat": .., "lon": ..}`` dict.

    Returns:
        ``{"matrix": [[{"distance_km": float, "time_seconds": int}, ...], ...]}``
        indexed by ``[source_index][destination_index]``.
    """
    logger.info(
        "distance_matrix(%d sources x %d destinations, %s)",
        len(sources), len(destinations), costing,
    )
    try:
        src_coords = [await aresolve_location(s, nominatim=nominatim) for s in sources]
        dst_coords = [await aresolve_location(d, nominatim=nominatim) for d in destinations]
        matrix = await valhalla.sources_to_targets(
            sources=[c.coord for c in src_coords],
            targets=[c.coord for c in dst_coords],
            costing=costing,
        )
    except (NominatimError, ValhallaError) as exc:
        return {"error": str(exc)}

    return {
        "matrix": [
            [
                {"distance_km": round(cell.distance_km, 2), "time_seconds": int(cell.time_seconds)}
                for cell in row
            ]
            for row in matrix
        ]
    }


@mcp.tool()
async def find_within(
    center: str | dict[str, float],
    within: str,
    places: list[str] | None = None,
    collection: str | None = None,
    metric: str = "road",
    costing: str = "auto",
) -> dict[str, Any]:
    """Find POIs within a distance or time budget of a center point.

    This is the headline tool: it answers queries like "Best Western hotels
    within 50 km of Nuremberg" or "... an hour's drive from Nuremberg".

    The ``within`` string determines the budget kind:

    * **distance** — ``"50 km"``, ``"50km"``, ``"30 miles"``. Default metric
      is road distance via Valhalla; set ``metric="crow"`` for straight-line.
    * **time** — ``"1 hour"``, ``"45 minutes"``, ``"30 min"``. Uses a Valhalla
      isochrone polygon + point-in-polygon, with a matrix-reported duration
      per match.

    Args:
        center: Place name (auto-geocoded) or ``{"lat": .., "lon": ..}`` dict.
        within: Budget string (see above).
        places: Optional list of place names to evaluate. If omitted, uses
            the ingested POI cache for ``collection`` (default: all Best
            Western hotels). Each entry is geocoded.
        collection: POI collection to draw candidates from when ``places`` is
            omitted. Defaults to ``POI_DEFAULT_COLLECTION``.
        metric: ``"road"`` (default) or ``"crow"`` — only affects distance budgets.
        costing: Valhalla costing mode (default ``auto``).

    Returns:
        ``{"budget": {...}, "center": {...}, "matches": [...],
        "match_count": int, "total_evaluated": int}``. Matches are sorted
        nearest/shortest first; each match has name, address, lat/lon, and
        either ``distance_km`` or ``time_seconds`` + ``inside_isochrone``.
    """
    coll = collection or settings.poi_default_collection
    logger.info("find_within(center=%s, within=%s, places=%s, collection=%s)",
                center, within, len(places) if places else None, coll)
    try:
        center_geo = await aresolve_location(center, nominatim=nominatim)

        if places:
            # Geocode each supplied place name into a temporary POI list.
            candidates: list[POI] = []
            for name in places:
                try:
                    g = await nominatim.geocode(name)
                except NominatimError as exc:
                    logger.warning("could not geocode place %r: %s", name, exc)
                    continue
                candidates.append(_ad_hoc_poi(name, g.lat, g.lon))
        else:
            candidates = poi_store.load(coll)
            if not candidates:
                return {
                    "error": (
                        f"POI collection {coll!r} is empty. Run ingest_poi first "
                        "(or pass explicit 'places')."
                    ),
                    "collection": coll,
                }

        result = await run_find_within(
            center_geo,
            within,
            candidates,
            valhalla=valhalla,
            metric=metric,  # type: ignore[arg-type]
            costing=costing,
            max_matrix_size=settings.max_matrix_size,
        )
    except (NominatimError, ValhallaError, ValueError) as exc:
        return {"error": str(exc), "center": center, "within": within}
    return result


def _ad_hoc_poi(name: str, lat: float, lon: float) -> POI:
    """Build an ad-hoc POI for a geocoded place name passed to find_within."""
    import hashlib

    pid = hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]
    return POI(
        place_id=pid,
        name=name,
        street="",
        zip="",
        city="",
        country="",
        lat=lat,
        lon=lon,
        source_url="",
        source_file="",
        collection="ad-hoc",
    )


@mcp.tool()
async def ingest_poi(
    source: str = "overpass",
    collection: str | None = None,
    brand: str | None = None,
    area: str | list[str] | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Populate a POI collection from OpenStreetMap or a curated file.

    **This is a one-time admin operation** to fill the cache. For querying
    hotels by distance, use ``find_within`` instead — it reads the existing
    cache and does NOT re-query Overpass. Only call ``ingest_poi`` if the
    collection is empty or you need to refresh it with new data.

    **Important:** Best Western hotels are already pre-seeded in the default
    collection (``bestwestern-de``) with 164 hotels covering Germany,
    Austria, and Switzerland. Do NOT call ``ingest_poi`` for Best Western
    — use ``find_within`` directly. Only use ``ingest_poi`` for OTHER POI
    types (restaurants, pharmacies, gas stations, other hotel brands).

    The ``source`` argument chooses where the POIs come from:

    * ``"overpass"`` *(default)* — query OpenStreetMap via Overpass for every
      feature tagged ``brand=<brand>`` inside ``<area>``. Returns hotels with
      **exact coordinates** already attached (no geocoding needed). This is a
      cache-fill — run it once, then ``find_within`` reads the cache with zero
      Overpass dependency. Re-running with the **same** area is idempotent
      (dedup by ``place_id``); re-running with a **different** area
      **accumulates** POIs into the collection unless ``replace=True``.
      **Warning:** OSM coverage varies by region (poor in Austria/Switzerland).
      If the collection already has data, prefer ``find_within`` over
      re-ingesting.

    * ``"csv:<path>"`` or ``"json:<path>"`` — import a curated file. The file
      is read once and cached. Use this to curate a checked-in list, or to
      supplement Overpass results.

    Args:
        source: ``"overpass"`` (default), or ``"csv:<path>"`` / ``"json:<path>"``.
        collection: Target collection name. Defaults to ``POI_DEFAULT_COLLECTION``.
        brand: *(overpass only)* Brand name. Defaults to ``POI_DEFAULT_BRAND``
            (``Best Western``).
        area: *(overpass only)* Geographic scope. One of:

            * ``"europe"`` *(default)* — continental Europe bbox.
            * a 2-letter ISO country code, e.g. ``"DE"``.
            * a JSON list of codes, e.g. ``'["DE","AT"]'``.
            * a JSON bbox ``"[south,west,north,east]"`` in degrees, e.g.
              ``"[47.5,10.5,48.9,12.7]"``.
            * ``"name:<place>"`` — search the *named area* (city/region
              boundary) called ``<place>`` in OSM, e.g.
              ``"name:München"``. Use the **local** name (``München``, not
              ``Munich``) for best results. This matches administrative
              boundaries only — it does not geocode a point, and it does
              **not** include surrounding towns/suburbs. For that, use
              ``around:``.
            * ``"around:<radius>,<lat>,<lon>"`` — a **radius search** around
              a point, in metres unless suffixed with ``km``/``mi``.
              Examples: ``"around:30000,48.137,11.575"`` (30 km around
              central Munich) or ``"around:30 km,48.137,11.575"``. Use this
              when you want a distance circle (incl. nearby suburbs like
              Erding/Vaterstetten) rather than a city boundary.
            * ``"raw:<QL>"`` — a raw Overpass QL area predicate, for cases
              the other forms don't cover.

            A bare string with no prefix is treated as a country code (so
            ``"München"`` raises an error — use ``"name:München"``).
        replace: If ``True``, clear the collection **before** ingesting, so
            only the new POIs remain (default ``False``). Re-ingesting with a
            *different* area accumulates POIs by default — e.g. an
            ``"around:30000,..."`` query after an earlier ``"europe"`` ingest
            would leave both sets in the collection. Set ``replace=True``
            whenever you re-run with a different area to avoid stale POIs
            (e.g. hotels from another city) polluting subsequent
            ``find_within`` / ``list_poi`` results. Re-running with the same
            area does not need this (it dedups by ``place_id``).

    Returns:
        ``{"source": str, "collection": str, "ingested": int, "total": int}``.
        On error: ``{"error": str, "source": str, "collection": str}``.
    """
    coll = collection or settings.poi_default_collection
    # Normalize area to a string: if the caller passes a list (e.g.
    # ["DE","AT","CH"]), JSON-encode it so _parse_area can handle it as the
    # documented JSON-list form. This makes the tool tolerant of both
    # string and list inputs.
    if isinstance(area, list):
        area = json.dumps(area)
    logger.info("ingest_poi(source=%s, collection=%s, brand=%s, area=%s, replace=%s)",
                source, coll, brand, area, replace)
    try:
        if source == "overpass" or source.startswith("overpass:"):
            pois = await _ingest_from_overpass(
                coll, brand or settings.poi_default_brand,
                area or settings.poi_default_area,
            )
        elif source.startswith("csv:") or source.startswith("json:"):
            fmt, _, path = source.partition(":")
            pois = import_file(path, collection=coll)
            if not source.lower().endswith(Path(path).suffix.lower()) and fmt == "json":
                # import_file dispatches on suffix; for an explicit "json:"
                # prefix with an odd extension, force JSON parse.
                from src.sources import import_json
                pois = import_json(path, collection=coll)
        else:
            # Bare path: detect by extension.
            pois = import_file(source, collection=coll)
    except (OverpassError, ImportError_, FileNotFoundError, ValueError) as exc:
        return {"error": str(exc), "source": source, "collection": coll}

    # Clear the collection first when replacing, so POIs from a previous
    # ingest (e.g. a different area) don't accumulate alongside the new ones.
    if replace:
        poi_store.clear(coll)
    written = poi_store.upsert_many(coll, pois)
    return {
        "source": source,
        "collection": coll,
        "ingested": written,
        "total": len(poi_store.load(coll)),
    }


async def _ingest_from_overpass(collection: str, brand: str, area: str) -> list[POI]:
    """Fetch brand POIs from Overpass and map them to cacheable POIs."""
    # area may be a JSON-encoded list/bbox or a plain string keyword/code.
    parsed_area = _parse_area(area)
    op_pois = await overpass.fetch_brand(brand, area=parsed_area)
    return [overpass_to_poi(op, collection=collection) for op in op_pois]


def _parse_area(area: str) -> Any:
    """Parse the area string from ingest_poi into the OverpassClient form."""
    import json as _json
    s = area.strip()
    # Bbox as "[s,w,n,e]".
    if s.startswith("[") and s.endswith("]"):
        try:
            vals = _json.loads(s)
            if isinstance(vals, list) and len(vals) == 4:
                return tuple(float(v) for v in vals)
            if isinstance(vals, list):
                return [str(v) for v in vals]
        except _json.JSONDecodeError:
            pass
    return s


@mcp.tool()
async def list_poi(collection: str | None = None) -> dict[str, Any]:
    """List all POIs cached in a collection.

    Args:
        collection: Collection name (defaults to ``POI_DEFAULT_COLLECTION``).

    Returns:
        ``{"collection": str, "count": int, "places": [{name, address, lat,
        lon, source_url}, ...]}``.
    """
    coll = collection or settings.poi_default_collection
    pois = poi_store.load(coll)
    return {
        "collection": coll,
        "count": len(pois),
        "places": [
            {
                "name": p.name,
                "address": p.address,
                "lat": p.lat,
                "lon": p.lon,
                "source_url": p.source_url,
            }
            for p in pois
        ],
    }


@mcp.tool()
async def maps_health() -> dict[str, Any]:
    """Health check for the maps backends.

    **Internal / administrative only** — do not call during user
    conversations.

    Returns:
        ``{"status": "healthy"|"degraded", "valhalla": bool, "nominatim":
        bool, "overpass": bool, "poi_collections": {name: count}}``.
    """
    val_ok = await valhalla.health()
    nom_ok = await nominatim.health()
    ov_ok = await overpass.health()
    poi_files = {}
    if settings.poi_data_path.exists():
        for f in settings.poi_data_path.glob("*.json"):
            try:
                poi_files[f.stem] = len(poi_store.load(f.stem))
            except Exception:
                poi_files[f.stem] = -1
    # Overpass is optional (only used at ingest time); don't let it drag the
    # overall status to "degraded" if Valhalla + Nominatim are up.
    status = "healthy" if (val_ok and nom_ok) else "degraded"
    return {
        "status": status,
        "valhalla": val_ok,
        "nominatim": nom_ok,
        "overpass": ov_ok,
        "poi_collections": poi_files,
    }


# ---------------------------------------------------------------------------
# Custom routes
# ---------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> JSONResponse:
    """Lightweight health check (no backend calls)."""
    return JSONResponse(
        {"status": "healthy", "service": SERVICE_NAME, "version": "1.0.0"}
    )


def main() -> None:
    """Run the MCP server."""
    asyncio.run(
        mcp.run_http_async(
            host="0.0.0.0",
            port=settings.port,
            log_level="info",
        )
    )


if __name__ == "__main__":
    main()
