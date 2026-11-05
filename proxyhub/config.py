from __future__ import annotations

import hashlib
import ipaddress
import os
import secrets
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)
    measure_geo: bool = True
    measure_ping: bool = True
    measure_speed: bool = False
    allowed_geo: list[str] = Field(default_factory=list)
    geo_blacklist: bool = False
    max_ping: float = Field(1500, ge=0)
    min_speed: float = Field(1, ge=0)
    require_no_leak: bool = False
    check_udp: bool = False
    require_udp: bool = False
    dedupe_by_exit_ip: bool = True
    pool_size: int = Field(10, ge=0, le=50)
    select_mode: Literal["best", "random_top", "geo"] = "best"
    top_x: int = Field(10, ge=1, le=10000)
    pool_geo: list[str] = Field(default_factory=list)
    best_sort: Literal["balanced", "speed", "ping"] = "balanced"
    on_demand: bool = True
    permanent_proxy: bool = False
    permanent_port: int = Field(1080, ge=1024, le=65535)
    permanent_check_seconds: int = Field(30, ge=5, le=3600)
    lease_ttl: int = Field(600, ge=1, le=604800)
    cooldown_minutes: float = Field(30, ge=0)
    recheck_after_cooldown: bool = True
    fail_retry_minutes: float = Field(60, ge=0.01)
    blacklist_after_fails: int = Field(5, ge=0)
    recheck_active_minutes: float = Field(5, ge=0.01)
    recheck_valid_minutes: float = Field(360, ge=0.01)
    check_concurrency: int = Field(50, ge=1, le=500)
    check_batch_size: int = Field(25, ge=1, le=100)
    connect_timeout: float = Field(8, gt=0, le=120)
    speed_timeout: float = Field(15, gt=0, le=300)
    speed_max_mb: float = Field(5, gt=0, le=1000)
    ping_samples: int = Field(3, ge=1, le=20)
    ping_urls: list[str] = Field(default_factory=lambda: ["http://cp.cloudflare.com/generate_204", "https://www.gstatic.com/generate_204"])
    speed_urls: list[str] = Field(default_factory=lambda: ["https://speed.cloudflare.com/__down?bytes=5000000", "http://speedtest.tele2.net/10MB.zip"])
    ip_echo_urls: list[str] = Field(default_factory=lambda: ["https://api.ipify.org", "https://ifconfig.me/ip"])
    pool_port_range: tuple[int, int] = (10800, 10899)
    check_port_range: tuple[int, int] = (20000, 20999)
    bind_address: str = "127.0.0.1"
    advertise_host: str = "127.0.0.1"
    socks_auth: bool = False
    socks_username: str = "proxyhub"
    socks_password: str = ""
    web_host: str = "127.0.0.1"
    web_port: int = Field(8080, ge=1, le=65535)
    ui_password_hash: str = ""
    api_token: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    xray_path: str = ""
    mmdb_path: str = ""
    auto_download: bool = True
    pool_restart_fallback: bool = False
    max_body_mb: int = Field(50, ge=1, le=500)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def validate_settings(self):
        a, b = self.pool_port_range, self.check_port_range
        for lo, hi in (a, b):
            if not 1024 <= lo <= hi <= 65535:
                raise ValueError("Диапазон портов: 1024 ≤ начало ≤ конец ≤ 65535")
            if lo <= self.web_port <= hi:
                raise ValueError("Порт UI попадает в диапазон SOCKS")
        if max(a[0], b[0]) <= min(a[1], b[1]):
            raise ValueError("Диапазоны портов пересекаются")
        if self.permanent_proxy and (self.permanent_port == self.web_port or any(lo <= self.permanent_port <= hi for lo, hi in (a, b))):
            raise ValueError("Постоянный порт должен отличаться от порта UI и диапазонов пула/проверок")
        if self.pool_size > a[1] - a[0] + 1:
            raise ValueError("Пул больше диапазона портов")
        if self.check_concurrency > b[1] - b[0] + 1:
            raise ValueError("Параллелизм больше диапазона проверок")
        for address in (self.bind_address, self.web_host):
            ipaddress.ip_address(address)
        if not ipaddress.ip_address(self.web_host).is_loopback and not self.ui_password_hash:
            raise ValueError("Для внешнего web_host необходим пароль UI")
        if self.socks_auth and (not self.socks_username or not self.socks_password):
            raise ValueError("Укажите логин и пароль SOCKS")
        if len(self.socks_username.encode()) > 255 or len(self.socks_password.encode()) > 255:
            raise ValueError("Логин и пароль SOCKS: не более 255 байт каждый")
        if self.require_udp and not self.check_udp:
            raise ValueError("require_udp требует check_udp")
        for countries in (self.allowed_geo, self.pool_geo):
            if any(len(c) != 2 or not c.isalpha() or c != c.upper() for c in countries):
                raise ValueError("Страны задаются двухбуквенными кодами ISO-2 в верхнем регистре")
        for urls in (self.ping_urls, self.speed_urls, self.ip_echo_urls):
            if not urls or any(not u.startswith(("https://", "http://")) for u in urls):
                raise ValueError("Нужен непустой список HTTP(S) URL")
        if len(self.api_token) < 16:
            raise ValueError("API-токен должен содержать не менее 16 символов")
        return self


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return f"scrypt${salt}${digest}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt, _ = encoded.split("$")
        return secrets.compare_digest(hash_password(password, salt), encoded)
    except (ValueError, TypeError):
        return False


def data_dir() -> Path:
    return Path(os.environ.get("PROXYHUB_DATA_DIR", "data")).resolve()


RESTART_FIELDS = {"web_host", "web_port", "xray_path", "mmdb_path", "auto_download", "bind_address", "pool_port_range", "check_port_range", "socks_auth", "socks_username", "socks_password"}
