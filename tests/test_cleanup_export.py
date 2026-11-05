import asyncio

import pytest
from fastapi.testclient import TestClient

from conftest import FakeCore, seed
from proxyhub.api.routes import delete, delete_subscriptions
from proxyhub.main import create_app
from proxyhub.service import Service
from starlette.requests import Request


def client_for(tmp_path):
    return TestClient(create_app(tmp_path, core=FakeCore(), background=False), base_url='http://127.0.0.1')


def test_cleanup_subscriptions_confirmation_and_scope(tmp_path):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        s = c.app.state.service
        seed(s.db, 2, source='test')
        s.subscriptions.save('https://example.com/sub', 'test', 60)
        assert c.delete('/subscriptions').status_code == 400
        assert c.delete('/proxies').status_code == 400
        assert len(s.subscriptions.list()) == 1
        assert c.delete('/proxies?confirm=yes&scope=invalid').json() == {'deleted': 0, 'subscriptions_deleted': 0}
        assert c.delete('/proxies?confirm=yes&scope=source&source=test').json() == {'deleted': 2, 'subscriptions_deleted': 0}
        assert len(s.subscriptions.list()) == 1
        seed(s.db, 2)
        s.db.history(3, {'ok': True})
        token = s.settings.api_token
        result = c.delete('/proxies?confirm=yes').json()
        assert result == {'deleted': 2, 'subscriptions_deleted': 1}
        assert s.subscriptions.list() == []
        assert s.db.rows('SELECT * FROM history') == []
        assert s.db.rows('SELECT * FROM proxies') == []
        assert s.db.settings().api_token == token


def test_delete_all_subscriptions_preserves_configs_and_connections(tmp_path):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        s = c.app.state.service
        seed(s.db, 2)
        s.subscriptions.save('https://example.com/a', '', 0)
        s.subscriptions.save('https://example.com/b', '', 60)
        proxy = c.get('/proxy').json()
        assert c.delete('/subscriptions?confirm=yes', headers={'Origin': 'https://evil.example'}).status_code == 403
        assert c.delete('/subscriptions?confirm=yes').json() == {'deleted': 2}
        assert s.subscriptions.list() == []
        assert c.get('/proxies').json()['total'] == 2
        assert c.get('/count').json()['count'] == 1
        assert s.core.entries[proxy['id']][1] == proxy['port']
        assert c.delete('/subscriptions?confirm=yes').json() == {'deleted': 0}


@pytest.mark.parametrize('path', ['/export/configs', '/export/proxies', '/subscriptions?confirm=yes'])
def test_new_operations_require_auth(tmp_path, path):
    with client_for(tmp_path) as c:
        response = c.delete(path) if path.startswith('/subscriptions') else c.get(path)
        assert response.status_code == 401
        token = c.app.state.service.settings.api_token
        response = c.request('DELETE' if path.startswith('/subscriptions') else 'GET', path,
                             headers={'Authorization': 'Bearer ' + token})
        assert response.status_code == 200


def test_export_all_configs_without_pagination_and_preserve_raw(tmp_path):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        s = c.app.state.service
        seed(s.db, 205)
        c.post('/proxies', json={'data': 'not a config'})
        clash = {'proxies': [{'name': 'Пример', 'type': 'trojan', 'server': 'clash.example', 'port': 443, 'password': 'secret'}]}
        assert c.post('/proxies', json=clash).json()['accepted'] == 1
        s.db.execute("UPDATE proxies SET state='invalid' WHERE id=1")
        expected = [row['raw'] for row in s.db.rows('SELECT raw FROM proxies ORDER BY id')]
        response = c.get('/export/configs')
        assert response.status_code == 200
        assert response.text == ''.join(raw + '\n' for raw in expected)
        assert response.headers['content-disposition'] == 'attachment; filename="proxyhub-configs.txt"'
        assert response.headers['cache-control'] == 'no-store'
        response = c.get('/export/configs?format=json')
        assert response.json() == expected
        assert len(expected) == 207
        assert response.headers['content-disposition'].endswith('"proxyhub-configs.json"')
        assert c.get('/export/configs?format=csv').status_code == 400
        assert s.db.one('SELECT COUNT(*) n FROM proxies WHERE lease_expires_at IS NOT NULL')['n'] == 0


