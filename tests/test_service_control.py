import asyncio
import pytest

from conftest import FakeCore, seed
from proxyhub.checker.pipeline import Pipeline
from proxyhub.service import Service
from test_api import client_for


def test_controls_auth_stop_resume_and_persistence(tmp_path):
    with client_for(tmp_path) as c:
        for action in ('enable', 'disable'):
            assert c.post('/service/' + action).status_code == 401
        c.post('/auth/login', json={})
        assert c.post('/service/disable', headers={'Origin': 'https://evil.example'}).status_code == 403
        service = c.app.state.service
        seed(service.db, 3)
        assert c.get('/proxy').status_code == 200
        assert c.get('/config').status_code == 200
        assert service.core.entries
        for _ in range(2):
            response = c.post('/service/disable')
            assert response.status_code == 200
            status = response.json()
            assert status['operation'] == {'enabled': False, 'transitioning': False}
            assert status['raised'] == status['leased'] == status['checking'] == 0
            assert status['ports']['pool_used'] == status['ports']['check_used'] == 0
            assert status['xray']['ready'] is False
        assert not service.core.entries
        for path in ('/proxy', '/config', '/config?lease=0', '/proxy?wait=120'):
            response = c.get(path)
            assert response.status_code == 503 and response.json()['error'] == 'service_disabled'
        assert c.get('/proxies').json()['total'] == 3
        assert c.patch('/settings', json={'pool_size': 2}).status_code == 200
        assert c.post('/proxies', json={'data': 'trojan://new@example.com:443'}).json()['accepted'] == 1
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        assert not c.get('/status').json()['operation']['enabled']
        assert c.get('/proxy').status_code == 503
        for _ in range(2):
            assert c.post('/service/enable').json()['operation']['enabled']
        assert c.get('/proxy').status_code == 200
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        assert c.get('/status').json()['operation']['enabled']


async def test_stop_cancels_checks_and_subscriptions_then_resumes(tmp_path, monkeypatch):
    checking, downloading = asyncio.Event(), asyncio.Event()

    async def cheap(self, port):
        checking.set()
        await asyncio.Event().wait()

    async def download(*args):
        downloading.set()
        await asyncio.Event().wait()

    async def idle(self):
        await asyncio.Event().wait()

    monkeypatch.setattr(Pipeline, 'cheap', cheap)
    monkeypatch.setattr('proxyhub.subscriptions.download_limited', download)
    monkeypatch.setattr(Service, 'initialize_loop', idle)
    service = Service(tmp_path, core=FakeCore())
    try:
        seed(service.db, 2, state='new')
        sid = service.subscriptions.save('https://example.com/sub', '', 60)
        service.db.execute('UPDATE subscriptions SET next_update_at=0 WHERE id=?', (sid,))
        await service.start()
        await asyncio.wait_for(asyncio.gather(checking.wait(), downloading.wait()), 5)
        workers = list(service.tasks)
        assert service.scheduler.running and service.scheduler.ports.used
        await asyncio.wait_for(service.set_enabled(False), 5)
        assert all(task.done() for task in workers)
        assert not service.tasks and not service.scheduler.tasks and not service.subscriptions.running
        assert service.scheduler.running == 0 and not service.scheduler.ports.used
        assert all(row['state'] == 'new' and row['checking'] == 0 for row in service.db.rows('SELECT * FROM proxies'))
        assert await service.scheduler.claim(10) == []
        service.db.execute("UPDATE proxies SET state='valid'")
        await service.pool.tick()
        assert not service.core.entries
        # A successful enable starts only one set of workers, including when requested concurrently.
        await asyncio.gather(service.set_enabled(True), service.set_enabled(True))
        assert len(service.tasks) == 4
        await service.set_enabled(False)
    finally:
        await service.close()


async def test_disabled_start_has_no_background_workers(tmp_path):
    service = Service(tmp_path, core=FakeCore())
    await service.set_enabled(False)
    await service.close()
    service = Service(tmp_path, core=FakeCore())
    try:
        await service.start()
        assert not service.tasks and not service.pool.enabled
        await service.set_enabled(True)
        assert len(service.tasks) == 4
    finally:
        await service.close()


async def test_disconnect_does_not_interrupt_stop(tmp_path):
    started, finish = asyncio.Event(), asyncio.Event()

    class SlowCore(FakeCore):
        async def close(self):
            started.set()
            await finish.wait()
            await super().close()

    service = Service(tmp_path, core=SlowCore(), background=False)
    try:
        seed(service.db)
        await service.pool.acquire()
        request = asyncio.create_task(service.set_enabled(False))
        await asyncio.wait_for(started.wait(), 5)
        assert service.status()['operation']['transitioning']
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        finish.set()
        await asyncio.wait_for(asyncio.gather(*service.control_tasks), 5)
        assert not service.pool.ports.used and not service.core.entries
        assert service.status()['operation'] == {'enabled': False, 'transitioning': False}
    finally:
        finish.set()
        await service.close()
