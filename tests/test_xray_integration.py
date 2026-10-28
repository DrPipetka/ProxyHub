"""Local integration only: no public proxy credentials or remote upstream needed."""
import asyncio
import os
import time
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
