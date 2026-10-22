"""Idempotent, non-destructive schema migrations (v1 -> v2). Existing rows are never deleted."""
from __future__ import annotations

from datetime import datetime

from . import db

CURRENT_VERSION = 2


def _cols(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_col(conn, table, col, decl):
    if col not in _cols(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def version(conn) -> int:
    try:
        r = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        return r[0] or 1
    except Exception:  # noqa: BLE001
        return 1


def migrate(conn) -> int:
    conn.executescript(db.SCHEMA)  # v1 tables (IF NOT EXISTS)
    conn.executescript(db.SCHEMA_V2)
    _add_col(conn, "vehicles", "nickname", "TEXT")
    _add_col(conn, "vehicles", "vin", "TEXT")
    _add_col(conn, "vehicles", "pairing_code_hash", "TEXT")
    _add_col(conn, "vehicles", "data_mode", "TEXT NOT NULL DEFAULT 'historical-synthetic'")
    _add_col(conn, "alerts", "rule_version", "TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alert_vehicle ON alerts(vehicle_id, ts)")
    if version(conn) < CURRENT_VERSION:
        conn.execute("INSERT OR IGNORE INTO schema_version VALUES (?, ?)",
                     (CURRENT_VERSION, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return version(conn)
