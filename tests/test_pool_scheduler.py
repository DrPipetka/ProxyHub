import asyncio
import time

import pytest

from proxyhub.checker.pipeline import Result
from proxyhub.checker.scheduler import Scheduler
from proxyhub.pool.lease import ServiceError
from conftest import seed


async def test_twenty_concurrent_leases_unique(pool):
    seed(pool.db, 20)
    rows = await asyncio.gather(*(pool.acquire() for _ in range(20)))
    assert len({r["id"] for r in rows}) == 20
    assert len({r["port"] for r in rows}) == 20
    with pytest.raises(ServiceError, match="no_proxy_available"):
        await pool.acquire()


async def test_used_closes_port_cooldown_and_recheck(pool):
    seed(pool.db)
    leased = await pool.acquire()
    old_port = leased["port"]
    row = await pool.action("used", leased["id"])
    assert row["state"] == "cooldown" and row["port"] is None
    assert old_port not in pool.ports.used and not pool.core.entries
    with pytest.raises(ServiceError):
        await pool.acquire()
    pool.db.execute("UPDATE proxies SET cooldown_until=0")
    await pool.tick()
    assert pool.get(row["id"])["state"] == "new"


async def test_ttl_and_raw_config_no_process(pool):
    seed(pool.db)
    peek = await pool.acquire(config=True, lease=False)
    assert peek["state"] == "valid"
    raw = await pool.acquire(config=True)
    assert raw["state"] == "leased" and raw["port"] is None and not pool.core.entries
    pool.db.execute("UPDATE proxies SET lease_expires_at=0")
    await pool.tick()
    assert pool.get(raw["id"])["state"] == "cooldown"


async def test_blacklist_manual_recheck_and_success(pool):
    seed(pool.db)
    for _ in range(5):
        await pool.action("invalid", 1)
    assert pool.get(1)["state"] == "blacklisted"
    scheduler = Scheduler(pool.db, pool, pool.core, None)
    assert await scheduler.claim(1) == []
    await pool.action("recheck", 1)
    rows = await scheduler.claim(1)
    assert len(rows) == 1
    await scheduler.finish(rows[0], Result(ok=True, ping_ms=100))
    assert pool.get(1)["state"] == "valid" and pool.get(1)["fails"] == 0


async def test_scheduler_priority_and_retry(pool):
    seed(pool.db, 6, "new")
    pool.db.execute("UPDATE proxies SET manual_check=1 WHERE id=6")
    pool.db.execute("UPDATE proxies SET state='active' WHERE id=5")
    pool.db.execute("UPDATE proxies SET state='invalid',next_check_at=? WHERE id=1", (time.time()+3600,))
    s = Scheduler(pool.db, pool, pool.core, None)
    rows = await s.claim(3)
    assert [r["id"] for r in rows] == [6, 5, 2]
    assert len(await s.claim(10)) == 2


async def test_stale_check_cannot_undo_disable(pool):
    seed(pool.db, state="new")
    s = Scheduler(pool.db, pool, pool.core, None)
    row = (await s.claim(1))[0]
    await pool.action("disable", 1)
    await s.finish(row, Result(ok=True))
    assert pool.get(1)["state"] == "disabled"


async def test_exit_dedup_and_master_failure(pool):
    pool.settings.dedupe_by_exit_ip = True
    seed(pool.db, 2, "new")
    s = Scheduler(pool.db, pool, pool.core, None)
    rows = await s.claim(2)
    a, b = Result(ok=True, exit_ip="1.2.3.4"), Result(ok=True, exit_ip="1.2.3.4")
    await s.reserve_exit(1, a)
    await s.reserve_exit(2, b)
    assert b.master_id == 1
    await s.finish(rows[0], a)
    await s.finish(rows[1], b)
    assert pool.get(2)["state"] == "duplicate"
    await pool.action("invalid", 1)
    assert pool.get(2)["state"] == "new"


