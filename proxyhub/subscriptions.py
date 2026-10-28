"""Persistent subscription schedules shared by manual and background imports."""
import asyncio
import json
import time
from urllib.parse import urlsplit

import httpx

from .core.installer import download_limited
from .parsers import import_configs
from .pool.lease import ServiceError


class Subscriptions:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.running = set()

    def get(self, sid):
        row = self.db.one("SELECT * FROM subscriptions WHERE id=?", (sid,))
        if not row:
            raise ServiceError("subscription_not_found", 404)
        return row

    def list(self):
        rows = self.db.rows("SELECT * FROM subscriptions ORDER BY id DESC")
        for row in rows:
            row["updating"] = row["id"] in self.running
            row["last_result"] = json.loads(row["last_result"]) if row["last_result"] else None
        return rows

    def save(self, url, source, interval):
        try:
            parts = urlsplit(url)
            valid = parts.scheme in ("http", "https") and parts.hostname and parts.port != 0
        except ValueError:
            valid = False
        if not valid:
            raise ServiceError("invalid_subscription_url")
        existing = self.db.one("SELECT id FROM subscriptions WHERE url=?", (url,))
        if existing and existing["id"] in self.running:
            raise ServiceError("subscription_updating", 409)
        now = time.time()
        self.db.execute("""INSERT INTO subscriptions(url,source,interval_minutes,created_at,next_update_at)
            VALUES(?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
            source=excluded.source,interval_minutes=excluded.interval_minutes,next_update_at=excluded.next_update_at""",
                        (url, source, interval, now, now + interval * 60 if interval else None))
        return self.db.one("SELECT id FROM subscriptions WHERE url=?", (url,))["id"]

    def update_interval(self, sid, interval):
        self.get(sid)
        self.db.execute("UPDATE subscriptions SET interval_minutes=?,next_update_at=? WHERE id=?",
                        (interval, time.time() + interval * 60 if interval else None, sid))
        return self.get(sid)

    async def import_many(self, urls, source, interval):
        if not isinstance(urls, list) or not 1 <= len(urls) <= 500 or any(not isinstance(url, str) for url in urls):
            raise ServiceError("invalid_subscription_urls", message="Укажите список от 1 до 500 URL подписок")
        cleaned = [url.strip() for url in urls if url.strip()]
        unique = list(dict.fromkeys(cleaned))
        if not unique:
            raise ServiceError("invalid_subscription_urls", message="Список URL подписок пуст")
        slots = asyncio.Semaphore(4)

        async def load(url):
            async with slots:
                item = {"url": url, "subscription_id": None}
                try:
                    sid = self.save(url, source, interval)
                    item["subscription_id"] = sid
                    return {**item, "ok": True, **await self.refresh(sid)}
                except ServiceError as exc:
                    return {**item, "ok": False, "error": exc.code}

        results = await asyncio.gather(*(load(url) for url in unique))
        successful = [item for item in results if item["ok"]]
        summary = {key: sum(item[key] for item in successful)
                   for key in ("received", "accepted", "duplicates", "unsupported", "parse_errors")}
        summary["errors"] = [{**error, "subscription_id": item["subscription_id"]}
                             for item in successful for error in item["errors"]][:100]
        return {**summary, "results": results, "subscriptions_total": len(unique),
                "subscriptions_succeeded": len(successful), "subscriptions_failed": len(results) - len(successful),
                "duplicate_urls": len(cleaned) - len(unique)}

    async def refresh(self, sid):
        if sid in self.running:
            raise ServiceError("subscription_updating", 409)
        row = self.get(sid)
        self.running.add(sid)
        started = time.monotonic()
        self.db.execute("UPDATE subscriptions SET last_attempt_at=? WHERE id=?", (time.time(), sid))
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False) as client:
                try:
                    data = (await download_limited(client, row["url"], self.service.settings.max_body_mb * 1024 * 1024)).decode("utf-8-sig")
                except (httpx.HTTPError, ValueError, UnicodeError):
                    raise ServiceError("subscription_download_failed", 400) from None
            async with self.service.import_lock:
                # Keep shutdown from closing SQLite while the import thread is still using it.
                task = asyncio.create_task(asyncio.to_thread(import_configs, self.db, data, row["source"], set(self.service.core.protocols)))
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
            if not result["received"] or result["parse_errors"] == result["received"]:
                raise ServiceError("subscription_invalid_content", 400)
            self.db.execute("UPDATE subscriptions SET last_success_at=?,last_error=NULL,last_result=? WHERE id=?",
                            (time.time(), json.dumps(result), sid))
            self.db.event(f"Подписка #{sid}: обновлена, принято {result['accepted']}, дублей {result['duplicates']}")
            return result
        except ServiceError as exc:
            self.db.execute("UPDATE subscriptions SET last_error=? WHERE id=?", (exc.code, sid))
            self.db.event(f"Подписка #{sid}: {exc.code}", level="WARNING")
            raise
        except Exception:
            self.db.execute("UPDATE subscriptions SET last_error='subscription_update_failed' WHERE id=?", (sid,))
            self.db.event(f"Подписка #{sid}: subscription_update_failed", level="ERROR")
            raise ServiceError("subscription_update_failed", 500) from None
        finally:
            interval = self.get(sid)["interval_minutes"]
            self.db.execute("UPDATE subscriptions SET duration_ms=?,next_update_at=? WHERE id=?",
                            (round((time.monotonic() - started) * 1000), time.time() + interval * 60 if interval else None, sid))
            self.running.discard(sid)

    async def tick(self):
        due = self.db.rows("SELECT id FROM subscriptions WHERE interval_minutes>0 AND next_update_at<=? ORDER BY next_update_at LIMIT 10", (time.time(),))
        for row in due:
            try:
                await self.refresh(row["id"])
            except ServiceError:
                pass  # refresh records failures and schedules the next attempt.

    async def run(self):
        while True:
            await self.tick()
            await asyncio.sleep(1)
