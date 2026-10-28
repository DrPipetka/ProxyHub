import httpx

from proxyhub.checker.pipeline import Pipeline, Result
from proxyhub.config import Settings


class Geo:
    def country(self, ip):
        return "DE"


def mock_pipeline(settings, handler, direct=None):
    pipeline = Pipeline(settings, Geo(), direct)
    pipeline.client = lambda port=None: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return pipeline


async def test_disabled_measurements_ignored():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(204)
    p = mock_pipeline(Settings(measure_geo=False, measure_ping=False, measure_speed=False, dedupe_by_exit_ip=False, allowed_geo=["US"], max_ping=0, min_speed=999), handler)
    result = await p.cheap(1)
    await p.speed(1, result)
    assert result.ok and result.ping_ms is None and result.speed_mbps is None
    assert len(calls) == 1


async def test_leak_and_geo_results_preserved():
    def handler(request):
        return httpx.Response(200, text="1.2.3.4") if "ipify" in str(request.url) else httpx.Response(204)
    p = mock_pipeline(Settings(require_no_leak=True), handler, "1.2.3.4")
    r = await p.cheap(1)
    assert not r.ok and r.reason == "leak" and r.geo == "DE" and r.ping_ms is not None


async def test_url_fallback_and_geo_blacklist():
    def handler(request):
        if "cloudflare" in str(request.url):
            return httpx.Response(503)
        if "ipify" in str(request.url):
            return httpx.Response(200, json={"ip": "5.6.7.8"})
        return httpx.Response(204)
    p = mock_pipeline(Settings(allowed_geo=["DE"], geo_blacklist=True), handler)
    r = await p.cheap(1)
    assert r.reason == "geo_mismatch" and r.exit_ip == "5.6.7.8"


async def test_unknown_direct_ip_is_test_error():
    p = mock_pipeline(Settings(require_no_leak=True), lambda r: httpx.Response(200, text="1.2.3.4"))
    r = await p.cheap(1)
    assert r.test_error and r.reason == "direct_ip_unavailable"
