from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .config import Settings, hash_password

STATES = ("new", "checking", "valid", "active", "leased", "cooldown", "invalid", "duplicate", "blacklisted", "disabled", "unsupported", "parse_error")


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        if os.name != "nt":
            os.chmod(path, 0o600)
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            PRAGMA wal_autocheckpoint=10000;
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=10000;
            PRAGMA cache_size=-32768;
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT, hash TEXT UNIQUE, raw TEXT NOT NULL, config TEXT,
                protocol TEXT, host TEXT, remark TEXT, source TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'new', reason TEXT, master_id INTEGER,
                geo TEXT, exit_ip TEXT, ping_ms REAL, speed_mbps REAL, udp INTEGER,
                fails INTEGER NOT NULL DEFAULT 0, port INTEGER UNIQUE, lease_expires_at REAL,
                lease_status TEXT, cooldown_until REAL, last_check_at REAL,
                next_check_at REAL NOT NULL DEFAULT 0, checking INTEGER NOT NULL DEFAULT 0,
                manual_check INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL, updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_state ON proxies(state);
            CREATE INDEX IF NOT EXISTS idx_schedule ON proxies(state, checking, next_check_at);
            CREATE INDEX IF NOT EXISTS idx_manual ON proxies(manual_check, checking);
            CREATE INDEX IF NOT EXISTS idx_next ON proxies(next_check_at);
            CREATE INDEX IF NOT EXISTS idx_geo ON proxies(geo);
            CREATE INDEX IF NOT EXISTS idx_ping ON proxies(ping_ms);
            CREATE INDEX IF NOT EXISTS idx_speed ON proxies(speed_mbps);
            CREATE INDEX IF NOT EXISTS idx_exit ON proxies(exit_ip, state);
            CREATE INDEX IF NOT EXISTS idx_master ON proxies(master_id);
            CREATE INDEX IF NOT EXISTS idx_source ON proxies(source);
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY, proxy_id INTEGER NOT NULL REFERENCES proxies(id) ON DELETE CASCADE,
                checked_at REAL NOT NULL, result TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_history ON history(proxy_id, id DESC);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, time REAL NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL, proxy_id INTEGER);
        """)

    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def execute(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    def rows(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.lock:
            row = self.conn.execute(sql, args).fetchone()
            return dict(row) if row else None

    def settings(self):
        row = self.one("SELECT value FROM settings WHERE id=1")
        if row:
            return Settings.model_validate_json(row["value"])
        password = os.environ.get("PROXYHUB_UI_PASSWORD", "")
        s = Settings(ui_password_hash=hash_password(password) if password else "",
                     web_host=os.environ.get("PROXYHUB_HOST", "127.0.0.1"),
                     web_port=int(os.environ.get("PROXYHUB_PORT", "8080")))
        self.save_settings(s)
        return s

    def save_settings(self, settings):
        self.execute("INSERT OR REPLACE INTO settings VALUES (1,?)", (settings.model_dump_json(),))

    def recover(self):
        self.execute("""UPDATE proxies SET state=CASE WHEN state='checking' THEN 'new' ELSE 'valid' END,
                      port=NULL,lease_expires_at=NULL,lease_status='reset',checking=0,revision=revision+1
                      WHERE state IN ('checking','active','leased')""")
        self.execute("UPDATE proxies SET checking=0,revision=revision+1 WHERE checking=1")

    def event(self, message, proxy_id=None, level="INFO"):
        with self.transaction() as c:
            c.execute("INSERT INTO events(time,level,message,proxy_id) VALUES(?,?,?,?)", (time.time(), level, message, proxy_id))
            c.execute("DELETE FROM events WHERE id <= (SELECT MAX(id)-2000 FROM events)")

    def release_duplicates(self, master_id):
        self.execute("UPDATE proxies SET state='new',master_id=NULL,next_check_at=0,reason=NULL,revision=revision+1 WHERE master_id=? AND state='duplicate'", (master_id,))

    def history(self, proxy_id, result):
        with self.transaction() as c:
            c.execute("INSERT INTO history(proxy_id,checked_at,result) VALUES(?,?,?)", (proxy_id, time.time(), json.dumps(result)))
            c.execute("DELETE FROM history WHERE proxy_id=? AND id NOT IN (SELECT id FROM history WHERE proxy_id=? ORDER BY id DESC LIMIT 20)", (proxy_id, proxy_id))

    def close(self):
        self.conn.close()
