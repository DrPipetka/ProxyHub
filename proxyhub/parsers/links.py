import base64
import json
from urllib.parse import parse_qs, unquote, urlsplit

from .normalize import ProxyConfig


def b64decode(value):
    value = value.strip()
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8-sig")


def parse_link(line: str) -> ProxyConfig:
    if line.startswith("vmess://"):
        v = json.loads(b64decode(line[8:]))
        o = {"network": v.get("net", "tcp"), "security": v.get("tls") or "none", "sni": v.get("sni"),
             "host": v.get("host"), "path": v.get("path"), "alter_id": int(v.get("aid", 0)),
             "cipher": v.get("scy", "auto"), "fingerprint": v.get("fp")}
        if o["network"] == "grpc":
            o["service_name"] = v.get("path", "")
            o.pop("path", None)
        return ProxyConfig("vmess", v["add"], v["port"], v["id"], v.get("ps", ""), o)
    if line.startswith("ss://") and "@" not in line.split("#")[0]:
        body, _, remark = line[5:].partition("#")
        line = "ss://" + b64decode(body) + ("#" + remark if remark else "")
    u = urlsplit(line)
    if not u.scheme or not u.hostname:
        raise ValueError("Ожидается ссылка прокси")
    q = {k: v[-1] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
    p = {"hy2": "hysteria2"}.get(u.scheme, u.scheme)
    credential = unquote(u.username or "")
    if u.password is not None:
        credential += ":" + unquote(u.password)
    o = {"network": q.get("type", "tcp"), "security": q.get("security", "tls" if p in ("trojan", "hysteria2") else "none"),
         "sni": q.get("sni") or q.get("peer"), "host": q.get("host"), "path": q.get("path"),
         "fingerprint": q.get("fp"), "public_key": q.get("pbk"), "short_id": q.get("sid"),
         "spider_x": q.get("spx"), "flow": q.get("flow"), "service_name": q.get("serviceName"),
         "mode": q.get("mode"), "encryption": q.get("encryption")}
    if q.get("alpn"):
        o["alpn"] = q["alpn"].split(",")
    if q.get("allowInsecure", q.get("insecure", "0")).lower() in ("1", "true"):
        o["insecure"] = True
    if p == "ss":
        if ":" not in credential:
            credential = b64decode(credential)
        method, credential = credential.split(":", 1)
        o["method"] = method
        if q.get("plugin"):
            o["unsupported"] = "ss_plugin_not_supported"
    if q.get("obfs"):
        o["unsupported"] = "hysteria_obfs_not_supported"
    if q.get("headerType", "none") != "none":
        o["unsupported"] = "tcp_header_not_supported"
    known = {"type", "security", "sni", "peer", "host", "path", "fp", "pbk", "sid", "spx", "flow", "serviceName", "mode", "encryption", "alpn", "allowInsecure", "insecure", "plugin", "obfs", "obfs-password", "headerType"}
    if set(q) - known:
        o["unsupported"] = "unknown_link_parameters:" + ",".join(sorted(set(q) - known))
    return ProxyConfig(p, u.hostname, u.port or (443 if p in ("trojan", "hysteria2", "tuic") else 0), credential, unquote(u.fragment), o)
