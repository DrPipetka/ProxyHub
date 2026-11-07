"""Local integration only: no public proxy credentials or remote upstream needed."""
import asyncio
import os
import time
from contextlib import suppress
from pathlib import Path

import pytest

from proxyhub.config import Settings
from proxyhub.core.xray import PortAllocator, XrayAdapter, build_config
from proxyhub.parsers.links import parse_link
from conftest import UUID

BIN = Path(__file__).resolve().parents[1] / "bin" / ("xray.exe" if os.name == "nt" else "xray")
pytestmark = pytest.mark.skipif(not BIN.exists(), reason="Install Xray with python -m scripts.install_core")


async def socks_greeting(port):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    writer.write(b'\x05\x01\x00')
    await writer.drain()
    response = await asyncio.wait_for(reader.readexactly(2), 2)
    writer.close()
    await writer.wait_closed()
    return response


async def test_service_disable_stops_xray_and_enable_restarts(tmp_path):
    from conftest import seed
    from proxyhub.service import Service

    service = Service(tmp_path)
    service.settings.xray_path = str(BIN)
    service.settings.auto_download = False
    service.settings.measure_geo = False
    service.settings.pool_size = 1
    service.scheduler.direct_checked = time.time()
    seed(service.db)
    service.db.execute('UPDATE proxies SET next_check_at=?', (time.time() + 3600,))

    async def raised():
        while True:
            row = service.db.one('SELECT port FROM proxies WHERE port IS NOT NULL')
            if row:
                return row['port']
            await asyncio.sleep(0.05)

    try:
        await service.start()
        first_port = await asyncio.wait_for(raised(), 15)
        assert await socks_greeting(first_port) == b'\x05\x00'
        process = service.core.pool
        await service.set_enabled(False)
        assert process.returncode is not None
        assert not service.core.processes and not service.core.ready
        with pytest.raises(OSError):
            await asyncio.open_connection('127.0.0.1', first_port)
        await service.set_enabled(True)
        second_port = await asyncio.wait_for(raised(), 15)
        assert service.core.pool is not process
        assert await socks_greeting(second_port) == b'\x05\x00'
    finally:
        await service.close()


async def test_real_dynamic_api_and_process_cleanup(tmp_path):
    core = XrayAdapter(Settings(xray_path=str(BIN), auto_download=False), tmp_path)
    ports = PortAllocator((21000, 21999))
    p1, p2 = ports.take(), ports.take()
    c = parse_link(f"vless://{UUID}@127.0.0.1:1?security=none").asdict()
    pids = []
    try:
        await core.initialize()
        assert core.ready
        initial_pid = core.pool.pid
        await core.add(1, c, p1)
        await core.add(2, c, p2)
        assert await socks_greeting(p1) == b'\x05\x00'
        assert await socks_greeting(p2) == b'\x05\x00'
        start = time.monotonic()
        await core.remove(1)
        assert time.monotonic() - start < 2
        assert core.pool.pid == initial_pid
        assert await socks_greeting(p2) == b'\x05\x00'
        with pytest.raises(OSError):
            await asyncio.open_connection('127.0.0.1', p1)
        pids = list(core.processes)
    finally:
        await core.close()
    import psutil
    assert all(not psutil.pid_exists(pid) for pid in pids)
    assert not core.processes


async def test_real_supported_generated_configs(tmp_path):
    core = XrayAdapter(Settings(xray_path=str(BIN), auto_download=False), tmp_path)
    try:
        await core.initialize()
        links = [f"vless://{UUID}@example.com:443?security=tls&type=ws&path=%2Fp", 'ss://aes-128-gcm:password@example.com:1234', 'trojan://password@example.com:443']
        if 'hysteria2' in core.protocols:
            links.append('hy2://password@example.com:443')
        for link in links:
            assert await core.validate(build_config([(1, parse_link(link).asdict(), 20000)]))
    finally:
        await core.close()


