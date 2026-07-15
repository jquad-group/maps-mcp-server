"""Tests for the Overpass client — httpx mocked via respx, plus pure
query-builder tests that need no network.
"""

import asyncio

import httpx
import pytest

respx = pytest.importorskip("respx")

from src.sources.overpass_client import (  # noqa: E402
    EUROPE_BBOX,
    OverpassClient,
    OverpassError,
    OverpassPOI,
    _parse_element,
    build_brand_query,
)

ENDPOINT = "https://overpass.test/api/interpreter"


# ---------------------------------------------------------------------------
# Query builder (pure)
# ---------------------------------------------------------------------------


class TestBuildBrandQuery:
    def test_europe_uses_bbox(self):
        q = build_brand_query("Best Western", area="europe")
        assert "[out:json]" in q
        # The space in the brand is widened to a regex (Best[s_-]?Western)
        # so it also matches Best-Western / Best_Western variants in OSM.
        assert "Best" in q and "Western" in q
        s, w, n, e = EUROPE_BBOX
        assert f"{s},{w},{n},{e}" in q
        assert "out center tags" in q

    def test_country_code(self):
        q = build_brand_query("Best Western", area="DE")
        assert 'ISO3166-1"="DE"' in q
        assert "(area.a)" in q

    def test_country_list(self):
        q = build_brand_query("Best Western", area=["DE", "AT"])
        assert 'ISO3166-1"="DE"' in q
        assert 'ISO3166-1"="AT"' in q
        # Each country gets its own area variable.
        assert ".a_DE" in q and ".a_AT" in q

    def test_custom_bbox(self):
        bbox = (50.0, 10.0, 51.0, 11.0)
        q = build_brand_query("Best Western", area=bbox)
        assert "50.0,10.0,51.0,11.0" in q

    def test_brand_case_insensitive_flag(self):
        q = build_brand_query("Best Western", area="europe")
        assert '",i]' in q  # the case-insensitive regex flag

    def test_brand_space_widened_to_regex(self):
        # Space in brand becomes [s_-]? so Best-Western / Best_Western match.
        q = build_brand_query("Best Western", area="europe")
        assert r"[\\s_-]?" in q

    def test_timeout_embedded(self):
        q = build_brand_query("Best Western", area="europe", timeout=120)
        assert "timeout:120" in q
        assert "timeout:120" in q

    # --- Prefixed area forms --------------------------------------------

    def test_named_area(self):
        # name:München → area["name"="München"]
        q = build_brand_query("Best Western", area="name:München")
        assert '"name"="München"' in q
        assert "(area.search_area)" in q

    def test_named_area_escapes_quotes(self):
        # A double quote in the place name must be escaped, not break out
        # of the QL string literal.
        q = build_brand_query("X", area='name:evil"place')
        assert '"name"="evil\\"place"' in q

    def test_named_area_strips_control_chars(self):
        q = build_brand_query("X", area="name:bad\nplace")
        # The newline is stripped before embedding; the value is "badplace".
        assert '"name"="badplace"' in q

    def test_named_area_empty_raises(self):
        with pytest.raises(ValueError, match="requires a place name"):
            build_brand_query("X", area="name:")

    def test_around_radius_metres(self):
        q = build_brand_query("Best Western", area="around:30000,48.137,11.575")
        assert "around:30000,48.137,11.575" in q
        assert "out center tags" in q

    def test_around_radius_km_suffix(self):
        q = build_brand_query("Best Western", area="around:30 km,48.1372,11.5754")
        # 30 km → 30000 m
        assert "around:30000,48.1372,11.5754" in q

    def test_around_radius_mi_suffix(self):
        q = build_brand_query("Best Western", area="around:5 mi,48.137,11.575")
        # 5 mi ≈ 8046.72 m
        assert "around:8046.7" in q

    def test_around_bad_shape_raises(self):
        with pytest.raises(ValueError, match="around:RADIUS,LAT,LON"):
            build_brand_query("X", area="around:30000,48.137")  # missing lon

    def test_around_bad_coords_raises(self):
        with pytest.raises(ValueError):
            build_brand_query("X", area="around:30000,999,11.575")  # lat > 90

    def test_raw_predicate(self):
        q = build_brand_query("X", area="raw:(area.boundary_of_berlin)")
        assert "(area.boundary_of_berlin)" in q

    def test_bare_non_country_string_raises(self):
        # The old bug: "München" used to silently build a no-match
        # area["ISO3166-1"="MÜNCHEN"] query. It must now raise.
        with pytest.raises(ValueError, match="Cannot interpret area"):
            build_brand_query("Best Western", area="München")

    def test_bare_two_letter_country_still_works(self):
        q = build_brand_query("Best Western", area="DE")
        assert 'ISO3166-1"="DE"' in q
        assert "(area.a)" in q


