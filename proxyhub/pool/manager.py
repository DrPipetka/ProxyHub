from __future__ import annotations

import asyncio
import json
import random
import time

from ..core.adapter import CoreError
from ..core.xray import PortAllocator
from .lease import ServiceError


class PoolManager:
    def __init__(self, db, settings, core):
        self.db, self.settings, self.core = db, settings, core
        self.lock = asyncio.Lock()
        self.ports = PortAllocator(settings.pool_port_range, settings.bind_address)

    def get(self, proxy_id):
        row = self.db.one("SELECT * FROM proxies WHERE id=?", (proxy_id,))
        if not row:
            raise ServiceError("not_found", 404)
        return row

    def resolve(self, proxy_id=None, addr=None):
        if proxy_id is not None:
            return self.get(proxy_id)
        if not addr:
            raise ServiceError("id_or_addr_required")
        try:
            host, port = addr.rsplit(":", 1)
            if host.strip("[]") != self.settings.advertise_host:
                raise ValueError()
            row = self.db.one("SELECT * FROM proxies WHERE port=?", (int(port),))
        except (ValueError, AttributeError):
            raise ServiceError("invalid_addr") from None
        if not row:
            raise ServiceError("not_found", 404)
        return row

    def candidates(self, states, filters=None, limit=1):
        f, s = filters or {}, self.settings
        where = ["state IN (" + ",".join("?" for _ in states) + ")", "checking=0"]
        args = list(states)
        countries = f.get("geo") or (s.pool_geo if s.select_mode == "geo" else [])
        if countries:
            where.append("geo IN (" + ",".join("?" for _ in countries) + ")")
            args.extend(countries)
        for key, operator in (("max_ping", "<="), ("min_speed", ">=")):
            if f.get(key) is not None:
                where.append(("ping_ms" if key == "max_ping" else "speed_mbps") + operator + "?")
                args.append(f[key])
        order = {"ping": "COALESCE(ping_ms,1e12) ASC", "speed": "COALESCE(speed_mbps,0) DESC",
                 "balanced": "COALESCE(speed_mbps,1)/(1+COALESCE(ping_ms,100000)/100.0) DESC"}[s.best_sort]
        mode = f.get("mode") or s.select_mode
        if mode == "random":
            order = "RANDOM()"
        amount = max(limit, f.get("top") or s.top_x) if mode == "random_top" else limit
        rows = self.db.rows("SELECT * FROM proxies WHERE " + " AND ".join(where) + " ORDER BY " + order + ",id LIMIT ?", (*args, amount))
        if mode == "random_top":
            random.shuffle(rows)
        return rows[:limit]

    async def _raise(self, row):
        if not self.core.ready:
            raise ServiceError("core_unavailable", 503)
        port = self.ports.take()
        try:
            await self.core.add(row["id"], json.loads(row["config"]), port)
        except BaseException:
            self.ports.release(port)
            raise
        now = time.time()
        self.db.execute("UPDATE proxies SET state='active',port=?,next_check_at=MIN(next_check_at,?),updated_at=? WHERE id=?", (port, now + self.settings.recheck_active_minutes * 60, now, row["id"]))
        self.db.event("valid → active", row["id"])
        return self.get(row["id"])

    async def _close(self, row):
        if row["port"] is not None:
            await self.core.remove(row["id"])
            self.ports.release(row["port"])
            self.db.execute("UPDATE proxies SET port=NULL WHERE id=?", (row["id"],))

    async def acquire(self, config=False, lease=True, filters=None, ttl=None):
        async with self.lock:
            rows = self.candidates(("active", "valid") if config else ("active",), filters)
            if not rows and not config and self.settings.on_demand:
                rows = self.candidates(("valid",), filters)
            if not rows:
                raise ServiceError("no_proxy_available", 404)
            row = rows[0]
            if not config and row["state"] == "valid":
                row = await self._raise(row)
            if lease:
                if config:
                    await self._close(row)
                now = time.time()
                self.db.execute("UPDATE proxies SET state='leased',lease_expires_at=?,lease_status='open',next_check_at=MIN(next_check_at,?),revision=revision+1,updated_at=? WHERE id=?", (now + (ttl or self.settings.lease_ttl), now + self.settings.recheck_active_minutes * 60, now, row["id"]))
                self.db.event(row["state"] + " → leased", row["id"])
                row = self.get(row["id"])
            return row

    async def action(self, action, proxy_id=None, addr=None, reason=None):
        async with self.lock:
            row = self.resolve(proxy_id, addr)
            pid, now = row["id"], time.time()
            if action == "used":
                if row["state"] not in ("leased", "active"):
                    raise ServiceError("not_leased_or_active", 409)
                await self._cooldown(row, now)
            elif action == "invalid":
                await self._fail(row, reason or "manual")
            elif action == "disable":
                await self._close(row)
                self.db.execute("UPDATE proxies SET state='disabled',checking=0,manual_check=0,revision=revision+1,lease_expires_at=NULL,lease_status='broken' WHERE id=?", (pid,))
                self.db.release_duplicates(pid)
            elif action in ("enable", "recheck"):
                if row["config"] is None:
                    raise ServiceError("config_not_parseable", 409)
                await self._close(row)
                self.db.execute("UPDATE proxies SET state='new',reason=NULL,master_id=NULL,next_check_at=0,checking=0,manual_check=?,revision=revision+1,lease_expires_at=NULL,lease_status='broken',cooldown_until=NULL WHERE id=?", (int(action == "recheck"), pid))
                self.db.release_duplicates(pid)
            elif action == "release":
                if row["state"] != "leased":
                    raise ServiceError("not_leased", 409)
                self.db.execute("UPDATE proxies SET state=?,lease_expires_at=NULL,lease_status='released',revision=revision+1 WHERE id=?", ("active" if row["port"] else "valid", pid))
            else:
                raise ServiceError("unknown_action")
            self.db.event(row["state"] + " → " + self.get(pid)["state"], pid)
            return self.get(pid)

    async def _cooldown(self, row, now):
        await self._close(row)
        self.db.execute("UPDATE proxies SET state='cooldown',cooldown_until=?,lease_expires_at=NULL,lease_status='ended',checking=0,revision=revision+1,updated_at=? WHERE id=?", (now + self.settings.cooldown_minutes * 60, now, row["id"]))

    async def _fail(self, row, reason):
        await self._close(row)
        fails = row["fails"] + 1
        state = "blacklisted" if self.settings.blacklist_after_fails and fails >= self.settings.blacklist_after_fails else "invalid"
        self.db.execute("UPDATE proxies SET state=?,reason=?,fails=?,next_check_at=?,lease_expires_at=NULL,cooldown_until=NULL,lease_status=CASE WHEN state='leased' THEN 'broken' ELSE lease_status END,checking=0,revision=revision+1,updated_at=? WHERE id=?",
                        (state, reason, fails, time.time() + self.settings.fail_retry_minutes * 60, time.time(), row["id"]))
        self.db.release_duplicates(row["id"])

    async def tick(self):
        async with self.lock:
            now = time.time()
            for row in self.db.rows("SELECT * FROM proxies WHERE state='leased' AND lease_expires_at<=?", (now,)):
                await self._cooldown(row, now)
                self.db.event("leased → cooldown (TTL)", row["id"])
            recheck = self.settings.recheck_after_cooldown
            self.db.execute("UPDATE proxies SET state=?,cooldown_until=NULL,next_check_at=?,revision=revision+1 WHERE state='cooldown' AND cooldown_until<=?", ("new" if recheck else "valid", 0 if recheck else now + self.settings.recheck_valid_minutes * 60, now))
            if not self.core.ready:
                return
            active = self.db.one("SELECT COUNT(*) n FROM proxies WHERE state='active'")["n"]
            if active > self.settings.pool_size:
                for row in self.db.rows("SELECT * FROM proxies WHERE state='active' AND checking=0 ORDER BY id DESC LIMIT ?", (active - self.settings.pool_size,)):
                    await self._close(row)
                    self.db.execute("UPDATE proxies SET state='valid',revision=revision+1 WHERE id=?", (row["id"],))
            elif active < self.settings.pool_size:
                for row in self.candidates(("valid",), limit=self.settings.pool_size - active):
                    if len(self.ports.used) >= self.ports.span[1] - self.ports.span[0] + 1:
                        break
                    try:
                        await self._raise(row)
                    except CoreError:
                        self.db.event("Не удалось поднять SOCKS; проверьте ядро и порты", row["id"], "ERROR")
                        break

    async def delete(self, scope="all", source=None):
        async with self.lock:
            where, args = "1=1", ()
            if scope == "invalid":
                where = "state IN ('invalid','blacklisted','parse_error','unsupported','duplicate')"
            elif scope == "source":
                if source is None:
                    raise ServiceError("source_required")
                where, args = "source=?", (source,)
            elif scope != "all":
                raise ServiceError("invalid_scope")
            for row in self.db.rows("SELECT * FROM proxies WHERE port IS NOT NULL AND " + where, args):
                await self._close(row)
            # SQL subquery avoids materializing a 100k-row deletion set.
            with self.db.transaction() as c:
                deleted = c.execute("DELETE FROM proxies WHERE " + where, args).rowcount
                c.execute("UPDATE proxies SET state='new',master_id=NULL,next_check_at=0,revision=revision+1 WHERE state='duplicate' AND NOT EXISTS (SELECT 1 FROM proxies master WHERE master.id=proxies.master_id)")
            return deleted