async def test_real_batch_uses_one_process_and_frees_ports(tmp_path):
    core = XrayAdapter(Settings(xray_path=str(BIN), auto_download=False), tmp_path)
    ports = PortAllocator((22000, 22999))
    p1, p2 = ports.take(), ports.take()
    config = parse_link('trojan://password@127.0.0.1:1').asdict()
    try:
        await core.initialize()
        async with core.batch([(1, config, p1), (2, config, p2)]):
            assert len(core.processes) == 2  # One pool + one checker process.
            assert await socks_greeting(p1) == b'\x05\x00'
            assert await socks_greeting(p2) == b'\x05\x00'
        assert len(core.processes) == 1
        with pytest.raises(OSError):
            await asyncio.open_connection('127.0.0.1', p2)
    finally:
        await core.close()


async def test_scheduler_retries_good_peer_after_real_config_rejection(tmp_path, pool, monkeypatch):
    import json
    from conftest import seed
    from proxyhub.checker.pipeline import Pipeline, Result
    from proxyhub.checker.scheduler import Scheduler

    seed(pool.db, 2, "new")
    bad = parse_link('ss://unknown-cipher:password@127.0.0.1:1').asdict()
    pool.db.execute("UPDATE proxies SET config=? WHERE id=2", (json.dumps(bad),))
    core = XrayAdapter(Settings(xray_path=str(BIN), auto_download=False), tmp_path / "core")
    checked = []

    async def cheap(self, port):
        assert await socks_greeting(port) == b'\x05\x00'
        checked.append(port)
        return Result(ok=True)

    monkeypatch.setattr(Pipeline, "cheap", cheap)
    try:
        await core.initialize()
        pool.core = core
        scheduler = Scheduler(pool.db, pool, core, None)
        rows = await scheduler.claim(2)
        scheduler.running = len(rows)
        await scheduler.run_batch(rows)
        assert pool.get(1)["state"] == "valid"
        assert pool.get(2)["reason"] == "unsupported:core_config_rejected"
        assert len(checked) == 1
        assert not scheduler.ports.used and scheduler.running == 0
        assert len(core.processes) == 1  # Only the pool survives the check.
    finally:
        await core.close()