# ---------------------------------------------------------------------------
# Element parsing (pure)
# ---------------------------------------------------------------------------


class TestParseElement:
    def test_node_with_address(self):
        el = {
            "type": "node",
            "id": 12345,
            "lat": 51.1681,
            "lon": 6.9307,
            "tags": {
                "name": "Best Western Hilden",
                "brand": "Best Western",
                "addr:street": "Schwanenstr.",
                "addr:housenumber": "27",
                "addr:postcode": "40721",
                "addr:city": "Hilden",
                "addr:country": "DE",
                "phone": "+49 2103 5030",
                "tourism": "hotel",
            },
        }
        poi = _parse_element(el)
        assert poi is not None
        assert poi.name == "Best Western Hilden"
        assert poi.lat == pytest.approx(51.1681)
        assert poi.lon == pytest.approx(6.9307)
        assert poi.street == "Schwanenstr."
        assert poi.housenumber == "27"
        assert poi.postcode == "40721"
        assert poi.city == "Hilden"
        assert poi.country == "DE"
        assert poi.phone == "+49 2103 5030"
        assert poi.osm_id == "node:12345"
        # address is the postal address, not the brand name.
        assert "Hilden" in poi.address and "Schwanenstr." in poi.address

    def test_way_uses_center(self):
        el = {
            "type": "way",
            "id": 999,
            "center": {"lat": 49.45, "lon": 11.08},
            "tags": {"name": "A Hotel", "brand": "Best Western"},
        }
        poi = _parse_element(el)
        assert poi is not None
        assert poi.lat == pytest.approx(49.45)
        assert poi.lon == pytest.approx(11.08)
        assert poi.osm_type == "way"

    def test_unnamed_skipped(self):
        assert _parse_element({"type": "node", "id": 1, "lat": 0, "lon": 0, "tags": {}}) is None
        assert _parse_element({"type": "node", "id": 1, "lat": 0, "lon": 0, "tags": {"name": ""}}) is None

    def test_missing_coords_skipped(self):
        assert _parse_element({"type": "node", "id": 1, "tags": {"name": "X"}}) is None

    def test_address_property_partial(self):
        poi = OverpassPOI(osm_id="n:1", osm_type="node", name="X",
                          lat=0, lon=0, city="Berlin", country="DE")
        assert poi.address == "Berlin, DE"


# ---------------------------------------------------------------------------
# fetch_brand (httpx mocked)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> OverpassClient:
    return OverpassClient(ENDPOINT, user_agent="test", timeout=10.0, connect_timeout=5.0)


