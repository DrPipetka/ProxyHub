import asyncio
import sqlite3
import time

import pytest

from conftest import FakeCore
from proxyhub.db import Database
from proxyhub.service import Service
from test_api import client_for


@pytest.fixture
def feed(monkeypatch):
    content = {'value': b'trojan://secret@old.example:443'}

    async def download(*args):
        return content['value']

    monkeypatch.setattr('proxyhub.subscriptions.download_limited', download)
    return content


def add(c, url='https://example.com/a', **options):
    response = c.post('/proxies', json={'url': url, 'interval_minutes': 0, **options})
    assert response.status_code == 200, response.text
    return response.json()['subscription_id']


def refresh(c, sid):
    response = c.post(f'/subscriptions/{sid}/refresh')
    assert response.status_code == 200, response.text
    return response.json()


def test_bulk_settings_partial_updates_validation_and_persistence(tmp_path, feed):
    with client_for(tmp_path) as c:
        assert c.patch('/subscriptions', json={'interval_minutes': 5}).status_code == 401
        c.post('/auth/login', json={})
        a, b = add(c), add(c, 'https://example.com/b')
        for payload in ({'interval_minutes': True}, {'interval_minutes': -1},
                        {'interval_minutes': 0.5}, {'interval_minutes': 525601},
                        {'delete_old_configs': 'yes'}, {'keep_working_configs': None}, {'unknown': 1}):
            assert c.patch('/subscriptions', json=payload).status_code == 400
        options = {'interval_minutes': 5, 'delete_old_configs': True, 'keep_working_configs': True}
        assert c.patch('/subscriptions', json=options).json() == {'updated': 2}
        rows = c.get('/subscriptions').json()['items']
        assert all(299 <= r['next_update_at'] - time.time() <= 301 for r in rows)
        assert all(r['delete_old_configs'] and r['keep_working_configs'] for r in rows)
        due = rows[0]['next_update_at']
        assert c.patch(f'/subscriptions/{b}', json={'keep_working_configs': False}).status_code == 200
        assert c.app.state.service.subscriptions.get(b)['next_update_at'] == due
        assert c.patch('/subscriptions/999', json={}).status_code == 404
        assert c.patch('/subscriptions', json={}).json() == {'updated': 0}
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        rows = c.get('/subscriptions').json()['items']
        assert all(r['interval_minutes'] == 5 and r['delete_old_configs'] for r in rows)
        assert c.patch('/subscriptions', json={'interval_minutes': 0}).json() == {'updated': 2}
        assert all(r['next_update_at'] is None for r in c.get('/subscriptions').json()['items'])


@pytest.mark.parametrize('state,keep,removed', [
    ('new', True, 1), ('invalid', True, 1), ('blacklisted', True, 1),
    ('valid', True, 0), ('active', True, 0), ('leased', True, 0),
    ('cooldown', True, 0), ('duplicate', True, 0),
    ('valid', False, 1), ('active', False, 1), ('leased', False, 1),
])
def test_cleanup_states_and_connections(tmp_path, feed, state, keep, removed):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        sid = add(c, delete_old_configs=True, keep_working_configs=keep)
        service = c.app.state.service
        if state in ('active', 'leased'):
            service.db.execute("UPDATE proxies SET state='valid'")
            assert c.get('/proxy').status_code == 200
        service.db.execute('UPDATE proxies SET state=?', (state,))
        service.db.history(1, {'ok': state == 'valid'})
        feed['value'] = b'trojan://secret@fresh.example:443'
        result = refresh(c, sid)
        assert result['removed'] == removed
        assert bool(service.db.one('SELECT * FROM proxies WHERE id=1')) == (not removed)
        assert len(service.db.rows('SELECT * FROM history')) == (0 if removed else 1)
        if state in ('active', 'leased'):
            assert (1 in service.core.entries) == (not removed)


def test_unchanged_failure_keeps_history_blacklist_and_retry(tmp_path, feed):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        sid = add(c, delete_old_configs=True, keep_working_configs=False)
        db = c.app.state.service.db
        db.execute("UPDATE proxies SET state='blacklisted',fails=5,next_check_at=9876543210")
        db.history(1, {'ok': False})
        before = db.one('SELECT * FROM proxies')
        assert refresh(c, sid)['removed'] == 0
        assert db.one('SELECT * FROM proxies') == before
        assert len(db.rows('SELECT * FROM history')) == 1


def test_shared_and_manual_configs_survive_until_last_owner_removes(tmp_path, feed):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        a = add(c, delete_old_configs=True)
        b = add(c, 'https://example.com/b', delete_old_configs=True)
        feed['value'] = b'trojan://secret@fresh.example:443'
        assert refresh(c, a)['removed'] == 0
        assert refresh(c, b)['removed'] == 1
        # A manual duplicate import protects the same record from auto-cleanup.
        c.post('/proxies', json={'data': feed['value'].decode()})
        feed['value'] = b'trojan://secret@next.example:443'
        assert refresh(c, a)['removed'] == 0
        assert refresh(c, b)['removed'] == 0
        assert c.get('/proxies').json()['total'] == 2


@pytest.mark.parametrize('content,status', [(b'', 400), (b'broken', 400),
    (b'<html>error</html>', 400), (b'trojan://secret@fresh.example:443\nbroken', 200)])
def test_failed_or_partial_refresh_never_cleans(tmp_path, feed, content, status):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        sid = add(c, delete_old_configs=True)
        feed['value'] = content
        response = c.post(f'/subscriptions/{sid}/refresh')
        assert response.status_code == status
        assert c.app.state.service.db.one('SELECT id FROM proxies WHERE id=1')
        if status == 200:
            assert response.json()['cleanup_skipped'] and response.json()['removed'] == 0


