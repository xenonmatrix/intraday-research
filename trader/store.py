import csv
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import uuid
from contextlib import contextmanager
from .models import Unsafe

HEADER = "timestamp,ticker,side,quantity,entry_price,exit_price,position_value,setup,stop_condition,target_condition,realized_pnl,account_value".split(",")


def atomic_text(path, text):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    with open(temp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


@contextmanager
def process_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise Unsafe("Another monitoring process owns this project") from e
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS intents(
              intent_key TEXT PRIMARY KEY, ref_id TEXT UNIQUE NOT NULL,
              mode TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
              broker_id TEXT UNIQUE, filled_quantity TEXT NOT NULL DEFAULT '0');
            CREATE TABLE IF NOT EXISTS fills(
              execution_id TEXT PRIMARY KEY, ref_id TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
        """)
        self.db.commit()

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, default=str)))

    def save(self, key, value):
        with self.db:
            self.put(key, value)

    def event(self, timestamp, kind, payload):
        with self.db:
            self.db.execute("INSERT INTO events(timestamp,kind,payload) VALUES(?,?,?)", (timestamp, kind, json.dumps(payload, default=str)))

    def intent(self, key, mode, payload):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO intents(intent_key,ref_id,mode,status,payload) VALUES(?,?,?,?,?)", (key, str(uuid.uuid4()), mode, "PREPARED", json.dumps(payload, default=str)))
        row = self.db.execute("SELECT * FROM intents WHERE intent_key=?", (key,)).fetchone()
        if json.loads(row["payload"]) != json.loads(json.dumps(payload, default=str)) or row["mode"] != mode:
            raise Unsafe("Intent key reused with different parameters")
        return dict(row)

    def unresolved(self):
        return list(self.db.execute("SELECT * FROM intents WHERE status NOT IN ('FILLED','CANCELLED','REJECTED','ABORTED')"))

    def recover(self):
        # PAPER commits simulated fill + position + cash atomically. PREPARED means
        # no fill happened. Abort it rather than execute an old signal on restart.
        with self.db:
            self.db.execute("UPDATE intents SET status='ABORTED' WHERE mode='PAPER' AND status='PREPARED'")
        return self.unresolved()

    def rows(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT payload FROM fills ORDER BY rowid")]

    def export_csv(self, path):
        # Materialized view: regeneration is safe after a crash, with no duplicate rows.
        import io
        buf = io.StringIO()
        writer = csv.DictWriter(buf, HEADER)
        writer.writeheader()
        for row in self.rows():
            writer.writerow({k: row.get(k, "") for k in HEADER})
        atomic_text(path, buf.getvalue())

    def close(self):
        self.db.close()
