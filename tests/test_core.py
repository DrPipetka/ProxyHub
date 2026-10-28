import hashlib

import pytest

from proxyhub.config import Settings
from proxyhub.core.installer import asset_name, verify_digest
from proxyhub.core.xray import PortAllocator, build_config
from proxyhub.parsers.links import parse_link
from conftest import UUID


def test_batch_routing_no_direct_fallback():
    c = parse_link(f"vless://{UUID}@example.com:443?security=tls&type=ws&path=%2Fproxy&host=front.example").asdict()
    result = build_config([(1, c, 20000), (2, c, 20001)], api_port=30000)
    assert result["outbounds"][0]["protocol"] == "blackhole"
    assert len(result["inbounds"]) == 3
    assert result["routing"]["rules"][-1] == {"type": "field", "inboundTag": ["p2"], "outboundTag": "p2"}
    assert result["outbounds"][1]["streamSettings"]["wsSettings"]["headers"]["Host"] == "front.example"


def test_socks_auth_and_hysteria():
    c = parse_link("hy2://secret@example.com:443").asdict()
    config = build_config([(1, c, 10800)], Settings(socks_auth=True, socks_password="password"))
    assert config["inbounds"][0]["settings"]["accounts"][0]["pass"] == "password"
    out = config["outbounds"][1]
    assert out["protocol"] == "hysteria"
    assert out["streamSettings"]["hysteriaSettings"]["auth"] == "secret"


def test_checksum_and_platform():
    verify_digest(b"data", "SHA256= " + hashlib.sha256(b"data").hexdigest())
    with pytest.raises(RuntimeError):
        verify_digest(b"data", "0" * 64)
    assert asset_name("Windows", "AMD64") == "Xray-windows-64.zip"
    assert asset_name("Linux", "aarch64") == "Xray-linux-arm64-v8a.zip"


def test_settings_validation():
    for params in ({"pool_size": 51}, {"pool_port_range": (20000, 20100)}, {"web_host": "0.0.0.0"}, {"require_udp": True}, {"pool_port_range": (10800, 10802)}, {"allowed_geo": ["germany"]}):
        with pytest.raises(ValueError):
            Settings(**params)
