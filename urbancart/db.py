import json
import sqlite3
import threading
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS hubs(name TEXT PRIMARY KEY, status TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS products(sku TEXT PRIMARY KEY, name TEXT, category TEXT, price REAL);
CREATE TABLE IF NOT EXISTS inventory(sku TEXT, hub TEXT, on_hand INTEGER, reserved INTEGER DEFAULT 0,
    PRIMARY KEY(sku, hub));
CREATE TABLE IF NOT EXISTS inventory_mismatches(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, sku TEXT,
    hub TEXT, system_qty INTEGER, physical_qty INTEGER);
CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, customer TEXT, sku TEXT, qty INTEGER, hub TEXT,
    amount REAL, status TEXT, tracking TEXT, gateway TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS order_events(id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT, ts REAL,
    status TEXT, note TEXT);
CREATE INDEX IF NOT EXISTS ix_oe ON order_events(order_id);
CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, outcome TEXT,
    latency_ms REAL, sale INTEGER);
CREATE TABLE IF NOT EXISTS availability(ts REAL, ok INTEGER);
CREATE TABLE IF NOT EXISTS alerts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, check_name TEXT,
    severity TEXT, message TEXT, incident_id INTEGER, detection_s REAL);
CREATE TABLE IF NOT EXISTS incidents(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, description TEXT,
    priority TEXT, status TEXT, source TEXT, level TEXT, created_at REAL, acknowledged_at REAL,
    resolved_at REAL, sla_due REAL, resolution TEXT, problem_id INTEGER, auto INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS incident_log(id INTEGER PRIMARY KEY AUTOINCREMENT, incident_id INTEGER,
    ts REAL, note TEXT);
CREATE TABLE IF NOT EXISTS problems(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, source TEXT,
    status TEXT, root_cause TEXT, workaround TEXT, created_at REAL, resolved_at REAL);
CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, description TEXT,
    risk TEXT, type TEXT, version TEXT, status TEXT, rollout_pct INTEGER DEFAULT 0, requester TEXT,
    approver TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS change_log(id INTEGER PRIMARY KEY AUTOINCREMENT, change_id INTEGER, ts REAL,
    note TEXT);
CREATE TABLE IF NOT EXISTS kb(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, body TEXT, tags TEXT,
    created_at REAL);
CREATE TABLE IF NOT EXISTS csat(order_id TEXT PRIMARY KEY, rating INTEGER, ts REAL);
"""


class DB:
    """Thin thread-safe SQLite wrapper (one connection, one re-entrant lock)."""

    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=OFF")
        self.conn.executescript(SCHEMA)

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    def kv_get(self, k, default=None):
        r = self.one("SELECT v FROM kv WHERE k=?", (k,))
        return r["v"] if r else default

    def kv_set(self, k, v):
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))

    def kv_incr(self, k, n=1):
        self.x("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=CAST(v AS INTEGER)+?",
               (k, str(n), n))

    def kv_json(self, k, default=None):
        v = self.kv_get(k)
        return json.loads(v) if v else default
