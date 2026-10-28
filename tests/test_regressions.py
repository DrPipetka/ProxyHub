import asyncio
import time

import pytest

from proxyhub.checker.pipeline import Pipeline, Result
from proxyhub.checker.scheduler import Scheduler
from proxyhub.instance import InstanceLock
from conftest import seed


def test_second_instance_cannot_reap_first(tmp_path):
    first = InstanceLock(tmp_path)
    try:
        with pytest.raises(RuntimeError):
            InstanceLock(tmp_path)
    finally:
        first.close()
    second = InstanceLock(tmp_path)
    second.close()


async def test_batch_endpoint_outage_not_blacklisted(pool, monkeypatch):
    seed(pool.db, 2, 'new')
    async def cheap(self, port):
        return Result(reason='timeout', stage='ping')
    async def unavailable(self, stage):
        return False
    monkeypatch.setattr(Pipeline, 'cheap', cheap)
    monkeypatch.setattr(Pipeline, 'test_direct', unavailable)
    s = Scheduler(pool.db, pool, pool.core, None)
    rows = await s.claim(2)
    s.running = 2
    await s.run_batch(rows)
    for row in pool.db.rows('SELECT * FROM proxies'):
        assert row['state'] == 'new' and row['fails'] == 0
        assert row['next_check_at'] > time.time()


async def test_failed_master_does_not_strand_duplicate(pool):
    pool.settings.dedupe_by_exit_ip = True
    seed(pool.db, 2, 'new')
    s = Scheduler(pool.db, pool, pool.core, None)
    rows = await s.claim(2)
    master, duplicate = Result(ok=True, exit_ip='1.1.1.1'), Result(ok=True, exit_ip='1.1.1.1')
    await s.reserve_exit(1, master)
    await s.reserve_exit(2, duplicate)
    master.ok, master.reason = False, 'speed_low'
    await s.finish(rows[0], master)
    await s.finish(rows[1], duplicate)
    assert pool.get(1)['state'] == 'invalid'
    assert pool.get(2)['state'] == 'new'


async def test_manual_action_during_check_ignores_old_result(pool):
    seed(pool.db, state='new')
    s = Scheduler(pool.db, pool, pool.core, None)
    old = (await s.claim(1))[0]
    await pool.action('recheck', 1)
    newer = (await s.claim(1))[0]
    await s.finish(old, Result(ok=False, reason='timeout'))
    assert pool.get(1)['checking'] == 1
    await s.finish(newer, Result(ok=True))
    assert pool.get(1)['state'] == 'valid' and pool.get(1)['fails'] == 0


async def test_shrink_pool_preserves_leases(pool):
    seed(pool.db, 5)
    pool.settings.pool_size = 3
    await pool.tick()
    leased = await pool.acquire()
    await pool.tick()
    assert len(pool.core.entries) == 4
    pool.settings.pool_size = 1
    await pool.tick()
    assert len(pool.core.entries) == 2
    assert pool.get(leased['id'])['state'] == 'leased'


async def test_deleted_proxy_id_not_reused_by_inflight_check(pool):
    seed(pool.db, state='new')
    s = Scheduler(pool.db, pool, pool.core, None)
    old = (await s.claim(1))[0]
    await pool.delete()
    new = seed(pool.db, state='new')[0]
    assert new['id'] != old['id']
    await s.finish(old, Result(ok=True))
    assert pool.get(new['id'])['state'] == 'new'


async def test_delete_invalid_scope_does_not_resurrect_selected_duplicates(pool):
    seed(pool.db, 2, 'invalid')
    pool.db.execute("UPDATE proxies SET state='duplicate',master_id=1 WHERE id=2")
    assert await pool.delete('invalid') == 2
    assert pool.db.one('SELECT COUNT(*) n FROM proxies')['n'] == 0


async def test_promotion_shortens_valid_schedule_to_active_interval(pool):
    seed(pool.db)
    pool.db.execute('UPDATE proxies SET next_check_at=?', (time.time()+6*3600,))
    leased = await pool.acquire()
    assert leased['next_check_at'] <= time.time() + pool.settings.recheck_active_minutes * 60
    assert leased['next_check_at'] > time.time()


async def test_optional_cooldown_recheck_can_really_be_disabled(pool):
    seed(pool.db, state='cooldown')
    pool.db.execute('UPDATE proxies SET cooldown_until=0')
    pool.settings.recheck_after_cooldown = False
    await pool.tick()
    s = Scheduler(pool.db, pool, pool.core, None)
    assert pool.get(1)['state'] == 'valid'
    assert await s.claim(1) == []
