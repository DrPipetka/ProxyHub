import asyncio
import time

import httpx
import pytest

from proxyhub.pool.lease import ServiceError
from test_api import client_for


@pytest.mark.parametrize('url,download_url', [
    ('https://github.com/owner/repo/blob/main/server.txt',
     'https://raw.githubusercontent.com/owner/repo/main/server.txt'),
    ('https://github.com/owner/repo/raw/refs/heads/main/sub/list.txt?token=example#file',
     'https://raw.githubusercontent.com/owner/repo/refs/heads/main/sub/list.txt?token=example'),
    ('https://raw.githubusercontent.com/owner/repo/main/server.txt',
     'https://raw.githubusercontent.com/owner/repo/main/server.txt'),
])
def test_github_file_urls_download_raw_content(tmp_path, monkeypatch, url, download_url):
    calls = []

    async def download(client, target, limit):
        calls.append(target)
        return b'trojan://secret@example.com:443'

    monkeypatch.setattr('proxyhub.subscriptions.download_limited', download)
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        assert c.post('/proxies', json={'url': url, 'interval_minutes': 0}).json()['accepted'] == 1
        assert calls == [download_url]
        assert c.get('/subscriptions').json()['items'][0]['url'] == url


@pytest.mark.parametrize('url', [
    'https://github.com/owner/repo', 'https://github.com/owner/repo/',
    'https://github.com/owner/repo/tree/main/sub',
])
def test_github_repository_is_not_a_subscription(tmp_path, url):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        response = c.post('/proxies', json={'url': url})
        assert response.status_code == 400
        assert response.json()['error'] == 'subscription_file_required'
        assert c.get('/subscriptions').json()['items'] == []
        assert c.get('/proxies').json()['total'] == 0


@pytest.mark.parametrize('content,expected_error', [
    (b'<!doctype html>\n<html>\n<body>secret</body>\n</html>', 'subscription_html_content'),
    (b'not a config\nstill not a config', 'subscription_invalid_content'),
])
def test_failed_subscription_refresh_does_not_pollute_database(tmp_path, monkeypatch, content, expected_error):
    async def download(*args):
        return content

    monkeypatch.setattr('proxyhub.subscriptions.download_limited', download)
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        payload = {'url': 'https://example.com/sub', 'interval_minutes': 0}
        for _ in range(2):
            response = c.post('/proxies', json=payload)
            assert response.status_code == 400
            assert response.json()['error'] == expected_error
            assert 'secret' not in response.text
        assert c.get('/proxies').json()['total'] == 0
        assert c.get('/subscriptions').json()['items'][0]['last_error'] == expected_error


def test_mixed_subscription_reports_bad_lines_without_storing_them(tmp_path, monkeypatch):
    async def download(*args):
        return b'trojan://secret@example.com:443\nbroken'

    monkeypatch.setattr('proxyhub.subscriptions.download_limited', download)
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        payload = {'url': 'https://example.com/sub', 'interval_minutes': 0}
        for accepted, duplicates in [(1, 0), (0, 1)]:
            result = c.post('/proxies', json=payload).json()
            assert result['accepted'] == accepted and result['duplicates'] == duplicates
            assert result['received'] == 2 and result['parse_errors'] == 1
            assert result['errors'][0]['line'] == 2
            assert c.get('/proxies').json()['total'] == 1


def test_bulk_subscription_import_partial_failure_and_duplicates(tmp_path, monkeypatch):
    calls = []

    async def download(client, url, limit):
        calls.append(url)
        if url.endswith('/offline'):
            raise httpx.ConnectError('private download details')
        return b'trojan://secret@example.com:443'

    monkeypatch.setattr('proxyhub.subscriptions.download_limited', download)
    with client_for(tmp_path) as c:
        payload = {'urls': [' https://example.com/first ', '', 'https://example.com/offline',
                            'file:///secret', 'https://example.com/second', 'https://example.com/first'],
                   'source': 'batch', 'interval_minutes': 5}
        assert c.post('/proxies', json=payload).status_code == 401
        c.post('/auth/login', json={})
        response = c.post('/proxies', json=payload)
        assert response.status_code == 200
        result = response.json()
        assert (result['subscriptions_total'], result['subscriptions_succeeded'], result['subscriptions_failed']) == (4, 2, 2)
        assert result['duplicate_urls'] == 1
        assert result['received'] == 2 and result['accepted'] == 1 and result['duplicates'] == 1
        assert [item['ok'] for item in result['results']] == [True, False, False, True]
        assert result['results'][1]['error'] == 'subscription_download_failed'
        assert result['results'][1]['subscription_id'] is not None
        assert result['results'][2]['error'] == 'invalid_subscription_url'
        assert result['results'][2]['subscription_id'] is None
        assert len(calls) == len(set(calls)) == 3
        assert 'private download details' not in response.text
        rows = c.get('/subscriptions').json()['items']
        assert len(rows) == 3
        assert all(row['source'] == 'batch' and row['interval_minutes'] == 5 for row in rows)
        assert all(row['next_update_at'] is not None for row in rows)
        retry = c.post('/proxies', json={'urls': ['https://example.com/first'], 'interval_minutes': 0}).json()
        assert retry['subscriptions_succeeded'] == 1 and retry['duplicates'] == 1
        assert len(c.get('/subscriptions').json()['items']) == 3
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        assert len(c.get('/subscriptions').json()['items']) == 3