class TestFetchBrand:
    @respx.mock
    def test_returns_parsed_pois(self, client: OverpassClient):
        payload = {
            "elements": [
                {"type": "node", "id": 1, "lat": 51.0, "lon": 6.0,
                 "tags": {"name": "Hotel A", "brand": "Best Western"}},
                {"type": "node", "id": 2, "lat": 52.0, "lon": 7.0,
                 "tags": {"name": "Hotel B", "brand": "Best Western"}},
            ]
        }
        respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        pois = asyncio.run(client.fetch_brand("Best Western", area="europe"))
        assert len(pois) == 2
        assert {p.name for p in pois} == {"Hotel A", "Hotel B"}

    @respx.mock
    def test_dedupes_by_osm_id(self, client: OverpassClient):
        payload = {
            "elements": [
                {"type": "node", "id": 1, "lat": 51.0, "lon": 6.0,
                 "tags": {"name": "Hotel A", "brand": "Best Western"}},
                {"type": "node", "id": 1, "lat": 51.0, "lon": 6.0,
                 "tags": {"name": "Hotel A (dup)", "brand": "Best Western"}},
            ]
        }
        respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        pois = asyncio.run(client.fetch_brand("Best Western", area="europe"))
        assert len(pois) == 1

    @respx.mock
    def test_skips_unnamed(self, client: OverpassClient):
        payload = {
            "elements": [
                {"type": "node", "id": 1, "lat": 51.0, "lon": 6.0, "tags": {}},
                {"type": "node", "id": 2, "lat": 52.0, "lon": 7.0,
                 "tags": {"name": "Hotel B", "brand": "Best Western"}},
            ]
        }
        respx.post(ENDPOINT).mock(return_value=httpx.Response(200, json=payload))
        pois = asyncio.run(client.fetch_brand("Best Western", area="europe"))
        assert len(pois) == 1
        assert pois[0].name == "Hotel B"

    @respx.mock
    def test_rate_limit_raises(self, client: OverpassClient):
        respx.post(ENDPOINT).mock(return_value=httpx.Response(429))
        with pytest.raises(OverpassError, match="rate limit"):
            asyncio.run(client.fetch_brand("Best Western"))

    @respx.mock
    def test_gateway_timeout_raises(self, client: OverpassClient):
        respx.post(ENDPOINT).mock(return_value=httpx.Response(504))
        with pytest.raises(OverpassError, match="gateway timeout"):
            asyncio.run(client.fetch_brand("Best Western"))

    @respx.mock
    def test_remark_error_raises(self, client: OverpassClient):
        respx.post(ENDPOINT).mock(return_value=httpx.Response(
            200, json={"elements": [], "remark": "error: syntax error at line 1"}
        ))
        with pytest.raises(OverpassError, match="syntax error"):
            asyncio.run(client.fetch_brand("Best Western"))

    @respx.mock
    def http_error_raises(self, client: OverpassClient):
        respx.post(ENDPOINT).mock(return_value=httpx.Response(500))
        with pytest.raises(OverpassError):
            asyncio.run(client.fetch_brand("Best Western"))

    @respx.mock
    def test_non_json_raises(self, client: OverpassClient):
        respx.post(ENDPOINT).mock(return_value=httpx.Response(200, text="not json"))
        with pytest.raises(OverpassError):
            asyncio.run(client.fetch_brand("Best Western"))

    @respx.mock
    def test_user_agent_sent(self, client: OverpassClient):
        route = respx.post(ENDPOINT).mock(return_value=httpx.Response(
            200, json={"elements": []}
        ))
        asyncio.run(client.fetch_brand("Best Western"))
        assert route.calls.last.request.headers["User-Agent"] == "test"

    @respx.mock
    def test_query_posted_as_body(self, client: OverpassClient):
        route = respx.post(ENDPOINT).mock(return_value=httpx.Response(
            200, json={"elements": []}
        ))
        asyncio.run(client.fetch_brand("Best Western", area="DE"))
        body = route.calls.last.request.content.decode("utf-8")
        # The space is widened to a regex, but both halves of the brand
        # appear in the query body.
        assert "Best" in body and "Western" in body
        assert "DE" in body


class TestHealth:
    @respx.mock
    def test_healthy(self, client: OverpassClient):
        respx.get("https://overpass.test/api/status").mock(
            return_value=httpx.Response(200)
        )
        assert asyncio.run(client.health()) is True

    @respx.mock
    def test_unhealthy(self, client: OverpassClient):
        respx.get("https://overpass.test/api/status").mock(
            return_value=httpx.Response(500)
        )
        assert asyncio.run(client.health()) is False
