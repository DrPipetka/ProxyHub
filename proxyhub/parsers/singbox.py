from .normalize import ProxyConfig


def parse_singbox(v):
    p = v["type"]
    t, tls = v.get("transport", {}), v.get("tls", {})
    o = {"network": t.get("type", "tcp"), "security": "tls" if tls.get("enabled") else "none",
         "sni": tls.get("server_name"), "path": t.get("path"), "host": t.get("headers", {}).get("Host"),
         "service_name": t.get("service_name"), "flow": v.get("flow"), "method": v.get("method"), "alpn": tls.get("alpn")}
    if p == "vmess":
        o.update(cipher=v.get("security", "auto"), alter_id=v.get("alter_id", 0))
    if tls.get("insecure"):
        o["insecure"] = True
    if tls.get("utls", {}).get("enabled"):
        o["fingerprint"] = tls["utls"].get("fingerprint", "chrome")
    if tls.get("reality", {}).get("enabled"):
        r = tls["reality"]
        o.update(security="reality", public_key=r.get("public_key"), short_id=r.get("short_id"))
    known = {"type", "tag", "server", "server_port", "uuid", "password", "transport", "tls", "flow", "method", "security", "alter_id"}
    if set(v) - known:
        o["unsupported"] = "singbox_options_not_supported:" + ",".join(sorted(set(v) - known))
    return ProxyConfig(p, v["server"], v["server_port"], str(v.get("uuid") or v.get("password") or ""), v.get("tag", ""), o)
