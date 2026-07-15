"""Settings for the maps-mcp-server.

Environment Variables:
    PORT: HTTP port the MCP server listens on (default: 8000).
    LOG_LEVEL: Logging level (default: INFO).
    VALHALLA_URL: Base URL of the Valhalla routing engine
        (default: http://valhalla.teams.svc.cluster.local:8002).
    NOMINATIM_URL: Base URL of the Nominatim geocoder
        (default: http://nominatim.teams.svc.cluster.local:8080).
    NOMINATIM_RATE_LIMIT_RPS: Max Nominatim requests/sec. The public
        Nominatim usage policy requires <= 1 req/s; the self-hosted
        instance has no such cap, but we throttle defensively (default 5.0).
    NOMINATIM_USER_AGENT: User-Agent header sent to Nominatim
        (default: jquad-maps-mcp).
    OVERPASS_URL: Endpoint for the Overpass API used by ingest_poi when
        source="overpass" (default: the public overpass-api.de). For
        production, point at a self-hosted instance or paid mirror.
    OVERPASS_TIMEOUT: Per-request timeout for Overpass queries (default 90s
        — European brand queries can be slow).
    POI_DATA_PATH: Directory where ingested POI JSON caches are stored
        (default: ./data/poi).
    POI_DEFAULT_COLLECTION: Default collection name for find/list
        operations (default: bestwestern-eu).
    POI_DEFAULT_BRAND: Default brand for ingest_poi(source="overpass")
        (default: Best Western).
    POI_DEFAULT_AREA: Default geographic scope for Overpass ingest
        (default: europe). Accepts a country code, list of codes, a bbox,
        or a prefixed form (name:<place> / around:<radius>,<lat>,<lon> /
        raw:<QL>) — see OverpassClient.build_brand_query.
    MAX_MATRIX_SIZE: Hard cap on the number of POIs sent to a single
        Valhalla sources_to_targets call; larger sets are chunked
        (default: 50).
    HTTP_TIMEOUT_SECONDS: Outbound HTTP timeout (default: 30.0).
    HTTP_CONNECT_TIMEOUT: Outbound connect timeout (default: 10.0).
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Defaults point at in-cluster sibling services deployed alongside this server.
DEFAULT_VALHALLA_URL = "http://valhalla.teams.svc.cluster.local:8002"
DEFAULT_NOMINATIM_URL = "http://nominatim.teams.svc.cluster.local:8080"
DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"


@dataclass(frozen=True)
class MapsSettings:
    """Settings for the maps MCP server.

    Attributes:
        port: HTTP listen port.
        valhalla_url: Base URL of the Valhalla routing engine.
        nominatim_url: Base URL of the Nominatim geocoder.
        nominatim_rate_limit_rps: Max Nominatim requests per second.
        nominatim_user_agent: User-Agent header sent to Nominatim and Overpass.
        overpass_url: Overpass /interpreter endpoint URL.
        overpass_timeout: Per-request timeout for Overpass queries (seconds).
        poi_data_path: Directory holding POI JSON caches.
        poi_default_collection: Default collection used by find_within/list_poi.
        poi_default_brand: Default brand for ingest_poi(source="overpass").
        poi_default_area: Default geographic scope for Overpass ingest.
        max_matrix_size: Max destinations per Valhalla distance-matrix call.
        http_timeout_seconds: Outbound HTTP total timeout.
        http_connect_timeout: Outbound HTTP connect timeout.
    """

    port: int = 8000
    valhalla_url: str = DEFAULT_VALHALLA_URL
    nominatim_url: str = DEFAULT_NOMINATIM_URL
    nominatim_rate_limit_rps: float = 5.0
    nominatim_user_agent: str = "jquad-maps-mcp"
    overpass_url: str = DEFAULT_OVERPASS_URL
    overpass_timeout: float = 90.0
    poi_data_path: Path = Path("./data/poi")
    poi_default_collection: str = "bestwestern-eu"
    poi_default_brand: str = "Best Western"
    poi_default_area: str = "europe"
    max_matrix_size: int = 50
    http_timeout_seconds: float = 30.0
    http_connect_timeout: float = 10.0

    @classmethod
    def from_env(cls) -> "MapsSettings":
        """Create settings from environment variables."""
        return cls(
            port=int(os.getenv("PORT", "8000")),
            valhalla_url=os.getenv("VALHALLA_URL", DEFAULT_VALHALLA_URL).rstrip("/"),
            nominatim_url=os.getenv("NOMINATIM_URL", DEFAULT_NOMINATIM_URL).rstrip("/"),
            nominatim_rate_limit_rps=float(
                os.getenv("NOMINATIM_RATE_LIMIT_RPS", "5.0")
            ),
            nominatim_user_agent=os.getenv("NOMINATIM_USER_AGENT", "jquad-maps-mcp"),
            overpass_url=os.getenv("OVERPASS_URL", DEFAULT_OVERPASS_URL),
            overpass_timeout=float(os.getenv("OVERPASS_TIMEOUT", "90.0")),
            poi_data_path=Path(os.getenv("POI_DATA_PATH", "./data/poi")),
            poi_default_collection=os.getenv(
                "POI_DEFAULT_COLLECTION", "bestwestern-eu"
            ),
            poi_default_brand=os.getenv("POI_DEFAULT_BRAND", "Best Western"),
            poi_default_area=os.getenv("POI_DEFAULT_AREA", "europe"),
            max_matrix_size=int(os.getenv("MAX_MATRIX_SIZE", "50")),
            http_timeout_seconds=float(os.getenv("HTTP_TIMEOUT_SECONDS", "30.0")),
            http_connect_timeout=float(os.getenv("HTTP_CONNECT_TIMEOUT", "10.0")),
        )


# Singleton instance
settings = MapsSettings.from_env()