async def vless_http_peer(reader, writer):
    try:
        # Minimal local VLESS peer: validate the request and return a real HTTP response.
        header = await reader.readexactly(18)
        assert header[0] == 0 and header[1:17].hex() == UUID.replace('-', '')
        await reader.readexactly(header[17])
        request = await reader.readexactly(4)
        assert request[0] == 1  # TCP
        address_size = {1: 4, 3: 16}.get(request[3])
        if request[3] == 2:
            address_size = (await reader.readexactly(1))[0]
        await reader.readexactly(address_size)
        writer.write(b'\x00\x00')
        await writer.drain()
        await reader.readuntil(b'\r\n\r\n')
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize('auth', [False, True])
async def test_permanent_switch_keeps_listener_and_routes_new_connections(tmp_path, auth):
    import httpx
    from proxyhub.pool.permanent import PermanentProxy

    server = await asyncio.start_server(vless_http_peer, '127.0.0.1', 0)
    upstream_port = server.sockets[0].getsockname()[1]
    target = 80
    ports = PortAllocator((23000, 23999))
    fixed, other_port = ports.take(), ports.take()
    settings = Settings(xray_path=str(BIN), auto_download=False, socks_auth=auth,
                        socks_username='user@:', socks_password='pass@:/', ping_urls=[f'http://127.0.0.1:{target}'])
    core = XrayAdapter(settings, tmp_path)

    def proxy(port):
        return httpx.Proxy(f'socks5://127.0.0.1:{port}', auth=(settings.socks_username, settings.socks_password) if auth else None)
    bad = parse_link(f'vless://{UUID}@127.0.0.1:1?security=none').asdict()
    good = {**bad, 'port': upstream_port}
    writer = None
    try:
        await core.initialize()
        await core.add(0, bad, fixed)
        await core.add(1, good, other_port)
        async with httpx.AsyncClient(proxy=proxy(other_port), trust_env=False, timeout=5) as client:
            assert (await client.get(f'http://127.0.0.1:{target}')).text == 'ok'
        await core.remove(1)
        pid = core.pool.pid
        reader, writer = await asyncio.open_connection('127.0.0.1', fixed)
        writer.write(b'\x05\x01\x02' if auth else b'\x05\x01\x00')
        await writer.drain()
        assert await reader.readexactly(2) == (b'\x05\x02' if auth else b'\x05\x00')
        if auth:
            username, password = settings.socks_username.encode(), settings.socks_password.encode()
            writer.write(bytes([1, len(username)]) + username + bytes([len(password)]) + password)
            await writer.drain()
            assert await reader.readexactly(2) == b'\x01\x00'
        await core.replace(0, good, fixed)
        # This socket was accepted before the change; the same inbound is still serving it.
        writer.write(b'\x05\x01\x00\x01\x7f\x00\x00\x01' + target.to_bytes(2, 'big'))
        await writer.drain()
        reply = await asyncio.wait_for(reader.readexactly(10), 5)
        assert reply[1] == 0
        writer.write(b'GET / HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        await writer.drain()
        assert b'200 OK' in await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
        assert await asyncio.wait_for(reader.readexactly(2), 5) == b'ok'
        assert core.pool.pid == pid
        await core.add(1, bad, other_port)
        await core.remove(1)
        assert await PermanentProxy(None).probe(settings, fixed)
        async with httpx.AsyncClient(proxy=proxy(fixed), trust_env=False, timeout=5) as client:
            assert (await client.get(f'http://127.0.0.1:{target}')).text == 'ok'
        await core._restart_pool()
        async with httpx.AsyncClient(proxy=proxy(fixed), trust_env=False, timeout=5) as client:
            assert (await client.get(f'http://127.0.0.1:{target}')).text == 'ok'
    finally:
        if writer:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
        await core.close()
        server.close()
        await server.wait_closed()


async def test_permanent_recovers_same_port_after_core_crash(tmp_path):
    from proxyhub.parsers import import_configs
    from proxyhub.service import Service

    upstream = await asyncio.start_server(vless_http_peer, '127.0.0.1', 0)
    upstream_port = upstream.sockets[0].getsockname()[1]
    service = Service(tmp_path)
    s = service.settings
    s.xray_path, s.auto_download = str(BIN), False
    s.measure_geo, s.measure_ping, s.dedupe_by_exit_ip = False, False, False
    s.pool_size = 0
    s.permanent_port = PortAllocator((24000, 24999)).take()
    s.permanent_proxy = True
    s.ping_urls = ['http://127.0.0.1:80']
    service.scheduler.direct_checked = time.time()
    import_configs(service.db, f'vless://{UUID}@127.0.0.1:{upstream_port}?security=none')
    service.db.execute("UPDATE proxies SET state='valid',next_check_at=?", (time.time() + 3600,))

    async def ready():
        while not service.pool.permanent.status()['ready']:
            await asyncio.sleep(0.05)
        return service.pool.permanent.status()

    async def restarted(process):
        while service.core.pool is process:
            await asyncio.sleep(0.05)

    try:
        await service.start()
        first = await asyncio.wait_for(ready(), 15)
        process = service.core.pool
        process.kill()
        await process.wait()
        # Wait for maintenance to notice the crash before observing readiness again.
        await asyncio.wait_for(restarted(process), 15)
        recovered = await asyncio.wait_for(ready(), 15)
        assert recovered['addr'] == first['addr']
        assert recovered['proxy']['id'] == first['proxy']['id']
        assert service.core.pool.pid != process.pid
    finally:
        await service.close()
        upstream.close()
        await upstream.wait_closed()