@pytest.mark.parametrize('payload', [
    {'urls': []}, {'urls': [' ', '\r\n']}, {'urls': 'https://example.com'},
    {'urls': [None]}, {'urls': [123]}, {'urls': ['https://example.com'] * 501},
    {'urls': ['https://example.com'], 'url': 'https://example.org'},
    {'urls': ['https://example.com'], 'data': ''},
    {'urls': ['https://example.com'], 'interval_minutes': -1},
    {'urls': ['https://example.com'], 'source': 123},
])
def test_bulk_subscription_validation_has_no_side_effects(tmp_path, payload):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        assert c.post('/proxies', json=payload).status_code == 400
        assert c.get('/subscriptions').json()['items'] == []


def test_bulk_subscription_all_failures(tmp_path, monkeypatch):
    async def download(*args):
        return b''

    monkeypatch.setattr('proxyhub.subscriptions.download_limited', download)
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        result = c.post('/proxies', json={'urls': ['https://example.com/empty', 'invalid']}).json()
        assert result['subscriptions_succeeded'] == 0 and result['subscriptions_failed'] == 2
        assert result['accepted'] == result['received'] == 0
        assert result['results'][0]['error'] == 'subscription_invalid_content'
        assert len(c.get('/subscriptions').json()['items']) == 1


def test_subscription_import_schedule_pause_and_persistence(tmp_path, monkeypatch):
    async def download(*args):
        return b'trojan://secret@example.com:443'
    monkeypatch.setattr('proxyhub.subscriptions.download_limited', download)
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        result = c.post('/proxies', json={'url': 'https://example.com/sub', 'source': 'test', 'interval_minutes': 5})
        assert result.status_code == 200
        assert result.json()['accepted'] == 1
        sid = result.json()['subscription_id']
        row = c.get('/subscriptions').json()['items'][0]
        assert row['last_success_at'] and row['duration_ms'] >= 0
        assert 299 <= row['next_update_at'] - row['last_success_at'] <= 302
        assert c.post(f'/subscriptions/{sid}/refresh').json()['duplicates'] == 1
        assert c.patch(f'/subscriptions/{sid}', json={'interval_minutes': 0}).status_code == 200
        assert c.get('/subscriptions').json()['items'][0]['next_update_at'] is None
        assert c.get('/proxies').json()['total'] == 1
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        row = c.get('/subscriptions').json()['items'][0]
        assert row['id'] == sid and row['interval_minutes'] == 0 and row['last_success_at']
        assert c.delete(f'/subscriptions/{sid}').status_code == 200
        assert c.get('/proxies').json()['total'] == 1
        assert c.get('/subscriptions').json()['items'] == []


def test_subscription_validation_failure_retry_and_deduplication(tmp_path, monkeypatch):
    async def fail(*args):
        raise httpx.ConnectError('secret URL must not appear')
    monkeypatch.setattr('proxyhub.subscriptions.download_limited', fail)
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        for value in (-1, 525601, 'bad', True, 0.5):
            assert c.post('/proxies', json={'url': 'https://example.com/sub', 'interval_minutes': value}).status_code == 400
        for url in ('file:///etc/passwd', 'https://', 'https://[bad'):
            assert c.post('/proxies', json={'url': url}).status_code == 400
        assert c.get('/subscriptions').json()['items'] == []
        payload = {'url': 'https://example.com/sub', 'interval_minutes': 2}
        for _ in range(2):
            response = c.post('/proxies', json=payload)
            assert response.status_code == 400 and 'secret' not in response.text
        rows = c.get('/subscriptions').json()['items']
        assert len(rows) == 1
        row = rows[0]
        assert row['last_error'] == 'subscription_download_failed'
        assert row['last_success_at'] is None
        assert 119 <= row['next_update_at'] - time.time() <= 121
        assert c.patch('/subscriptions/999', json={'interval_minutes': 10}).status_code == 404
        assert c.patch(f"/subscriptions/{row['id']}", json={'interval_minutes': -1}).status_code == 400


@pytest.mark.asyncio
async def test_scheduler_due_only_and_concurrent_refresh(tmp_path, monkeypatch):
    from proxyhub.service import Service
    from conftest import FakeCore
    service = Service(tmp_path, core=FakeCore(), background=False)
    manager = service.subscriptions
    calls = []
    started, finish = asyncio.Event(), asyncio.Event()
    async def download(client, url, limit):
        calls.append(url)
        started.set()
        await finish.wait()
        return b'trojan://secret@example.com:443'
    monkeypatch.setattr('proxyhub.subscriptions.download_limited', download)
    try:
        sid = manager.save('https://example.com/due', '', 1)
        manager.save('https://example.com/paused', '', 0)
        manager.save('https://example.com/future', '', 60)
        service.db.execute('UPDATE subscriptions SET next_update_at=? WHERE id=?', (time.time()-1, sid))
        task = asyncio.create_task(manager.tick())
        await asyncio.wait_for(started.wait(), 5)
        assert manager.list()[-1]['updating']
        with pytest.raises(ServiceError, match='subscription_updating'):
            await manager.refresh(sid)
        finish.set()
        await task
        await manager.tick()
        assert calls == ['https://example.com/due']
        assert manager.get(sid)['next_update_at'] > time.time()
    finally:
        finish.set()
        await service.close()
