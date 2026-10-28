from .normalize import ProxyConfig


def parse_clash(v):
    p = v["type"]
    o = {"network": v.get("network", "tcp"), "security": "tls" if v.get("tls", p in ("trojan", "hysteria2", "hy2")) else "none",
         "sni": v.get("servername") or v.get("sni"), "method": v.get("cipher"), "flow": v.get("flow"),
         "fingerprint": v.get("client-fingerprint"), "alpn": v.get("alpn")}
    if v.get("skip-cert-verify"):
        o["insecure"] = True
    if p == "vmess":
        o.pop("method", None)
        o.update(cipher=v.get("cipher", "auto"), alter_id=int(v.get("alterId", 0)))
    ws = v.get("ws-opts", {})
    o.update(path=ws.get("path"), host=ws.get("headers", {}).get("Host"))
    if v.get("grpc-opts"):
        o["service_name"] = v["grpc-opts"].get("grpc-service-name")
    if v.get("reality-opts"):
        r = v["reality-opts"]
        o.update(security="reality", public_key=r.get("public-key"), short_id=r.get("short-id"))
    if v.get("plugin") or v.get("obfs"):
        o["unsupported"] = "plugin_or_obfs_not_supported"
    known = {"name", "type", "server", "port", "uuid", "password", "network", "tls", "servername", "sni", "cipher", "flow", "client-fingerprint", "alpn", "skip-cert-verify", "alterId", "ws-opts", "grpc-opts", "reality-opts", "plugin", "plugin-opts", "obfs", "obfs-password", "udp", "tfo"}
    if set(v) - known:
        o["unsupported"] = "clash_options_not_supported:" + ",".join(sorted(set(v) - known))
    return ProxyConfig(p, v["server"], v["port"], str(v.get("uuid") or v.get("password") or ""), v.get("name", ""), o)
