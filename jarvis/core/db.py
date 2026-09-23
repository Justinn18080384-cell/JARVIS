"""Lokale SQLite-Datenbank für Gedächtnis, Protokoll und Automationen."""
import json
import sqlite3
import threading

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts(
    key TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    value TEXT NOT NULL,
    category TEXT DEFAULT 'allgemein',
    created REAL, updated REAL
);
CREATE TABLE IF NOT EXISTS activity(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, kind TEXT, text TEXT, ok INTEGER
);
CREATE TABLE IF NOT EXISTS automations(
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,          -- profile | routine | command
    name TEXT NOT NULL,
    data TEXT NOT NULL,          -- JSON
    enabled INTEGER DEFAULT 1,
    updated REAL
);
CREATE TABLE IF NOT EXISTS kv(
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS rooms(
    name TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS devices(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    room TEXT,
    type TEXT,                   -- light | plug | tv | speaker | other
    provider TEXT,               -- homeassistant | virtual | ...
    address TEXT,                -- z. B. entity_id
    state TEXT,
    data TEXT
);
"""


class DB:
    def __init__(self):
        self._lock = threading.RLock()
        self._conn = None

    def conn(self):
        if self._conn is None:
            paths.DATA.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(paths.DB_FILE, check_same_thread=False, timeout=10)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        return self._conn

    def execute(self, sql, params=()):
        with self._lock:
            cur = self.conn().execute(sql, params)
            self.conn().commit()
            return cur

    def query(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self.conn().execute(sql, params).fetchall()]

    def one(self, sql, params=()):
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def kv_get(self, key, default=None):
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_set(self, key, value):
        self.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?,?)", (key, json.dumps(value)))

    def close(self):
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None


db = DB()
