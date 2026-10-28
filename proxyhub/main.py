from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from .api.routes import public, router
from .config import data_dir
from .core.adapter import CoreError, NoPorts
from .pool.lease import ServiceError
from .service import Service


def create_app(directory=None, core=None, background=True):
    @asynccontextmanager
    async def lifespan(app):
        app.state.service = Service(Path(directory) if directory else data_dir(), core=core, background=background)
        app.state.sessions = {}
        app.state.login_attempts = []
        app.state.import_lock = app.state.service.import_lock
        await app.state.service.start()
        try:
            yield
        finally:
            await app.state.service.close()

    app = FastAPI(title="ProxyHub", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def guards(request, call_next):
        s = request.app.state.service
        # Prevent DNS rebinding against passwordless localhost installations.
        if not s.settings.ui_password_hash and request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return JSONResponse({"error": "host_denied", "message": "Недопустимый Host"}, status_code=403)
        if request.method in ("POST", "PUT", "PATCH"):
            limit = s.settings.max_body_mb * 1024 * 1024
            try:
                if int(request.headers.get("content-length", "0")) > limit:
                    return JSONResponse({"error": "body_too_large", "message": "Превышен лимит тела запроса"}, status_code=413)
            except ValueError:
                return JSONResponse({"error": "invalid_content_length", "message": "Некорректный размер"}, status_code=400)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > limit:
                    return JSONResponse({"error": "body_too_large", "message": "Превышен лимит тела запроса"}, status_code=413)
            request._body = bytes(body)
        try:
            response = await call_next(request)
        except Exception as exc:
            # Suppress raw exception text: third-party errors may contain URLs/keys.
            s.log.error("Unhandled request error: %s", type(exc).__name__)
            response = JSONResponse({"error": "internal_error", "message": "Внутренняя ошибка; см. журнал приложения"}, status_code=500)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(NoPorts)
    async def ports_error(request, exc):
        return JSONResponse({"error": "no_free_ports", "message": "Нет свободных портов"}, status_code=503)

    @app.exception_handler(CoreError)
    async def core_error(request, exc):
        return JSONResponse({"error": "core_unavailable", "message": "Ядро xray недоступно; см. журнал"}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"error": "validation_error", "message": "Некорректные параметры запроса", "fields": [".".join(map(str, e["loc"])) for e in exc.errors()]}, status_code=400)

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        return JSONResponse({"error": "http_error", "message": str(exc.detail)}, status_code=exc.status_code)

    app.include_router(public)
    app.include_router(router)
    web = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=web / "static"), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(web / "index.html")

    return app