async def test_test_error_does_not_increase_failures(pool):
    seed(pool.db, state="new")
    s = Scheduler(pool.db, pool, pool.core, None)
    row = (await s.claim(1))[0]
    await s.finish(row, Result(reason="test_url_unavailable:ping", test_error=True))
    updated = pool.get(1)
    assert updated["state"] == "new" and updated["fails"] == 0
    assert updated["next_check_at"] > time.time()


async def test_active_failure_breaks_lease(pool):
    seed(pool.db)
    await pool.acquire()
    s = Scheduler(pool.db, pool, pool.core, None)
    row = (await s.claim(1))[0]
    await s.finish(row, Result(reason="timeout"))
    updated = pool.get(1)
    assert updated["state"] == "invalid" and updated["lease_status"] == "broken" and updated["port"] is None


async def test_recovery_and_history_limit(pool):
    seed(pool.db, 3)
    await pool.acquire()
    pool.db.execute("UPDATE proxies SET state='checking' WHERE id=2")
    pool.db.recover()
    assert pool.get(1)["state"] == "valid" and pool.get(1)["port"] is None
    assert pool.get(2)["state"] == "new"
    for _ in range(25):
        pool.db.history(1, {"ok": True})
    assert pool.db.one("SELECT COUNT(*) n FROM history")["n"] == 20


async def test_scheduler_with_mocked_core(pool, monkeypatch):
    seed(pool.db, 3, "new")
    from proxyhub.checker.pipeline import Pipeline
    async def cheap(self, port):
        return Result(ok=True, ping_ms=42)
    monkeypatch.setattr(Pipeline, "cheap", cheap)
    s = Scheduler(pool.db, pool, pool.core, None)
    rows = await s.claim(3)
    s.running = 3
    await s.run_batch(rows)
    assert all(r["state"] == "valid" for r in pool.db.rows("SELECT state FROM proxies"))
    assert s.running == 0 and not s.ports.used
    assert len(pool.core.batches) == 1 and len(pool.core.batches[0]) == 3


@pytest.mark.parametrize("rejected, retry_fails", [(set(), True), ({2}, False), ({2}, True), ({1, 2, 3}, False)])
async def test_rejected_config_does_not_block_batch_peers(pool, monkeypatch, rejected, retry_fails):
    from contextlib import asynccontextmanager
    from proxyhub.checker.pipeline import Pipeline
    from proxyhub.core.adapter import CoreError

    seed(pool.db, 3, "new")
    batches, checked = [], []

    @asynccontextmanager
    async def batch(entries):
        ids = [pid for pid, _, _ in entries]
        batches.append(ids)
        if len(batches) == 1 or retry_fails:
            raise CoreError("xray_process_exited")
        yield

    async def validate(config):
        return int(config["outbounds"][1]["tag"][1:]) not in rejected

    async def cheap(self, port):
        checked.append(port)
        return Result(ok=True, ping_ms=42)

    monkeypatch.setattr(pool.core, "batch", batch)
    monkeypatch.setattr(pool.core, "validate", validate, raising=False)
    monkeypatch.setattr(Pipeline, "cheap", cheap)
    scheduler = Scheduler(pool.db, pool, pool.core, None)
    rows = await scheduler.claim(3)
    scheduler.running = len(rows)
    await scheduler.run_batch(rows)

    retry = bool(rejected) and len(rejected) < 3
    assert batches == [[1, 2, 3]] + ([[i for i in (1, 2, 3) if i not in rejected]] if retry else [])
    for row in pool.db.rows("SELECT * FROM proxies"):
        if row["id"] in rejected:
            assert row["state"] == "unsupported"
            assert row["reason"] == "unsupported:core_config_rejected"
        elif retry and not retry_fails:
            assert row["state"] == "valid" and row["reason"] is None
        else:
            assert row["state"] == "new" and row["reason"] == "core_batch_unavailable"
        assert row["fails"] == 0 and row["checking"] == 0
    assert len(checked) == (3 - len(rejected) if retry and not retry_fails else 0)
    assert scheduler.running == 0 and not scheduler.ports.used and not scheduler.reservations
