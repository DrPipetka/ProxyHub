import asyncio
import logging
import time
from logging.handlers import RotatingFileHandler

from .checker.geo import GeoDatabase
from .checker.scheduler import Scheduler
from .core.xray import XrayAdapter
from .db import Database, STATES
from .pool.manager import PoolManager
from .instance import InstanceLock


class Service:
    def __init__(self, directory, core=None, background=True):
        self.directory = directory
        self.instance = InstanceLock(directory)
        self.db = Database(directory / "proxyhub.db")
        self.settings = self.db.settings()
        self.runtime_settings = self.settings.model_copy(deep=True)
        self.db.recover()
        self.started = time.time()
        self.core = core or XrayAdapter(self.settings, directory, self.db.event)
        self.pool = PoolManager(self.db, self.settings, self.core)
        self.geo = GeoDatabase()
        self.scheduler = Scheduler(self.db, self.pool, self.core, self.geo)
        self.background = background
        self.tasks = []
        self.restart_required = []
        self.log = logging.getLogger("proxyhub")
        self.handler = RotatingFileHandler(directory / "proxyhub.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.log.addHandler(self.handler)
        self.log.setLevel(self.settings.log_level)

    async def start(self):
        if self.background:
            self.tasks = [asyncio.create_task(self.initialize_loop()), asyncio.create_task(self.scheduler.run()), asyncio.create_task(self.maintenance())]

    async def initialize_loop(self):
        async def initialize_core():
            await self.core.initialize()
            self.db.event("Ядро xray готово")
            if "hysteria2" in self.core.protocols:
                self.db.execute("UPDATE proxies SET state='new',reason=NULL,next_check_at=0 WHERE protocol='hysteria2' AND state='unsupported' AND reason IN ('protocol_not_supported:hysteria2','unsupported:protocol_not_supported:hysteria2')")

        while True:
            resources = []
            if not self.core.ready and not self.scheduler.running:
                resources.append(("xray", initialize_core()))
            if not self.geo.reader and self.pool.settings.measure_geo:
                resources.append(("MMDB", self.geo.initialize(self.pool.settings, self.directory)))
            if time.time() - self.scheduler.direct_checked > 3600:
                resources.append(("direct_ip", self.scheduler.refresh_direct()))
            results = await asyncio.gather(*(task for _, task in resources), return_exceptions=True)
            for (name, _), result in zip(resources, results):
                if isinstance(result, Exception):
                    self.db.event("Инициализация " + name + ": " + type(result).__name__ + "; повтор через 30 секунд", level="ERROR")
                    self.log.error("Resource %s initialization failed: %s", name, type(result).__name__)
            await asyncio.sleep(30)

    async def maintenance(self):
        while True:
            try:
                process = getattr(self.core, "pool", None)
                if self.core.ready and process and process.returncode is not None:
                    async with self.pool.lock:
                        self.core.ready = False
                        self.core.entries.clear()
                        self.pool.ports.used.clear()
                        for row in self.db.rows("SELECT id FROM proxies WHERE state='leased' AND port IS NOT NULL"):
                            self.db.release_duplicates(row["id"])
                        self.db.execute("UPDATE proxies SET state='invalid',reason='connect_fail',lease_status='broken',lease_expires_at=NULL,port=NULL,checking=0,revision=revision+1,next_check_at=? WHERE state='leased' AND port IS NOT NULL", (time.time() + self.settings.fail_retry_minutes * 60,))
                        self.db.execute("UPDATE proxies SET state='valid',port=NULL,checking=0,revision=revision+1 WHERE state='active'")
                        self.db.event("Процесс пула завершился; аренды прерваны, запускается восстановление", level="ERROR")
                await self.pool.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error("Pool maintenance failed: %s", type(exc).__name__)
                self.db.event("Обслуживание пула: " + type(exc).__name__, level="ERROR")
            await asyncio.sleep(0.5)

    def status(self):
        counters = dict.fromkeys(STATES, 0)
        counters.update({r["state"]: r["n"] for r in self.db.rows("SELECT state,COUNT(*) n FROM proxies GROUP BY state")})
        count = self.db.one("SELECT COUNT(*) n FROM proxies WHERE port IS NOT NULL")["n"]
        queue = self.db.one("SELECT COUNT(*) n FROM proxies WHERE checking=0 AND (manual_check=1 OR (state IN ('new','invalid','valid','active','leased') AND next_check_at<=?))", (time.time(),))["n"]
        from . import __version__
        return {"version": __version__, "uptime": round(time.time() - self.started), "xray": {"ready": self.core.ready, "version": self.core.version, "protocols": sorted(self.core.protocols)},
                "states": counters, "raised": count, "free": counters["active"], "leased": counters["leased"], "queue": queue,
                "checking": self.scheduler.running, "checks_per_minute": sum(t > time.time() - 60 for t in self.scheduler.completed),
                "ports": {"pool_used": len(self.pool.ports.used), "check_used": len(self.scheduler.ports.used), "pool_capacity": self.pool.ports.span[1] - self.pool.ports.span[0] + 1},
                "pool": {k: getattr(self.pool.settings, k) for k in ("pool_size", "select_mode", "best_sort", "on_demand")},
                "direct_ip": self.scheduler.direct_ip, "geo_ready": bool(self.geo.reader), "restart_required": self.restart_required,
                "last_errors": self.db.rows("SELECT * FROM events WHERE level IN ('WARNING','ERROR') ORDER BY id DESC LIMIT 10")}

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.core.close()
        self.geo.close()
        self.db.recover()
        self.db.close()
        self.log.removeHandler(self.handler)
        self.handler.close()
        self.instance.close()
