"""Persistent subscription schedules shared by manual and background imports."""
import asyncio
import json
import secrets
import time
from urllib.parse import urlsplit, urlunsplit

import httpx

from .core.installer import download_limited
from .parsers import import_configs, is_html
from .pool.lease import ServiceError


def subscription_download_url(url):
    """GitHub file viewer URLs must download the file, never its HTML viewer."""
    parts = urlsplit(url)
    if parts.hostname not in {"github.com", "www.github.com"}:
        return url
    path = parts.path.strip("/").split("/")
    if len(path) < 5 or path[2] not in {"blob", "raw"}:
        raise ServiceError("subscription_file_required", message="Укажите ссылку на файл подписки (Raw), а не на репозиторий или папку GitHub")
    return urlunsplit(("https", "raw.githubusercontent.com", "/" + "/".join(path[:2] + path[3:]), parts.query, ""))


class Subscriptions:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.running = set()
        self.generation = 0

    def delete_all(self):
        """Called under import_lock; invalidate queued imports and in-flight downloads."""
        with self.db.transaction() as c:
            c.execute("UPDATE proxies SET independent_import=1 WHERE id IN (SELECT proxy_id FROM subscription_proxies)")
            deleted = c.execute("DELETE FROM subscriptions").rowcount
        self.generation += 1
        return deleted

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

    def save(self, url, source, interval, **options):
        try:
            parts = urlsplit(url)
            valid = parts.scheme in ("http", "https") and parts.hostname and parts.port != 0
        except ValueError:
            valid = False
        if not valid:
            raise ServiceError("invalid_subscription_url")
        subscription_download_url(url)
        existing = self.db.one("SELECT id FROM subscriptions WHERE url=?", (url,))
        if existing and existing["id"] in self.running:
            raise ServiceError("subscription_updating", 409)
        now = time.time()
        self.db.execute("""INSERT INTO subscriptions(url,source,interval_minutes,created_at,next_update_at)
            VALUES(?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET
            source=excluded.source,interval_minutes=excluded.interval_minutes,next_update_at=excluded.next_update_at""",
                        (url, source, interval, now, now + interval * 60 if interval else None))
        sid = self.db.one("SELECT id FROM subscriptions WHERE url=?", (url,))["id"]
        if options:
            self.update_options(options, sid)
        return sid

    def update_options(self, options, sid=None):
        if sid is not None:
            self.get(sid)
        values = {k: v for k, v in options.items()
                  if k in ("interval_minutes", "delete_old_configs", "keep_working_configs")}
        if not values:
            return 0
        if "interval_minutes" in values:
            interval = values["interval_minutes"]
            values["next_update_at"] = time.time() + interval * 60 if interval else None
        clause = " WHERE id=?" if sid is not None else ""
        args = (*values.values(), sid) if sid is not None else tuple(values.values())
        return self.db.execute("UPDATE subscriptions SET " + ",".join(k + "=?" for k in values) + clause, args).rowcount

    def delete(self, sid):
        self.get(sid)
        if sid in self.running:
            raise ServiceError("subscription_updating", 409)
        with self.db.transaction() as c:
            c.execute("UPDATE proxies SET independent_import=1 WHERE id IN (SELECT proxy_id FROM subscription_proxies WHERE subscription_id=?)", (sid,))
            c.execute("DELETE FROM subscriptions WHERE id=?", (sid,))

    def update_interval(self, sid, interval):
        self.get(sid)
        self.db.execute("UPDATE subscriptions SET interval_minutes=?,next_update_at=? WHERE id=?",
                        (interval, time.time() + interval * 60 if interval else None, sid))
        return self.get(sid)

    async def import_many(self, urls, source, interval, **options):
        if not isinstance(urls, list) or not 1 <= len(urls) <= 500 or any(not isinstance(url, str) for url in urls):
            raise ServiceError("invalid_subscription_urls", message="Укажите список от 1 до 500 URL подписок")
        cleaned = [url.strip() for url in urls if url.strip()]
        unique = list(dict.fromkeys(cleaned))
        if not unique:
            raise ServiceError("invalid_subscription_urls", message="Список URL подписок пуст")
        slots = asyncio.Semaphore(4)
        generation = self.generation

        async def load(url):
            async with slots:
                item = {"url": url, "subscription_id": None}
                try:
                    if generation != self.generation:
                        raise ServiceError("subscriptions_deleted", 409, "Подписки удалены; импорт остановлен")
                    sid = self.save(url, source, interval, **options)
                    item["subscription_id"] = sid
                    return {**item, "ok": True, **await self.refresh(sid)}
                except ServiceError as exc:
                    return {**item, "ok": False, "error": exc.code}

        results = await asyncio.gather(*(load(url) for url in unique))
        successful = [item for item in results if item["ok"]]
        summary = {key: sum(item[key] for item in successful)
                   for key in ("received", "accepted", "duplicates", "unsupported", "parse_errors", "removed")}
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
                    data = (await download_limited(client, subscription_download_url(row["url"]), self.service.settings.max_body_mb * 1024 * 1024)).decode("utf-8-sig")
                except (httpx.HTTPError, ValueError, UnicodeError):
                    raise ServiceError("subscription_download_failed", 400) from None
            if is_html(data):
                raise ServiceError("subscription_html_content", message="Вместо конфигов получена HTML-страница. Укажите Raw-ссылку на файл подписки")
            async with self.service.import_lock:
                # A bulk deletion can finish while the subscription is downloading.
                # Never let that old response repopulate the cleared library.
                self.get(sid)
                token = secrets.token_hex(16)
                # Keep shutdown from closing SQLite while the import thread is still using it.
                task = asyncio.create_task(asyncio.to_thread(import_configs, self.db, data, row["source"], set(self.service.core.protocols), persist_errors=False,
                                                           subscription_id=sid, seen_token=token))
                try:
                    result = await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
                if not result["received"] or result["parse_errors"] == result["received"]:
                    raise ServiceError("subscription_invalid_content", 400)
                # A partial parse is not proof that missing configs were removed
                # upstream. Import the valid entries but defer destructive cleanup.
                result["cleanup_skipped"] = bool(result["parse_errors"])
                result["removed"] = 0
                current = self.get(sid)
                if current["delete_old_configs"] and not result["cleanup_skipped"]:
                    result["removed"] = await self.cleanup(current, token)
            self.db.execute("UPDATE subscriptions SET last_success_at=?,last_error=NULL,last_result=? WHERE id=?",
                            (time.time(), json.dumps(result), sid))
            self.db.event(f"Подписка #{sid}: обновлена, принято {result['accepted']}, дублей {result['duplicates']}, удалено {result['removed']}")
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
            current = self.db.one("SELECT interval_minutes FROM subscriptions WHERE id=?", (sid,))
            if current:
                interval = current["interval_minutes"]
                self.db.execute("UPDATE subscriptions SET duration_ms=?,next_update_at=? WHERE id=?",
                                (round((time.monotonic() - started) * 1000), time.time() + interval * 60 if interval else None, sid))
            self.running.discard(sid)

    async def cleanup(self, subscription, token):
        """Prune absent members under import_lock and the pool's state lock."""
        sid = subscription["id"]
        async with self.service.pool.lock:
            keep = ""
            delete_args = (sid, token, sid)
            if subscription["keep_working_configs"]:
                # In-progress checks may temporarily hide a previously valid state.
                keep = " AND p.checking=0 AND p.state NOT IN ('valid','active','leased','cooldown','duplicate') AND p.id IS NOT ?"
                delete_args += (self.service.pool.permanent.proxy_id,)
            where = """p.independent_import=0 AND p.id IN (
                SELECT proxy_id FROM subscription_proxies WHERE subscription_id=? AND seen_token<>?)
                AND NOT EXISTS (SELECT 1 FROM subscription_proxies other
                    WHERE other.proxy_id=p.id AND other.subscription_id<>?)"""
            for row in self.db.rows("SELECT p.* FROM proxies p WHERE " + where + keep + " AND (p.port IS NOT NULL OR p.id=?)",
                                    (*delete_args, self.service.pool.permanent.proxy_id)):
                await self.service.pool._close(row)
            with self.db.transaction() as c:
                removed = c.execute("DELETE FROM proxies AS p WHERE " + where + keep, delete_args).rowcount
                # Keep protected working members associated so a later refresh can
                # remove them if they fail. Detach stale shared/independent members.
                c.execute("""DELETE FROM subscription_proxies AS stale WHERE subscription_id=? AND seen_token<>?
                    AND EXISTS (SELECT 1 FROM proxies p WHERE p.id=stale.proxy_id AND (p.independent_import=1
                    OR EXISTS (SELECT 1 FROM subscription_proxies other WHERE other.proxy_id=p.id AND other.subscription_id<>?))""" + keep + ")",
                          delete_args)
                c.execute("UPDATE proxies SET state='new',master_id=NULL,next_check_at=0,revision=revision+1 WHERE state='duplicate' AND NOT EXISTS (SELECT 1 FROM proxies master WHERE master.id=proxies.master_id)")
            return removed

    async def tick(self):
        due = self.db.rows("SELECT id FROM subscriptions WHERE interval_minutes>0 AND next_update_at<=? ORDER BY next_update_at LIMIT 10", (time.time(),))
        slots = asyncio.Semaphore(4)
        async def refresh_due(row):
            async with slots:
                try:
                    await self.refresh(row["id"])
                except ServiceError:
                    pass  # refresh records failures and schedules the next attempt.
        await asyncio.gather(*(refresh_due(row) for row in due))

    async def run(self):
        while True:
            await self.tick()
            await asyncio.sleep(1)
