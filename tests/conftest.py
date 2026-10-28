from contextlib import asynccontextmanager

import pytest

from proxyhub.config import Settings
from proxyhub.db import Database
from proxyhub.parsers import import_configs
from proxyhub.pool.manager import PoolManager

UUID = "11111111-1111-4111-8111-111111111111"


class FakeCore:
    ready = True
    version = "fake-1"
    protocols = {"vless", "vmess", "ss", "trojan", "hysteria2"}

    def __init__(self):
        self.entries = {}
        self.batches = []

    async def initialize(self):
        self.ready = True

    async def add(self, pid, config, port):
        self.entries[pid] = (config, port)

    async def remove(self, pid):
        self.entries.pop(pid, None)

    @asynccontextmanager
    async def batch(self, entries):
        self.batches.append(entries)
        yield

    async def close(self):
        self.entries.clear()


@pytest.fixture
def db(tmp_path):
    value = Database(tmp_path / "test.db")
    yield value
    value.close()


@pytest.fixture
def pool(db):
    return PoolManager(db, Settings(pool_size=0, measure_geo=False, dedupe_by_exit_ip=False), FakeCore())


def seed(db, n=1, state="valid", source=""):
    import_configs(db, "\n".join(f"vless://{UUID}@host{i}.example:443?security=tls#Test-{i}" for i in range(n)), source)
    db.execute("UPDATE proxies SET state=?,ping_ms=100,speed_mbps=10,next_check_at=0", (state,))
    return db.rows("SELECT * FROM proxies ORDER BY id")
