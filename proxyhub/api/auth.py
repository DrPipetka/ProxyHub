import secrets
import time
from urllib.parse import urlsplit

from fastapi import Request

from ..pool.lease import ServiceError


async def authorize(request: Request):
    service = request.app.state.service
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and secrets.compare_digest(header[7:], service.settings.api_token):
        return
    cookie = request.cookies.get("proxyhub_session", "")
    session = request.app.state.sessions.get(cookie)
    if session and session["expires"] > time.time() and session["password_hash"] == service.settings.ui_password_hash:
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.headers.get("host"):
            raise ServiceError("origin_denied", 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise ServiceError("origin_denied", 403)
        return
    raise ServiceError("unauthorized", 401, "Требуется API-токен или вход в веб-интерфейс")
