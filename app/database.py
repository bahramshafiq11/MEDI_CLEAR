"""
Saves consultations in a small SQLite database file (data/mediclear.db).
SQLite comes with Python, so there is nothing extra to install.
"""
import json
import sqlite3
from datetime import datetime

from . import config


class _Conn(sqlite3.Connection):
    """Like a normal connection, but `with` also closes it (and commits)."""
    def __exit__(self, *exc):
        result = super().__exit__(*exc)
        self.close()
        return result


def _connect():
    conn = sqlite3.connect(config.DB_PATH, factory=_Conn)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS consultations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                patient_name TEXT,
                source TEXT,
                status TEXT NOT NULL DEFAULT 'draft',
                approved_at TEXT,
                result_json TEXT NOT NULL
            )
        """)


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["result"] = json.loads(d.pop("result_json"))
    return d


def save_new(patient_name: str, source: str, result: dict) -> dict:
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO consultations (created_at, patient_name, source, status, result_json) VALUES (?, ?, ?, 'draft', ?)",
            (now, patient_name, source, json.dumps(result)),
        )
        new_id = cur.lastrowid
    return get(new_id)  # read it back after the save is committed


def get(cid: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM consultations WHERE id = ?", (cid,)).fetchone()
        return _row_to_dict(row) if row else None


def list_all() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, patient_name, source, status FROM consultations ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]


def update_result(cid: int, result: dict, patient_name: str | None = None):
    with _connect() as conn:
        if patient_name is not None:
            conn.execute("UPDATE consultations SET result_json = ?, patient_name = ? WHERE id = ?",
                         (json.dumps(result), patient_name, cid))
        else:
            conn.execute("UPDATE consultations SET result_json = ? WHERE id = ?", (json.dumps(result), cid))


def set_status(cid: int, status: str):
    now = datetime.now().isoformat(timespec="seconds") if status == "approved" else None
    with _connect() as conn:
        conn.execute("UPDATE consultations SET status = ?, approved_at = ? WHERE id = ?", (status, now, cid))


def delete(cid: int):
    with _connect() as conn:
        conn.execute("DELETE FROM consultations WHERE id = ?", (cid,))