def test_export_all_proxies_including_leased_and_permanent(tmp_path):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        s = c.app.state.service
        seed(s.db, 3)
        # A valid config with no port is excluded; exports do not raise or lease it.
        s.db.execute("UPDATE proxies SET state='active',port=10800 WHERE id=1")
        s.db.execute("UPDATE proxies SET state='leased',port=10801,lease_expires_at=1234567890 WHERE id=2")
        s.pool.permanent.port = 1080
        live = s.pool.settings.model_copy(update={
            'advertise_host': '::1', 'socks_auth': True, 'socks_username': 'user@name', 'socks_password': 'p:/?#% word',
        })
        s.pool.settings = live
        expected = [f'socks5://user%40name:p%3A%2F%3F%23%25%20word@[::1]:{port}' for port in (10800, 10801, 1080)]
        before = s.db.rows('SELECT id,state,port,lease_expires_at FROM proxies ORDER BY id')
        assert c.get('/export/proxies').text.splitlines() == expected
        assert c.get('/export/proxies?format=json').json() == expected
        assert s.db.rows('SELECT id,state,port,lease_expires_at FROM proxies ORDER BY id') == before
        assert s.core.entries == {}
        # Persisted restart-only credentials must not leak into a running endpoint export.
        s.settings = s.settings.model_copy(update={'socks_password': 'future-password'})
        s.pool.settings = live.model_copy(update={'socks_auth': False, 'advertise_host': '127.0.0.1'})
        assert c.get('/export/proxies').text.splitlines() == [f'socks5://127.0.0.1:{port}' for port in (10800, 10801, 1080)]


def test_empty_exports_and_schema(tmp_path):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        schema = c.get('/openapi.json').json()
        for kind in ('proxies', 'configs'):
            assert c.get('/export/' + kind).text == ''
            assert c.get('/export/' + kind + '?format=json').json() == []
            content = schema['paths']['/export/' + kind]['get']['responses']['200']['content']
            assert 'text/plain' in content and 'application/json' in content
        assert 'delete' in schema['paths']['/subscriptions']


@pytest.mark.asyncio
@pytest.mark.parametrize('full', [False, True])
async def test_deletion_during_bulk_download_cannot_repopulate_database(tmp_path, monkeypatch, full):
    s = Service(tmp_path, core=FakeCore(), background=False)
    started, finish = asyncio.Event(), asyncio.Event()
    downloads = []

    async def download(client, url, limit):
        downloads.append(url)
        if len(downloads) == 4:
            started.set()
        await finish.wait()
        return b'trojan://secret@example.com:443'

    monkeypatch.setattr('proxyhub.subscriptions.download_limited', download)
    task = asyncio.create_task(s.subscriptions.import_many([f'https://example.com/{i}' for i in range(6)], '', 60))
    try:
        await asyncio.wait_for(started.wait(), 5)
        seed(s.db, 1)
        request = Request({'type': 'http', 'app': type('App', (), {'state': type('State', (), {'service': s, 'import_lock': s.import_lock})()})()})
        if full:
            result = await delete(request, confirm='yes', scope='all')
            assert result == {'deleted': 1, 'subscriptions_deleted': 4}
        else:
            assert await delete_subscriptions(request, confirm='yes') == {'deleted': 4}
        finish.set()
        result = await asyncio.wait_for(task, 5)
        assert result['subscriptions_succeeded'] == 0
        assert result['subscriptions_failed'] == 6
        assert len(downloads) == 4  # Two queued URLs never recreate subscriptions.
        assert not s.subscriptions.running
        assert s.subscriptions.list() == []
        assert s.db.one('SELECT COUNT(*) n FROM proxies')['n'] == (0 if full else 1)
        # New imports after deletion still work, including the same URL.
        sid = s.subscriptions.save('https://example.com/0', '', 0)
        assert (await s.subscriptions.refresh(sid))['accepted'] == 1
    finally:
        finish.set()
        await task
        await s.close()