def test_default_is_append_and_retained_working_can_be_cleaned_after_failure(tmp_path, feed):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        sid = add(c)
        feed['value'] = b'trojan://secret@fresh.example:443'
        assert refresh(c, sid)['removed'] == 0
        db = c.app.state.service.db
        db.execute("UPDATE proxies SET state='valid' WHERE id=1")
        c.patch(f'/subscriptions/{sid}', json={'delete_old_configs': True})
        assert refresh(c, sid)['removed'] == 0
        db.execute("UPDATE proxies SET state='invalid' WHERE id=1")
        assert refresh(c, sid)['removed'] == 1


def test_deleted_subscription_preserves_shared_configs(tmp_path, feed):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        a, b = add(c), add(c, 'https://example.com/b', delete_old_configs=True)
        assert c.delete(f'/subscriptions/{a}').status_code == 200
        feed['value'] = b'trojan://secret@fresh.example:443'
        assert refresh(c, b)['removed'] == 0
        assert c.get('/proxies').json()['total'] == 2


def test_shared_working_config_honors_each_subscriptions_protection(tmp_path, feed):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        a = add(c, delete_old_configs=True, keep_working_configs=True)
        b = add(c, 'https://example.com/b', delete_old_configs=True, keep_working_configs=False)
        db = c.app.state.service.db
        db.execute("UPDATE proxies SET state='valid'")
        feed['value'] = b'trojan://secret@fresh.example:443'
        assert refresh(c, a)['removed'] == 0
        assert refresh(c, b)['removed'] == 0
        assert db.one('SELECT id FROM proxies WHERE id=1')
        db.execute("UPDATE proxies SET state='invalid' WHERE id=1")
        assert refresh(c, a)['removed'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('keep', [True, False])
async def test_inflight_check_cannot_resurrect_cleaned_config(tmp_path, feed, keep):
    from proxyhub.checker.pipeline import Result
    service = Service(tmp_path, core=FakeCore(), background=False)
    try:
        sid = service.subscriptions.save('https://example.com/a', '', 0,
                                         delete_old_configs=True, keep_working_configs=keep)
        await service.subscriptions.refresh(sid)
        claimed = await service.scheduler.claim(1)
        feed['value'] = b'trojan://secret@fresh.example:443'
        result = await service.subscriptions.refresh(sid)
        assert result['removed'] == (0 if keep else 1)
        await service.scheduler.finish(claimed[0], Result(ok=True))
        old = service.db.one('SELECT state FROM proxies WHERE id=1')
        assert old == ({'state': 'valid'} if keep else None)
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('keep', [True, False])
async def test_permanent_proxy_cleanup(tmp_path, feed, keep, monkeypatch):
    service = Service(tmp_path, core=FakeCore(), background=False)

    async def healthy(*args):
        return True

    try:
        service.pool.settings.permanent_proxy = True
        monkeypatch.setattr(service.pool.permanent, 'probe', healthy)
        sid = service.subscriptions.save('https://example.com/a', '', 0,
                                         delete_old_configs=True, keep_working_configs=keep)
        await service.subscriptions.refresh(sid)
        service.db.execute("UPDATE proxies SET state='valid'")
        await service.pool.permanent.tick()
        assert service.pool.permanent.proxy_id == 1
        feed['value'] = b'trojan://secret@fresh.example:443'
        assert (await service.subscriptions.refresh(sid))['removed'] == (0 if keep else 1)
        assert (service.pool.permanent.proxy_id == 1) == keep
        assert (0 in service.core.entries) == keep
    finally:
        await service.close()


def test_old_schema_migration_preserves_unattributed_configs(tmp_path, feed):
    path = tmp_path / 'legacy.db'
    db = Database(path)
    from proxyhub.parsers import import_configs
    import_configs(db, feed['value'].decode())
    db.close()
    with sqlite3.connect(path) as c:
        c.execute('DROP TABLE subscription_proxies')
        c.execute('ALTER TABLE proxies DROP COLUMN independent_import')
        c.execute('ALTER TABLE subscriptions DROP COLUMN delete_old_configs')
        c.execute('ALTER TABLE subscriptions DROP COLUMN keep_working_configs')
    db = Database(path)
    try:
        assert db.one('SELECT independent_import FROM proxies')['independent_import'] == 1
        assert db.rows('SELECT * FROM subscription_proxies') == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_settings_changed_during_download_apply_to_that_refresh(tmp_path, monkeypatch):
    service = Service(tmp_path, core=FakeCore(), background=False)
    started, finish = asyncio.Event(), asyncio.Event()
    content = b'trojan://secret@old.example:443'

    async def download(*args):
        started.set()
        await finish.wait()
        return content

    monkeypatch.setattr('proxyhub.subscriptions.download_limited', download)
    try:
        sid = service.subscriptions.save('https://example.com/a', '', 0)
        finish.set()
        await service.subscriptions.refresh(sid)
        finish.clear()
        started.clear()
        content = b'trojan://secret@fresh.example:443'
        task = asyncio.create_task(service.subscriptions.refresh(sid))
        await asyncio.wait_for(started.wait(), 5)
        service.subscriptions.update_options({'interval_minutes': 5, 'delete_old_configs': True})
        finish.set()
        assert (await task)['removed'] == 1
        assert 299 <= service.subscriptions.get(sid)['next_update_at'] - time.time() <= 301
    finally:
        finish.set()
        await service.close()
