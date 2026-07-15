"""Tests for the Valhalla client — httpx mocked via respx.

respx is an httpx-specific mock library. If it is not installed, the tests
are skipped — they are not load-bearing for correctness of the regex
extraction (covered by test_poi_ingest.py) but verify the wire format.
"""

import asyncio

import httpx
import pytest

respx = pytest.importorskip("respx")

from src.services.valhalla_client import ValhallaClient, ValhallaError  # noqa: E402

BASE = "http://valhalla.test"


@pytest.fixture()
def client() -> ValhallaClient:
    return ValhallaClient(BASE, timeout=5.0, connect_timeout=2.0)


class TestIsochrone:
    @respx.mock
    def test_returns_geojson(self, client: ValhallaClient):
        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[11.0, 49.0], [11.5, 49.0], [11.5, 49.5], [11.0, 49.0]]],
                    },
                    "properties": {"time": 60},
                }
            ],
        }
        respx.post(f"{BASE}/isochrone").mock(return_value=httpx.Response(200, json=payload))
        data = asyncio.run(client.isochrone(49.45, 11.08, minutes=60))
        assert data["type"] == "FeatureCollection"
        assert len(data["features"]) == 1

    @respx.mock
    def test_status_error_raises(self, client: ValhallaClient):
        respx.post(f"{BASE}/isochrone").mock(
            return_value=httpx.Response(200, json={"status": 1, "status_message": "bad"})
        )
        with pytest.raises(ValhallaError):
            asyncio.run(client.isochrone(0, 0, minutes=60))

    @respx.mock
    def test_http_error_raises(self, client: ValhallaClient):
        respx.post(f"{BASE}/isochrone").mock(return_value=httpx.Response(500))
        with pytest.raises(ValhallaError):
            asyncio.run(client.isochrone(0, 0, minutes=60))


class TestRoute:
    @respx.mock
    def test_returns_trip_summary(self, client: ValhallaClient):
        payload = {"trip": {"summary": {"length": 123.4, "time": 5400}}}
        respx.post(f"{BASE}/route").mock(return_value=httpx.Response(200, json=payload))
        data = asyncio.run(client.route((49.45, 11.08), (49.30, 8.71)))
        assert data["trip"]["summary"]["length"] == 123.4
        assert data["trip"]["summary"]["time"] == 5400


class TestSourcesToTargets:
    @respx.mock
    def test_parses_matrix(self, client: ValhallaClient):
        payload = {
            "sources_to_targets": [
                [{"distance": 100.5, "time": 3600}, {"distance": 200.0, "time": 7200}],
                [{"distance": 0.0, "time": 0}],
            ]
        }
        respx.post(f"{BASE}/sources_to_targets").mock(
            return_value=httpx.Response(200, json=payload)
        )
        matrix = asyncio.run(
            client.sources_to_targets([(0, 0)], [(1, 1), (2, 2)])
        )
        assert matrix[0][0].distance_km == 100.5
        assert matrix[0][0].time_seconds == 3600
        assert matrix[0][1].distance_km == 200.0

    def test_empty_inputs_return_empty(self, client: ValhallaClient):
        result = asyncio.run(client.sources_to_targets([], [(1, 1)]))
        assert result == []

    @respx.mock
    def test_status_error_raises(self, client: ValhallaClient):
        respx.post(f"{BASE}/sources_to_targets").mock(
            return_value=httpx.Response(200, json={"status": 2, "status_message": "no"})
        )
        with pytest.raises(ValhallaError):
            asyncio.run(client.sources_to_targets([(0, 0)], [(1, 1)]))


class TestHealth:
    @respx.mock
    def test_healthy(self, client: ValhallaClient):
        respx.post(f"{BASE}/sources_to_targets").mock(
            return_value=httpx.Response(
                200, json={"sources_to_targets": [[{"distance": 0, "time": 0}]]}
            )
        )
        assert asyncio.run(client.health()) is True

    @respx.mock
    def test_unhealthy(self, client: ValhallaClient):
        respx.post(f"{BASE}/sources_to_targets").mock(return_value=httpx.Response(500))
        assert asyncio.run(client.health()) is False
