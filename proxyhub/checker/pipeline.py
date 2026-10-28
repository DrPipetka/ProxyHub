from __future__ import annotations

import asyncio
import ipaddress
import json
import statistics
import time
from dataclasses import asdict, dataclass

import httpx

from .udp import check_udp


@dataclass
class Result:
    ok: bool = False
    reason: str | None = None
    ping_ms: float | None = None
    speed_mbps: float | None = None
    geo: str | None = None
    exit_ip: str | None = None
    udp: bool | None = None
    test_error: bool = False
    stage: str | None = None
    master_id: int | None = None

    def asdict(self):
        return asdict(self)


async def small_request(client, url, read_body=False):
    async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as r:
        r.raise_for_status()
        if not 200 <= r.status_code < 300:
            raise httpx.HTTPError("unexpected_status")
        if not read_body:
            return ""
        body = bytearray()
        async for chunk in r.aiter_bytes():
            body.extend(chunk)
            if len(body) > 65536:
                raise ValueError("echo_response_too_large")
        return body.decode().strip()


async def echo_ip(client, urls):
    for url in urls:
        try:
            text = await small_request(client, url, True)
            if text.startswith("{"):
                v = json.loads(text)
                text = v.get("ip", v.get("origin", "")).split(",")[0].strip()
            return str(ipaddress.ip_address(text))
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            continue
    return None


class Pipeline:
    def __init__(self, settings, geo, direct_ip=None):
        self.s, self.geo, self.direct_ip = settings, geo, direct_ip

    def client(self, port=None):
        return httpx.AsyncClient(proxy=f"socks5://127.0.0.1:{port}" if port else None, timeout=self.s.connect_timeout,
                                 follow_redirects=True, trust_env=False)

    async def cheap(self, port):
        s, result = self.s, Result()
        async with self.client(port) as client:
            samples = None
            timed_out = False
            for url in s.ping_urls:
                try:
                    await small_request(client, url)
                    values = []
                    if s.measure_ping:
                        for _ in range(s.ping_samples):
                            start = time.perf_counter()
                            await small_request(client, url)
                            values.append((time.perf_counter() - start) * 1000)
                    samples = values
                    break
                except httpx.TimeoutException:
                    timed_out = True
                except (httpx.HTTPError, ValueError):
                    continue
            if samples is None:
                result.reason, result.stage = "timeout" if timed_out else "connect_fail", "ping"
                return result
            if samples:
                result.ping_ms = statistics.median(samples)
            if s.measure_geo or s.require_no_leak or s.dedupe_by_exit_ip:
                result.exit_ip = await echo_ip(client, s.ip_echo_urls)
                if not result.exit_ip:
                    result.reason, result.stage = "connect_fail", "ip"
                    return result
            if s.measure_geo:
                try:
                    result.geo = self.geo.country(result.exit_ip)
                except RuntimeError:
                    result.reason, result.test_error = "geo_database_unavailable", True
                    return result
            if s.require_no_leak:
                if not self.direct_ip:
                    result.reason, result.test_error = "direct_ip_unavailable", True
                    return result
                if result.exit_ip == self.direct_ip:
                    result.reason = "leak"
                    return result
            if s.check_udp:
                result.udp = await check_udp(port, s.connect_timeout)
                if s.require_udp and not result.udp:
                    result.reason = "udp_fail"
                    return result
            if s.measure_ping and result.ping_ms > s.max_ping:
                result.reason = "ping_high"
                return result
            if s.measure_geo and s.allowed_geo:
                matched = result.geo in s.allowed_geo
                if (matched and s.geo_blacklist) or (not matched and not s.geo_blacklist):
                    result.reason = "geo_mismatch"
                    return result
            result.ok = True
            return result

    async def speed(self, port, result):
        if not self.s.measure_speed or not result.ok:
            return result
        async with self.client(port) as client:
            for url in self.s.speed_urls:
                size, start, status_ok = 0, time.perf_counter(), False
                try:
                    async with asyncio.timeout(self.s.speed_timeout):
                        async with client.stream("GET", url, headers={"Accept-Encoding": "identity"}) as r:
                            r.raise_for_status()
                            if not 200 <= r.status_code < 300:
                                continue
                            status_ok = True
                            limit = int(self.s.speed_max_mb * 1_000_000)
                            async for chunk in r.aiter_raw(chunk_size=16384):
                                size += min(len(chunk), limit - size)
                                if size >= limit:
                                    break
                except (httpx.HTTPError, TimeoutError):
                    pass
                if status_ok and size:
                    result.speed_mbps = size * 8 / max(time.perf_counter() - start, 0.001) / 1_000_000
                    if result.speed_mbps < self.s.min_speed:
                        result.ok, result.reason = False, "speed_low"
                    return result
            result.ok, result.reason, result.stage = False, "connect_fail", "speed"
            return result

    async def test_direct(self, stage):
        async with self.client() as client:
            if stage == "ip":
                return bool(await echo_ip(client, self.s.ip_echo_urls))
            for url in self.s.speed_urls if stage == "speed" else self.s.ping_urls:
                try:
                    await small_request(client, url)
                    return True
                except (httpx.HTTPError, ValueError):
                    pass
        return False
