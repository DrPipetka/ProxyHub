from __future__ import annotations

import asyncio
import json
import secrets
import time
from html import escape
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field, ValidationError, model_validator

from ..checker.pipeline import small_request
from ..config import RESTART_FIELDS, Settings, hash_password, verify_password
from ..core.installer import download_limited
from ..core.xray import port_available
from ..db import STATES
from ..parsers import import_configs
from ..pool.lease import ServiceError
from .auth import authorize

router = APIRouter(dependencies=[Depends(authorize)])
public = APIRouter()


class Login(BaseModel):
    password: str = Field("", max_length=1024)


class Action(BaseModel):
    id: int | None = Field(None, ge=1)
    addr: str | None = None
    reason: Literal["connect_fail", "timeout", "ping_high", "speed_low", "geo_mismatch", "leak", "udp_fail", "manual"] = "manual"

    @model_validator(mode="after")
    def identifier(self):
        if (self.id is None) == (self.addr is None):
            raise ValueError("Укажите id либо addr")
        return self


class Bulk(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=500)
    action: Literal["disable", "enable", "used", "invalid", "recheck", "release"]


@public.post("/auth/login")
async def login(body: Login, request: Request):
    service, state = request.app.state.service, request.app.state
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise ServiceError("origin_denied", 403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise ServiceError("origin_denied", 403)
    now = time.time()
    state.login_attempts[:] = [t for t in state.login_attempts if t > now - 60]
    if len(state.login_attempts) >= 20:
        raise ServiceError("too_many_attempts", 429)
    state.login_attempts.append(now)
    if service.settings.ui_password_hash and not await asyncio.to_thread(verify_password, body.password, service.settings.ui_password_hash):
        raise ServiceError("invalid_password", 401)
    state.sessions = {k: v for k, v in state.sessions.items() if v["expires"] > now}
    if len(state.sessions) >= 200:
        state.sessions.pop(next(iter(state.sessions)))
    token = secrets.token_urlsafe(32)
    state.sessions[token] = {"expires": now + 43200, "password_hash": service.settings.ui_password_hash}
    response = JSONResponse({"ok": True})
    response.set_cookie("proxyhub_session", token, httponly=True, samesite="strict", secure=request.url.scheme == "https", max_age=43200)
    return response


@public.get("/health")
async def health():
    return {"ok": True}


@router.post("/auth/logout")
async def logout(request: Request):
    request.app.state.sessions.pop(request.cookies.get("proxyhub_session"), None)
    response = JSONResponse({"ok": True})
    response.delete_cookie("proxyhub_session")
    return response


@router.post("/proxies")
async def add_proxies(request: Request):
    s = request.app.state.service
    content = (await request.body()).decode("utf-8-sig", errors="replace")
    source = ""
    if "application/json" in request.headers.get("content-type", ""):
        try:
            value = json.loads(content)
            if isinstance(value, dict) and ("data" in value or "url" in value):
                source = value.get("source", "")
                if not isinstance(source, str) or len(source) > 256:
                    raise ServiceError("invalid_source")
                if "url" in value and "data" in value:
                    raise ServiceError("data_or_url_required")
                if "url" in value:
                    url = value["url"]
                    if not isinstance(url, str) or urlsplit(url).scheme not in ("http", "https"):
                        raise ServiceError("invalid_subscription_url")
                    async with httpx.AsyncClient(timeout=30, follow_redirects=True, trust_env=False) as client:
                        try:
                            content = (await download_limited(client, url, s.settings.max_body_mb * 1024 * 1024)).decode("utf-8-sig")
                        except (httpx.HTTPError, ValueError):
                            raise ServiceError("subscription_download_failed", 400) from None
                else:
                    content = value["data"]
                if not isinstance(content, str):
                    raise ServiceError("data_must_be_text")
            # A native sing-box JSON document is accepted as well.
        except json.JSONDecodeError:
            raise ServiceError("invalid_json") from None
    async with request.app.state.import_lock:
        return await asyncio.to_thread(import_configs, s.db, content, source, set(s.core.protocols))


async def acquire_response(request, is_config, geo, max_ping, min_speed, mode, top, ttl, wait, format, lease=True):
    s = request.app.state.service
    countries = [v.strip().upper() for v in geo.split(",") if v.strip()] if geo else []
    if any(len(v) != 2 or not v.isalpha() for v in countries):
        raise ServiceError("invalid_geo")
    filters = dict(geo=countries, max_ping=max_ping, min_speed=min_speed, mode=mode, top=top)
    deadline = time.monotonic() + wait
    while True:
        try:
            row = await s.pool.acquire(is_config, lease, filters, ttl)
            break
        except ServiceError as exc:
            if exc.code != "no_proxy_available" or time.monotonic() >= deadline:
                raise
            if await request.is_disconnected():
                raise ServiceError("client_disconnected", 400)
            await asyncio.sleep(min(0.25, max(0, deadline - time.monotonic())))
    response = {k: row[k] for k in ("id", "geo", "ping_ms", "speed_mbps", "lease_expires_at")}
    if is_config:
        response.update(link=row["raw"], protocol=row["protocol"])
        text = row["raw"]
    else:
        settings = s.pool.settings
        host = settings.advertise_host
        addr = (f"[{host}]" if ":" in host else host) + ":" + str(row["port"])
        response.update(addr=addr, host=host, port=row["port"], exit_ip=row["exit_ip"], udp=bool(row["udp"]) if row["udp"] is not None else None,
                        username=settings.socks_username if settings.socks_auth else None, password=settings.socks_password if settings.socks_auth else None)
        text = addr
    return PlainTextResponse(text) if format == "text" else response


@router.get("/proxy")
async def proxy(request: Request, geo: str = "", max_ping: float | None = Query(None, ge=0), min_speed: float | None = Query(None, ge=0),
                mode: Literal["best", "random", "random_top"] | None = None, top: int | None = Query(None, ge=1, le=10000),
                lease_ttl: int | None = Query(None, ge=1, le=604800), wait: float = Query(0, ge=0, le=120), format: Literal["json", "text"] = "json"):
    return await acquire_response(request, False, geo, max_ping, min_speed, mode, top, lease_ttl, wait, format)


@router.get("/config")
async def config(request: Request, geo: str = "", max_ping: float | None = Query(None, ge=0), min_speed: float | None = Query(None, ge=0),
                 mode: Literal["best", "random", "random_top"] | None = None, top: int | None = Query(None, ge=1, le=10000),
                 lease_ttl: int | None = Query(None, ge=1, le=604800), wait: float = Query(0, ge=0, le=120), format: Literal["json", "text"] = "json", lease: int = Query(1, ge=0, le=1)):
    return await acquire_response(request, True, geo, max_ping, min_speed, mode, top, lease_ttl, wait, format, bool(lease))


@router.post("/proxy/{action}")
async def proxy_action(action: Literal["used", "invalid", "release"], body: Action, request: Request):
    row = await request.app.state.service.pool.action(action, body.id, body.addr, body.reason)
    return {"id": row["id"], "state": row["state"]}


@router.post("/actions")
async def bulk_action(body: Bulk, request: Request):
    result = []
    for pid in dict.fromkeys(body.ids):
        try:
            row = await request.app.state.service.pool.action(body.action, pid)
            result.append({"id": pid, "state": row["state"]})
        except ServiceError as exc:
            result.append({"id": pid, "error": exc.code})
    return {"results": result}


@router.delete("/proxies")
async def delete(request: Request, confirm: str = "", scope: Literal["all", "invalid", "source"] = "all", source: str | None = None):
    if confirm != "yes":
        raise ServiceError("confirmation_required")
    async with request.app.state.import_lock:
        return {"deleted": await request.app.state.service.pool.delete(scope, source)}


@router.get("/status")
async def status(request: Request):
    return request.app.state.service.status()


@router.get("/count")
async def count(request: Request, state: Literal["active", "leased"] | None = None, format: Literal["json", "text"] = "json"):
    query = "SELECT COUNT(*) n FROM proxies WHERE port IS NOT NULL AND state "
    row = request.app.state.service.db.one(query + ("=?" if state else "IN ('active','leased')"), (state,) if state else ())
    return PlainTextResponse(str(row["n"])) if format == "text" else {"count": row["n"]}


@router.get("/proxies")
async def list_proxies(request: Request, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200), state: str = "", reason: str = "",
                       geo: str = "", protocol: str = "", source: str = "", q: str = Query("", max_length=256), raised: bool = False,
                       min_ping: float | None = None, max_ping: float | None = None, min_speed: float | None = None, max_speed: float | None = None,
                       sort: Literal["id", "ping_ms", "speed_mbps", "state", "geo", "last_check_at", "source"] = "id", direction: Literal["asc", "desc"] = "desc"):
    s = request.app.state.service
    where, args = ["1=1"], []
    for key, value in (("state", state), ("reason", reason), ("geo", geo), ("protocol", protocol), ("source", source)):
        if value:
            where.append(key + "=?")
            args.append(value)
    if raised:
        where.append("port IS NOT NULL")
    for col, op, value in (("ping_ms", ">=", min_ping), ("ping_ms", "<=", max_ping), ("speed_mbps", ">=", min_speed), ("speed_mbps", "<=", max_speed)):
        if value is not None:
            where.append(col + op + "?")
            args.append(value)
    if q:
        where.append("(host LIKE ? ESCAPE '\\' OR remark LIKE ? ESCAPE '\\' OR source LIKE ? ESCAPE '\\' OR CAST(id AS TEXT)=?)")
        pattern = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        args.extend((pattern, pattern, pattern, q))
    clause = " AND ".join(where)
    # Do not select raw/config here: secrets stay server-side until explicit copy.
    fields = "id,protocol,host,remark,source,state,reason,master_id,geo,exit_ip,ping_ms,speed_mbps,udp,fails,port,lease_expires_at,lease_status,cooldown_until,last_check_at,next_check_at,checking"
    total = s.db.one("SELECT COUNT(*) n FROM proxies WHERE " + clause, args)["n"]
    rows = s.db.rows(f"SELECT {fields} FROM proxies WHERE {clause} ORDER BY {sort} {direction},id {direction} LIMIT ? OFFSET ?", (*args, page_size, (page - 1) * page_size))
    for row in rows:
        host = s.pool.settings.advertise_host
        row["addr"] = (f"[{host}]" if ":" in host else host) + ":" + str(row["port"]) if row["port"] else None
        row["masked_link"] = f"{row['protocol'] or '?'}://••••@{row['host'] or '—'}"
    return {"items": rows, "total": total, "page": page, "page_size": page_size}


