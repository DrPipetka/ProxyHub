from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field


class Unsupported(ValueError):
    pass


@dataclass
class ProxyConfig:
    protocol: str
    host: str
    port: int
    credential: str
    remark: str = ""
    options: dict = field(default_factory=dict)

    def __post_init__(self):
        self.protocol = {"hy2": "hysteria2", "shadowsocks": "ss"}.get(self.protocol, self.protocol)
        self.host = self.host.strip().lower().rstrip(".")
        self.port = int(self.port)
        if not self.host or not 1 <= self.port <= 65535 or not self.credential:
            raise ValueError("Не указаны сервер, корректный порт или учётные данные")
        if self.protocol in ("vless", "vmess"):
            self.credential = str(uuid.UUID(self.credential))
        self.options = {k: v for k, v in self.options.items() if v is not None and v != ""}
        self.options.setdefault("network", "tcp")
        if self.options["network"] == "raw":
            self.options["network"] = "tcp"
        self.options.setdefault("security", "tls" if self.protocol in ("trojan", "hysteria2") else "none")
        if self.protocol == "hysteria2":
            self.options["network"] = "hysteria"

    def asdict(self):
        return asdict(self)

    @property
    def hash(self):
        value = self.asdict()
        value.pop("remark")
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def support_reason(self, protocols):
        if self.protocol not in protocols:
            return "protocol_not_supported:" + self.protocol
        if self.options.get("network") not in {"tcp", "ws", "grpc", "httpupgrade", "xhttp", "hysteria"}:
            return "transport_not_supported:" + str(self.options["network"])
        if self.options.get("security") not in {"none", "tls", "reality"}:
            return "security_not_supported"
        if self.options.get("unsupported"):
            return self.options["unsupported"]
        return None
