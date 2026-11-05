import asyncio
import socket

import pytest

from conftest import FakeCore, seed
from proxyhub.config import Settings
from proxyhub.core.adapter import CoreError
from proxyhub.core.xray import XrayAdapter
from proxyhub.pool.lease import ServiceError
from proxyhub.service import Service
from test_api import client_for


async def healthy(*args):
    return True


async def unhealthy(*args):
    return len(args) == 1  # Test URLs still work directly.


async def test_fixed_port_switch_no_lease_and_failover(pool, monkeypatch):
    seed(pool.db, 3)
    pool.settings.permanent_proxy = True
    permanent = pool.permanent
    monkeypatch.setattr(permanent, 'probe', healthy)
    await permanent.tick()
    first, port = permanent.proxy_id, permanent.port
    assert permanent.status()['ready'] and port == 1080
    assert pool.core.entries[0][1] == port
    leased = await pool.acquire()
    assert leased['id'] != first
    assert pool.get(first)['state'] == 'valid'
    assert pool.get(first)['lease_expires_at'] is None
    await permanent.switch()
    second = permanent.proxy_id
    assert second not in (first, leased['id']) and permanent.port == port
    assert pool.get(first)['state'] == 'valid'  # Host-specific problem is not a global failure.
    monkeypatch.setattr(permanent, 'probe', unhealthy)
    await permanent.tick()
    assert permanent.proxy_id == first and permanent.port == port
    assert pool.get(second)['state'] == 'invalid'
    monkeypatch.setattr(permanent, 'probe', healthy)
    await permanent.tick()
    assert permanent.status()['ready']


async def test_no_alternative_keeps_current_listener(pool, monkeypatch):
    seed(pool.db)
    pool.settings.permanent_proxy = True
    monkeypatch.setattr(pool.permanent, 'probe', healthy)
    await pool.permanent.tick()
    entry = pool.core.entries[0]
    with pytest.raises(ServiceError) as exc:
        await pool.permanent.switch()
    assert exc.value.code == 'no_alternative_config'
    assert pool.core.entries[0] == entry and pool.permanent.status()['ready']
    pool.settings.permanent_port = 1081
    await pool.permanent.tick()
    assert pool.permanent.port == 1081 and pool.permanent.status()['ready']
    await pool.action('disable', pool.permanent.proxy_id)
    assert 0 not in pool.core.entries
    await pool.permanent.tick()
    assert not pool.permanent.status()['ready']


async def test_delete_releases_dedicated_inbound(pool, monkeypatch):
    seed(pool.db, source='permanent')
    pool.settings.permanent_proxy = True
    monkeypatch.setattr(pool.permanent, 'probe', healthy)
    await pool.permanent.tick()
    assert await pool.delete('source', 'other') == 0
    assert 0 in pool.core.entries
    assert await pool.delete('source', 'permanent') == 1
    assert 0 not in pool.core.entries and pool.permanent.proxy_id is None


async def test_health_check_does_not_hold_lock_or_change_new_config(pool, monkeypatch):
    seed(pool.db, 2)
    pool.settings.permanent_proxy = True
    started, finish = asyncio.Event(), asyncio.Event()

    async def delayed(*args):
        started.set()
        await finish.wait()
        return False

    monkeypatch.setattr(pool.permanent, 'probe', delayed)
    task = asyncio.create_task(pool.permanent.tick())
    try:
        await asyncio.wait_for(started.wait(), 2)
        await asyncio.wait_for(pool.permanent.switch(), 2)
        current = pool.permanent.proxy_id
        finish.set()
        await task
        assert pool.permanent.proxy_id == current
        assert pool.get(current)['state'] == 'valid'
    finally:
        finish.set()
        await task