@router.get("/proxies/{proxy_id}/link")
async def full_link(proxy_id: int, request: Request):
    return {"link": request.app.state.service.pool.get(proxy_id)["raw"]}


@router.get("/proxies/{proxy_id}/history")
async def history(proxy_id: int, request: Request):
    return request.app.state.service.db.rows("SELECT * FROM history WHERE proxy_id=? ORDER BY id DESC LIMIT 20", (proxy_id,))


@router.get("/settings")
async def settings(request: Request):
    s = request.app.state.service.settings
    value = s.model_dump()
    value.pop("ui_password_hash")
    value["ui_password_set"] = bool(s.ui_password_hash)
    return {"values": value, "restart_fields": sorted(RESTART_FIELDS), "schema": Settings.model_json_schema()["properties"]}


@router.patch("/settings")
async def save_settings(request: Request):
    service = request.app.state.service
    try:
        changes = await request.json()
        if not isinstance(changes, dict) or "ui_password_hash" in changes:
            raise ServiceError("invalid_settings")
        if "ui_password" in changes:
            password = changes.pop("ui_password")
            if not isinstance(password, str) or (password and len(password) < 8) or len(password) > 1024:
                raise ServiceError("password_too_short", 400, "Пароль: от 8 до 1024 символов")
            changes["ui_password_hash"] = await asyncio.to_thread(hash_password, password) if password else ""
        new = Settings.model_validate({**service.settings.model_dump(), **changes})
    except (ValueError, TypeError):
        raise ServiceError("invalid_settings", 400, "Некорректные настройки: проверьте диапазоны, зависимости галочек и пароль для внешнего адреса") from None
    restart = [k for k in RESTART_FIELDS if getattr(new, k) != getattr(service.runtime_settings, k)]
    for key in ("pool_port_range", "check_port_range"):
        if key in changes and getattr(new, key) != getattr(service.settings, key):
            used = service.pool.ports.used | service.scheduler.ports.used
            host = new.bind_address if key == "pool_port_range" else "127.0.0.1"
            lo, hi = getattr(new, key)
            if any(p not in used and not port_available(p, host) for p in range(lo, hi + 1)):
                raise ServiceError("ports_in_use", 409)
    async with service.pool.lock:
        # Restart-only settings are persisted but retain their running values until restart.
        runtime_values = new.model_dump()
        runtime_values.update({k: getattr(service.runtime_settings, k) for k in RESTART_FIELDS})
        try:
            live = Settings.model_validate(runtime_values)
        except ValidationError:
            raise ServiceError("restart_settings_conflict", 409, "Сначала измените диапазоны и перезапустите приложение, затем размер пула/параллелизм") from None
        service.db.save_settings(new)
        service.settings, service.pool.settings = new, live
        service.core.settings = live
        service.restart_required = restart
        service.log.setLevel(new.log_level)
    return {"ok": True, "restart_required": restart}


