"""Tests for the Nominatim client — httpx mocked via respx."""

import asyncio
import re

import httpx
import pytest

respx = pytest.importorskip("respx")

from src.services.nominatim_client import NominatimClient, NominatimError  # noqa: E402

BASE = "http://nominatim.test"


@pytest.fixture()
def client() -> NominatimClient:
    # Very high rate limit so tests don't sleep.
    return NominatimClient(BASE, user_agent="test", rate_limit_rps=1000.0,
                           timeout=5.0, connect_timeout=2.0)


class TestGeocode:
    @respx.mock
    def test_returns_first_hit(self, client: NominatimClient):
        payload = [
            {"lat": "49.4521", "lon": "11.0767", "display_name": "Nürnberg, Bayern, Germany"}
        ]
        respx.get(url__regex=rf"{re.escape(BASE)}/search.*").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = asyncio.run(client.geocode("Nuremberg"))
        assert result.lat == pytest.approx(49.4521)
        assert result.lon == pytest.approx(11.0767)
        assert "Nürnberg" in result.display_name

    @respx.mock
    def test_empty_results_raise(self, client: NominatimClient):
        respx.get(url__regex=rf"{re.escape(BASE)}/search.*").mock(
            return_value=httpx.Response(200, json=[])
        )
        with pytest.raises(NominatimError):
            asyncio.run(client.geocode("nonexistent"))

    @respx.mock
    def test_http_error_raises(self, client: NominatimClient):
        respx.get(url__regex=rf"{re.escape(BASE)}/search.*").mock(
            return_value=httpx.Response(503)
        )
        with pytest.raises(NominatimError):
            asyncio.run(client.geocode("x"))

    @respx.mock
    def test_malformed_payload_raises(self, client: NominatimClient):
        respx.get(url__regex=rf"{re.escape(BASE)}/search.*").mock(
            return_value=httpx.Response(200, json=[{"no": "lat"}])
        )
        with pytest.raises(NominatimError):
            asyncio.run(client.geocode("x"))

    @respx.mock
    def test_countrycodes_param_forwarded(self, client: NominatimClient):
        route = respx.get(url__regex=rf"{re.escape(BASE)}/search.*").mock(
            return_value=httpx.Response(
                200, json=[{"lat": "0", "lon": "0", "display_name": "x"}]
            )
        )
        asyncio.run(client.geocode("Berlin", countrycodes="de"))
        request = httpx.Request("GET", route.calls.last.request.url)
        assert "countrycodes=de" in str(request.url)

    @respx.mock
    def test_user_agent_header(self, client: NominatimClient):
        route = respx.get(url__regex=rf"{re.escape(BASE)}/search.*").mock(
            return_value=httpx.Response(
                200, json=[{"lat": "0", "lon": "0", "display_name": "x"}]
            )
        )
        asyncio.run(client.geocode("x"))
        assert route.calls.last.request.headers["User-Agent"] == "test"


class TestReverseGeocode:
    @respx.mock
    def test_returns_address(self, client: NominatimClient):
        payload = {"lat": "49.45", "lon": "11.08", "display_name": "Nürnberg"}
        respx.get(url__regex=rf"{re.escape(BASE)}/reverse.*").mock(
            return_value=httpx.Response(200, json=payload)
        )
        result = asyncio.run(client.reverse_geocode(49.45, 11.08))
        assert result.lat == pytest.approx(49.45)
        assert "Nürnberg" in result.display_name

    @respx.mock
    def test_error_in_payload_raises(self, client: NominatimClient):
        respx.get(url__regex=rf"{re.escape(BASE)}/reverse.*").mock(
            return_value=httpx.Response(200, json={"error": "Unable to geocode"})
        )
        with pytest.raises(NominatimError):
            asyncio.run(client.reverse_geocode(0, 0))


class TestThrottle:
    def test_throttle_enforces_min_interval(self):
        c = NominatimClient(BASE, rate_limit_rps=2.0)  # 0.5s between calls
        import time
        asyncio.run(c._throttle())
        t0 = time.monotonic()
        asyncio.run(c._throttle())
        elapsed = time.monotonic() - t0
        # Second call immediately after the first must wait ~0.5s.
        assert elapsed >= 0.4

    def test_zero_rate_no_throttle(self):
        c = NominatimClient(BASE, rate_limit_rps=0.0)
        import time
        asyncio.run(c._throttle())
        t0 = time.monotonic()
        asyncio.run(c._throttle())
        assert time.monotonic() - t0 < 0.1


class TestHealth:
    @respx.mock
    def test_healthy(self, client: NominatimClient):
        respx.get(f"{BASE}/status").mock(return_value=httpx.Response(200))
        assert asyncio.run(client.health()) is True

    @respx.mock
    def test_unhealthy(self, client: NominatimClient):
        respx.get(f"{BASE}/status").mock(return_value=httpx.Response(500))
        assert asyncio.run(client.health()) is False
