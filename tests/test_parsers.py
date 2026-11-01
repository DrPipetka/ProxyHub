import base64
import json

import pytest

from proxyhub.parsers import import_configs
from proxyhub.parsers.links import parse_link
from proxyhub.parsers.clash import parse_clash
from proxyhub.parsers.singbox import parse_singbox
from conftest import UUID


def b64(v):
    return base64.urlsafe_b64encode(v.encode()).decode().rstrip("=")


@pytest.mark.parametrize("link,protocol", [
    (f"vless://{UUID}@example.com:443?security=reality&pbk=abc&sid=00&type=ws&path=%2Fx", "vless"),
    ("vmess://" + b64(json.dumps({"add": "example.com", "port": "443", "id": UUID, "net": "ws", "tls": "tls"})), "vmess"),
    ("ss://" + b64("aes-128-gcm:pass") + "@example.com:1234#name", "ss"),
    ("ss://" + b64("aes-128-gcm:pass@example.com:1234"), "ss"),
    ("trojan://p%40ss@example.com:443?sni=example.com", "trojan"),
    ("hysteria2://pass@example.com:443?sni=example.com", "hysteria2"),
    ("hy2://pass@example.com", "hysteria2"),
    ("tuic://uuid:pass@example.com:443", "tuic"),
    (f"vless://{UUID}@[::1]:443", "vless"),
])
def test_protocols(link, protocol):
    c = parse_link(link)
    assert c.protocol == protocol
    assert c.port > 0 and c.credential


def test_hash_ignores_remark_and_parameter_order():
    a = parse_link(f"vless://{UUID}@EXAMPLE.com:443?security=tls&type=tcp#one")
    b = parse_link(f"vless://{UUID}@example.com:443?type=raw&security=tls#two")
    assert a.hash == b.hash
    assert a.hash != parse_link(f"vless://{UUID}@example.com:444?security=tls").hash


def test_import_errors_and_duplicates(db):
    result = import_configs(db, f"vless://{UUID}@example.com:443#one\nvless://{UUID}@example.com:443#two\nbroken\ntuic://id:pass@example.com:443\nvless://bad@example.com:443")
    assert result["received"] == 5
    assert result["accepted"] == 1
    assert result["duplicates"] == 1
    assert result["unsupported"] == 1
    assert result["parse_errors"] == 2
    assert len(db.rows("SELECT * FROM proxies")) == 4


def test_base64_subscription(db):
    result = import_configs(db, b64(f"trojan://pass@example.com:443\nhy2://pass@example.com:443"))
    assert result["accepted"] == 1 and result["unsupported"] == 1


def test_clash_and_singbox(db):
    clash = f"proxies:\n  - name: sample\n    type: vless\n    server: example.com\n    port: 443\n    uuid: {UUID}\n    tls: true\n"
    sing = {"outbounds": [{"type": "shadowsocks", "tag": "hello", "server": "ss.example", "server_port": 1234, "method": "aes-128-gcm", "password": "pass"}]}
    assert import_configs(db, clash)["accepted"] == 1
    assert import_configs(db, json.dumps(sing))["accepted"] == 1
    assert import_configs(db, '{"outbounds":[{"type":"trojan"}]}')["parse_errors"] == 1


def test_formats_normalize_same_hash():
    a = parse_link(f"vless://{UUID}@example.com:443?security=tls")
    b = parse_clash({"type": "vless", "server": "example.com", "port": 443, "uuid": UUID, "tls": True})
    c = parse_singbox({"type": "vless", "server": "example.com", "server_port": 443, "uuid": UUID, "tls": {"enabled": True}})
    assert a.hash == b.hash == c.hash


def test_unknown_parameters_are_not_silently_dropped(db):
    r = import_configs(db, f"vless://{UUID}@example.com:443?type=magic\nss://{b64('aes-128-gcm:p')}@example.com:443?plugin=foo")
    assert r["unsupported"] == 2


def test_error_list_bounded_and_raw_preserved(db):
    result = import_configs(db, "\n".join("bad" for _ in range(120)))
    assert result["parse_errors"] == 120
    assert len(result["errors"]) == 100
    assert db.one("SELECT raw FROM proxies LIMIT 1")["raw"] == "bad"


@pytest.mark.parametrize("data", [
    '\ufeff\n<!DOCTYPE html>\n<html>\n<body>vless://fake</body>\n</html>',
    '<!-- GitHub -->\n<HTML lang="en">\n<head></head>\n</HTML>',
    '<html><script>const example = "proxies:";</script></html>',
    b64('<html><body>Access denied</body></html>'),
])
def test_html_is_rejected_without_storing_page_lines(db, data):
    result = import_configs(db, data)
    assert result["received"] == result["parse_errors"] == 1
    assert result["errors"][0]["reason"].startswith("html_document:")
    assert db.one("SELECT COUNT(*) n FROM proxies")["n"] == 0