@router.post("/settings/test-urls")
async def test_urls(request: Request):
    s = request.app.state.service.settings
    urls = list(dict.fromkeys(s.ping_urls + s.speed_urls + s.ip_echo_urls))
    async with httpx.AsyncClient(timeout=s.connect_timeout, follow_redirects=True, trust_env=False) as client:
        async def probe(url):
            try:
                await small_request(client, url)
                return {"url": url, "ok": True}
            except (httpx.HTTPError, ValueError):
                return {"url": url, "ok": False}
        return {"results": await asyncio.gather(*(probe(url) for url in urls))}


@router.get("/events")
async def events(request: Request, before: int | None = None, limit: int = Query(100, ge=1, le=500)):
    return request.app.state.service.db.rows("SELECT * FROM events WHERE id<? ORDER BY id DESC LIMIT ?", (before or 2**63 - 1, limit))


@router.get("/ui/events-fragment", response_class=HTMLResponse)
async def events_fragment(request: Request):
    rows = request.app.state.service.db.rows("SELECT * FROM events ORDER BY id DESC LIMIT 100")
    return "".join(
        '<div class="event"><time>' + datetime.fromtimestamp(r['time']).strftime('%d.%m.%Y %H:%M:%S')
        + '</time><span class="' + escape(r['level'], quote=True) + '">' + escape(r['level'])
        + '</span><span>' + ('#' + str(r['proxy_id']) if r['proxy_id'] else '—')
        + '</span><span>' + escape(r['message']) + '</span></div>' for r in rows
    ) or '<p class="muted">Событий пока нет</p>'
