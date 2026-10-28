from __future__ import annotations

import asyncio
import json
import time
from collections import deque

from ..core.adapter import CoreError, NoPorts
from ..core.xray import PortAllocator, build_config
from ..parsers.normalize import ProxyConfig
from .pipeline import Pipeline, Result, echo_ip


class Scheduler:
    def __init__(self, db, pool, core, geo):
        self.db, self.pool, self.core, self.geo = db, pool, core, geo
        self.ports = PortAllocator(pool.settings.check_port_range)
        self.tasks = set()
        self.running = 0
        self.direct_ip = None
        self.direct_checked = 0
        self.completed = deque(maxlen=10000)
        self.reservations = {}

    @property
    def settings(self):
        return self.pool.settings

    async def refresh_direct(self):
        p = Pipeline(self.settings, self.geo)
        async with p.client() as client:
            self.direct_ip = await echo_ip(client, self.settings.ip_echo_urls)
        self.direct_checked = time.time()
        if self.direct_ip is None:
            self.db.event("Прямой IP определить не удалось", level="WARNING")

    async def claim(self, limit):
        async with self.pool.lock:
            if not self.pool.enabled:
                return []
            found = []
            now = time.time()
            # Each branch uses state/schedule indexes. Never load the full queue.
            queries = [("manual_check=1", ()),
                       ("state IN ('active','leased') AND next_check_at<=?", (now,)),
                       ("state='new' AND next_check_at<=?", (now,)),
                       ("state='valid' AND next_check_at<=?", (now,)),
                       ("state='invalid' AND next_check_at<=?", (now,))]
            with self.db.transaction() as c:
                for where, args in queries:
                    if len(found) >= limit:
                        break
                    rows = c.execute("SELECT * FROM proxies WHERE checking=0 AND config IS NOT NULL AND " + where + " ORDER BY next_check_at,id LIMIT ?", (*args, limit - len(found))).fetchall()
                    for r in rows:
                        row = dict(r)
                        row["revision"] += 1
                        c.execute("UPDATE proxies SET checking=1,manual_check=0,revision=revision+1,state=CASE WHEN state IN ('active','leased') THEN state ELSE 'checking' END WHERE id=?", (row["id"],))
                        found.append(row)
            return found

    async def finish(self, original, result):
        async with self.pool.lock:
            row = self.db.one("SELECT * FROM proxies WHERE id=?", (original["id"],))
            if not row or row["revision"] != original["revision"] or row["created_at"] != original["created_at"]:
                return
            now, pid = time.time(), row["id"]
            if result.test_error:
                self.db.execute("UPDATE proxies SET state=?,checking=0,next_check_at=?,reason=? WHERE id=?",
                                (original["state"], now + 60, result.reason, pid))
                self.db.event("Ошибка теста: " + (result.reason or "unavailable"), pid, "WARNING")
                return
            if row["exit_ip"] and result.exit_ip != row["exit_ip"]:
                self.db.release_duplicates(pid)
            self.db.execute("UPDATE proxies SET geo=?,exit_ip=?,ping_ms=?,speed_mbps=?,udp=?,last_check_at=?,updated_at=?,checking=0 WHERE id=?",
                            (result.geo, result.exit_ip, result.ping_ms, result.speed_mbps, result.udp, now, now, pid))
            if result.master_id:
                master = self.db.one("SELECT state,checking,exit_ip FROM proxies WHERE id=?", (result.master_id,))
                await self.pool._close(row)
                if master and ((master["state"] in ("valid", "active", "leased", "cooldown") and master["exit_ip"] == result.exit_ip) or master["checking"]):
                    self.db.execute("UPDATE proxies SET state='duplicate',master_id=?,reason='duplicate_exit_ip',lease_expires_at=NULL,lease_status=CASE WHEN state='leased' THEN 'broken' ELSE lease_status END WHERE id=?", (result.master_id, pid))
                else:
                    self.db.execute("UPDATE proxies SET state='new',next_check_at=0,master_id=NULL WHERE id=?", (pid,))
                self.db.release_duplicates(pid)
            elif result.ok:
                state = original["state"] if original["state"] in ("active", "leased") else "valid"
                interval = self.settings.recheck_active_minutes if state in ("active", "leased") else self.settings.recheck_valid_minutes
                self.db.execute("UPDATE proxies SET state=?,reason=NULL,fails=0,master_id=NULL,next_check_at=? WHERE id=?", (state, now + interval * 60, pid))
            elif result.reason and result.reason.startswith("unsupported:"):
                await self.pool._close(row)
                self.db.execute("UPDATE proxies SET state='unsupported',reason=?,lease_expires_at=NULL,lease_status='broken' WHERE id=?", (result.reason, pid))
                self.db.release_duplicates(pid)
            else:
                await self.pool._fail(row, result.reason or "connect_fail")
            self.db.history(pid, result.asdict())
            self.db.event(original["state"] + " → " + self.pool.get(pid)["state"], pid)
            self.completed.append(now)

    async def reserve_exit(self, pid, result):
        if not self.settings.dedupe_by_exit_ip or not result.exit_ip or not result.ok:
            return
        async with self.pool.lock:
            master = self.db.one("SELECT id FROM proxies WHERE exit_ip=? AND id<>? AND state IN ('valid','active','leased','cooldown') ORDER BY id LIMIT 1", (result.exit_ip, pid))
            existing = master["id"] if master else self.reservations.get(result.exit_ip)
            if existing and existing != pid:
                result.ok, result.master_id, result.reason = False, existing, "duplicate_exit_ip"
            else:
                self.reservations[result.exit_ip] = pid

    async def run_batch(self, rows):
        entries, results = [], {}
        pipeline = Pipeline(self.settings, self.geo, self.direct_ip)
        try:
            for row in rows:
                c = json.loads(row["config"])
                reason = ProxyConfig(**c).support_reason(self.core.protocols)
                if reason:
                    results[row["id"]] = Result(reason="unsupported:" + reason)
                    continue
                entries.append((row["id"], c, self.ports.take()))
            if entries:
                try:
                    async with self.core.batch(entries):
                        cheap = await asyncio.gather(*(pipeline.cheap(port) for _, _, port in entries))
                        for entry, result in zip(entries, cheap):
                            await self.reserve_exit(entry[0], result)
                        final = await asyncio.gather(*(pipeline.speed(e[2], r) for e, r in zip(entries, cheap)))
                        # Only a whole-batch outage plus a failed direct probe is a test failure.
                        for stage in ("ping", "ip", "speed"):
                            applicable = [r for r in final if r.stage == stage]
                            progressed = any(r.ok or (stage == "ping" and r.stage != "ping") or (stage == "ip" and r.exit_ip) or (stage == "speed" and r.speed_mbps is not None) for r in final)
                            if applicable and not progressed and not await pipeline.test_direct(stage):
                                for r in applicable:
                                    r.test_error, r.reason = True, "test_url_unavailable:" + stage
                        results.update({e[0]: r for e, r in zip(entries, final)})
                except CoreError:
                    # Identify invalid generated configs without blaming all peers in a batch.
                    for pid, c, port in entries:
                        valid = await self.core.validate(build_config([(pid, c, port)])) if hasattr(self.core, "validate") else True
                        results[pid] = Result(reason="core_batch_unavailable" if valid else "unsupported:core_config_rejected", test_error=valid)
            for row in rows:
                await self.finish(row, results.get(row["id"], Result(reason="check_interrupted", test_error=True)))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.db.event("Ошибка проверки: " + type(exc).__name__, level="ERROR")
            for row in rows:
                await self.finish(row, Result(reason="check_internal_error", test_error=True))
        finally:
            for pid, _, port in entries:
                self.ports.release(port)
            ids = {r["id"] for r in rows}
            for ip, pid in list(self.reservations.items()):
                if pid in ids:
                    self.reservations.pop(ip, None)
            self.running -= len(rows)

    async def run(self):
        try:
            while True:
                if self.core.ready:
                    available = self.settings.check_concurrency - self.running
                    while available > 0:
                        rows = await self.claim(min(available, self.settings.check_batch_size))
                        if not rows:
                            break
                        self.running += len(rows)
                        task = asyncio.create_task(self.run_batch(rows))
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
                        available -= len(rows)
                await asyncio.sleep(0.25)
        finally:
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
