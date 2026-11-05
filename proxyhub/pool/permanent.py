from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import quote

import httpx

from ..checker.pipeline import small_request
from ..core.adapter import CoreError
from .lease import ServiceError


class PermanentProxy:
    # Database IDs start at 1. This inbound is independent of leases and pool ports.
    CORE_ID = 0

    def __init__(self, pool):
        self.pool = pool
        self.reset()

    def reset(self):
        self.proxy_id = None
        self.port = None
        self.healthy = False
        self.last_check_at = None
        self.error = None
        self.next_check = 0
        self.retry_at = 0
        self.generation = getattr(self, "generation", 0) + 1

    def status(self):
        p, s = self.pool, self.pool.settings
        host = s.advertise_host
        row = p.db.one("SELECT id,remark,geo,host FROM proxies WHERE id=?", (self.proxy_id,))
        return {"enabled": s.permanent_proxy, "port": s.permanent_port,
                "addr": (f"[{host}]" if ":" in host else host) + f":{s.permanent_port}",
                "ready": bool(p.enabled and p.core.ready and self.healthy and self.port == s.permanent_port),
                "proxy": row, "last_check_at": self.last_check_at, "error": self.error}

    async def stop(self):
        if self.port is not None:
            await self.pool.core.remove(self.CORE_ID)
        self.reset()

    async def select(self, manual=False):
        """Caller holds pool.lock. Keep the old inbound if no alternative exists."""
        p, s = self.pool, self.pool.settings
        if not p.enabled:
            raise ServiceError("service_disabled", 503)
        if not s.permanent_proxy:
            raise ServiceError("permanent_proxy_disabled", 409, "Включите постоянный прокси в настройках")
        if not p.core.ready:
            raise ServiceError("core_unavailable", 503)
        rows = p.candidates(("valid",), limit=20) or p.candidates(("active",), limit=20)
        if not rows:
            raise ServiceError("no_alternative_config" if self.proxy_id else "no_proxy_available", 409 if self.proxy_id else 404,
                               "Другого рабочего конфига пока нет" if self.proxy_id else "Ожидание проверенного рабочего конфига")
        for row in rows:
            try:
                if row["port"] is not None:
                    await p._close(row)
                    p.db.execute("UPDATE proxies SET state='valid',revision=revision+1 WHERE id=?", (row["id"],))
                config = json.loads(row["config"])
                if self.port is not None:
                    await p.core.replace(self.CORE_ID, config, self.port)
                else:
                    await p.core.add(self.CORE_ID, config, s.permanent_port)
                break
            except CoreError:
                continue
        else:
            raise ServiceError("permanent_proxy_start_failed", 503, "Не удалось запустить постоянный прокси; проверьте порт и журнал")
        self.proxy_id, self.port = row["id"], s.permanent_port
        self.generation += 1
        self.healthy, self.error = False, None
        self.last_check_at, self.next_check = None, 0
        self.retry_at = 0
        p.db.event("Постоянный прокси: " + ("ручная смена конфига" if manual else "выбран конфиг"), self.proxy_id)
        return self.status()

    async def switch(self):
        async with self.pool.lock:
            return await self.select(manual=True)

    async def probe(self, settings, port=None):
        host = settings.bind_address
        host = "127.0.0.1" if host == "0.0.0.0" else "::1" if host == "::" else host
        auth = (quote(settings.socks_username, safe="") + ":" + quote(settings.socks_password, safe="") + "@") if settings.socks_auth else ""
        proxy = f"socks5://{auth}{'[' + host + ']' if ':' in host else host}:{port}" if port is not None else None
        async with httpx.AsyncClient(proxy=proxy, timeout=settings.connect_timeout, follow_redirects=True, trust_env=False) as client:
            for url in settings.ping_urls:
                try:
                    await small_request(client, url)
                    return True
                except (httpx.HTTPError, ValueError):
                    continue
        return False

    async def tick(self):
        p = self.pool
        async with p.lock:
            s = p.settings
            if not p.enabled or not s.permanent_proxy:
                await self.stop()
                return
            if not p.core.ready:
                return
            if self.port is not None and self.port != s.permanent_port:
                await self.stop()
            if self.proxy_id is None:
                if time.monotonic() < self.retry_at:
                    return
                try:
                    await self.select()
                except ServiceError as exc:
                    self.error = exc.code
                    self.retry_at = time.monotonic() + 5
                    return
            if time.monotonic() < self.next_check:
                return
            generation, pid, port = self.generation, self.proxy_id, self.port
            self.next_check = time.monotonic() + s.permanent_check_seconds
        # Network checks must not block pool operations or service controls.
        healthy = await self.probe(s, port)
        test_available = healthy or await self.probe(s)
        async with p.lock:
            if (generation, pid, port) != (self.generation, self.proxy_id, self.port) or s is not p.settings:
                return
            self.healthy, self.last_check_at = healthy, time.time()
            self.error = None if healthy else "connect_fail"
            if not healthy:
                if not test_available:
                    self.error = "test_url_unavailable"
                    return
                failed = p.get(pid)
                try:
                    await self.select()
                except ServiceError:
                    pass
                await p._fail(failed, "connect_fail")
                self.error = "connect_fail"
                p.db.event("Постоянный прокси: соединение недоступно, выполняется восстановление", pid, "WARNING")

    async def run(self):
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = "permanent_proxy_error"
                self.pool.db.event("Постоянный прокси: " + type(exc).__name__, level="ERROR")
            await asyncio.sleep(1)
