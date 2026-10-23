"""SQLite storage for print jobs, templates and runtime settings."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    task_id INTEGER,  -- unused; kept so older databases still match
    rows INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""

class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            # Jobs that were mid-flight when the server stopped never finished.
            self._conn.execute(
                "UPDATE jobs SET status='failed', error='server restarted' WHERE status IN ('queued','printing')"
            )
            self._conn.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _all(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    # ---- jobs --------------------------------------------------------------

    def create_job(self, kind: str, title: str, rows: int) -> dict:
        cur = self._exec(
            "INSERT INTO jobs (kind, title, status, rows, created_at) VALUES (?,?,?,?,?)",
            (kind, title[:200], "queued", rows, time.time()),
        )
        return self.get_job(cur.lastrowid)

    def get_job(self, job_id: int) -> dict | None:
        rows = self._all("SELECT * FROM jobs WHERE id=?", (job_id,))
        return dict(rows[0]) if rows else None

    def update_job(self, job_id: int, **values: Any) -> dict | None:
        if values:
            sets = ", ".join(f"{k}=?" for k in values)
            self._exec(f"UPDATE jobs SET {sets} WHERE id=?", (*values.values(), job_id))
        return self.get_job(job_id)

    def list_jobs(self, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self._all("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]

    def old_job_ids(self, keep: int) -> list[int]:
        rows = self._all("SELECT id FROM jobs ORDER BY id DESC LIMIT -1 OFFSET ?", (keep,))
        return [r["id"] for r in rows]

    def delete_jobs(self, ids: list[int]) -> None:
        for job_id in ids:
            self._exec("DELETE FROM jobs WHERE id=?", (job_id,))

    # ---- templates ---------------------------------------------------------

    @staticmethod
    def _template(row: sqlite3.Row) -> dict:
        return {**dict(row), "payload": json.loads(row["payload"])}

    def list_templates(self) -> list[dict]:
        return [self._template(r) for r in self._all("SELECT * FROM templates ORDER BY name COLLATE NOCASE")]

    def get_template(self, template_id: int) -> dict | None:
        rows = self._all("SELECT * FROM templates WHERE id=?", (template_id,))
        return self._template(rows[0]) if rows else None

    def create_template(self, name: str, kind: str, payload: dict) -> dict:
        cur = self._exec(
            "INSERT INTO templates (name, kind, payload, created_at) VALUES (?,?,?,?)",
            (name[:100], kind, json.dumps(payload), time.time()),
        )
        return self.get_template(cur.lastrowid)

    def delete_template(self, template_id: int) -> bool:
        return self._exec("DELETE FROM templates WHERE id=?", (template_id,)).rowcount > 0

    # ---- settings ----------------------------------------------------------

    def get_settings(self) -> dict:
        return {r["key"]: json.loads(r["value"]) for r in self._all("SELECT key, value FROM settings")}

    def set_settings(self, values: dict) -> None:
        for key, value in values.items():
            self._exec(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
