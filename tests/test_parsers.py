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


@pytest.mark.parametrize('scheme', ['http', 'https', 'socks', 'socks4', 'socks4a', 'socks5', 'socks5h'])
def test_proxy_without_optional_auth_is_unsupported_not_malformed(db, scheme):
    result = import_configs(db, f'{scheme}://example.com:1080', persist_errors=False)
    assert result['received'] == result['unsupported'] == 1
    assert result['accepted'] == result['parse_errors'] == 0
    assert result['errors'][0]['reason'] == f'protocol_not_supported:{scheme}'
    assert db.one('SELECT state FROM proxies')['state'] == 'unsupported'


@pytest.mark.parametrize('link', [
    'vless://example.com:443', 'vmess://example.com:443',
    'trojan://example.com:443', 'ss://example.com:443',
    'hysteria2://example.com:443', 'tuic://example.com:443',
    'https://:443', 'socks5://example.com:0', 'https://example.com:65536',
])
def test_missing_required_credentials_or_invalid_endpoint_remains_parse_error(db, link):
    result = import_configs(db, link, persist_errors=False)
    assert result['received'] == result['parse_errors'] == 1
    assert result['accepted'] == result['unsupported'] == 0


def test_base64_subscription_with_optional_proxy_auth(db):
    data = b64('\n'.join([
        'https://null:null@first.example:9002?allowInsecure=1',
        'socks5://second.example:7000',
        'https://third.example:8081?allowInsecure=1',
        'anytls://secret@fourth.example:443/?sni=example.com&insecure=1&alpn=http/1.1&fp=chrome&udp=1',
        f'vless://{UUID}@fifth.example:443',
        'trojan://secret@sixth.example:443',
    ]))
    result = import_configs(db, data, persist_errors=False)
    assert result['received'] == 6 and result['accepted'] == 2
    assert result['unsupported'] == 4 and result['parse_errors'] == 0


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


@pytest.mark.parametrize("broken", [
    '{type: vmess, server: ::ffff:3981:3258, port: 16513, uuid: ' + UUID + '}',
    '{type: trojan, server: broken.example, port: 443, password: private-secret',
])
def test_clash_flow_subscription_recovers_valid_entries(db, broken):
    data = (
        'proxies:\n'
        '  - {type: trojan, server: first.example, port: 443, password: first}\n'
        f'  - {broken}\n'
        '  # A broken entry must not discard entries after it.\n'
        f'  - {{type: vless, server: last.example, port: 443, uuid: {UUID}}}\n'
    )
    result = import_configs(db, data, persist_errors=False)
    assert result['received'] == 3
    assert result['accepted'] == 2 and result['parse_errors'] == 1
    assert result['errors'][0]['line'] == 3
    assert 'private-secret' not in json.dumps(result)
    assert [r['host'] for r in db.rows('SELECT host FROM proxies ORDER BY id')] == ['first.example', 'last.example']
    repeated = import_configs(db, data, persist_errors=False)
    assert repeated['duplicates'] == 2 and repeated['parse_errors'] == 1


def test_broken_yaml_with_multiline_entries_is_not_split_arbitrarily(db):
    data = ('proxies:\n'
            '  - {type: trojan, server: first.example, port: 443, password: first}\n'
            '  - type: trojan\n'
            '    server: [broken\n')
    result = import_configs(db, data, persist_errors=False)
    assert result['received'] == result['parse_errors'] == 1
    assert result['accepted'] == 0
    assert db.one('SELECT COUNT(*) n FROM proxies')['n'] == 0


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