async def test_test_url_outage_does_not_discard_config(pool, monkeypatch):
    seed(pool.db)
    pool.settings.permanent_proxy = True

    async def unavailable(*args):
        return False

    monkeypatch.setattr(pool.permanent, 'probe', unavailable)
    await pool.permanent.tick()
    assert pool.permanent.error == 'test_url_unavailable'
    assert pool.permanent.proxy_id and 0 in pool.core.entries
    assert pool.get(pool.permanent.proxy_id)['state'] == 'valid'
    assert pool.get(pool.permanent.proxy_id)['fails'] == 0


async def test_stop_restart_and_settings_persist(tmp_path, monkeypatch):
    service = Service(tmp_path, core=FakeCore(), background=False)
    try:
        seed(service.db)
        service.settings.permanent_proxy = True
        service.settings.permanent_port = 1090
        service.db.save_settings(service.settings)
        monkeypatch.setattr(service.pool.permanent, 'probe', healthy)
        await service.pool.permanent.tick()
        assert service.status()['permanent_proxy']['ready']
        # Simulate the pool process dying, followed by a fresh core.
        service.core.entries.clear()
        service.pool.permanent.reset()
        await service.pool.permanent.tick()
        assert service.pool.permanent.port == 1090
        await service.set_enabled(False)
        assert not service.status()['permanent_proxy']['ready'] and not service.core.entries
    finally:
        await service.close()
    service = Service(tmp_path, core=FakeCore(), background=False)
    try:
        assert service.settings.permanent_proxy and service.settings.permanent_port == 1090
        await service.set_enabled(True)
        monkeypatch.setattr(service.pool.permanent, 'probe', healthy)
        await service.pool.permanent.tick()
        assert service.pool.permanent.port == 1090
    finally:
        await service.close()


def test_settings_and_switch_api(tmp_path):
    with client_for(tmp_path) as c:
        assert c.post('/permanent-proxy/switch').status_code == 401
        c.post('/auth/login', json={})
        assert c.post('/permanent-proxy/switch').status_code == 409
        for port in (8080, 10800, 20000, 80, 65536):
            assert c.patch('/settings', json={'permanent_proxy': True, 'permanent_port': port}).status_code == 400
        assert c.patch('/settings', json={'permanent_proxy': True, 'permanent_port': 1091}).json()['restart_required'] == []
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', 0))
            response = c.patch('/settings', json={'permanent_port': occupied.getsockname()[1]})
            assert response.status_code == 409 and response.json()['error'] == 'ports_in_use'
        assert c.get('/settings').json()['values']['permanent_port'] == 1091
        assert c.post('/permanent-proxy/switch').status_code == 404
        seed(c.app.state.service.db, 2)
        first = c.post('/permanent-proxy/switch').json()
        second = c.post('/permanent-proxy/switch').json()
        assert first['proxy']['id'] != second['proxy']['id']
        assert first['addr'] == second['addr'] == '127.0.0.1:1091'
        assert c.patch('/settings', json={'permanent_proxy': False}).status_code == 200
        assert 0 not in c.app.state.service.core.entries
        assert c.post('/permanent-proxy/switch').status_code == 409


async def test_replace_rolls_back_failed_route_update(tmp_path, monkeypatch):
    core = XrayAdapter(Settings(), tmp_path)
    config = {'protocol': 'trojan', 'host': 'localhost', 'port': 443, 'credential': 'secret', 'options': {}}
    core.entries[0] = (0, config, 1080)
    calls = []

    async def api(command, data=None, tag=None):
        calls.append((command, data, tag))
        if len(calls) == 2:
            raise CoreError('route_update_failed')

    monkeypatch.setattr(core, '_api', api)
    with pytest.raises(CoreError):
        await core.replace(0, {**config, 'host': 'new.example'}, 1080)
    assert core.entries[0][1] == config
    assert core.outbound_tags[0] == 'p0'
    assert calls[2][1]['routing']['rules'][-1]['outboundTag'] == 'p0'
    assert calls[-1][0] == 'rmo' and calls[-1][2] != 'p0'
