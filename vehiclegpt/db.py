"""SQLite storage layer: schema, connection helpers, and typed read functions."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pandas as pd

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    vehicle_type TEXT NOT NULL,
    usable_battery_kwh REAL NOT NULL,
    nominal_battery_kwh REAL NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 1,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS units (
    column_name TEXT PRIMARY KEY,
    unit TEXT NOT NULL,
    description TEXT
);
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY,
    vehicle_id TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    ts TEXT NOT NULL,
    trip_id TEXT,
    state TEXT NOT NULL,            -- driving | charging | parked
    speed_kmh REAL,
    soc_pct REAL,
    battery_temp_c REAL,
    motor_temp_c REAL,
    ambient_temp_c REAL,
    power_kw REAL,                  -- + discharge, - charge
    distance_km REAL,               -- distance in this interval
    energy_kwh REAL,                -- traction+HVAC energy used in this interval
    odometer_km REAL,
    tire_pressure_kpa REAL,
    valid INTEGER NOT NULL DEFAULT 1,
    validity_note TEXT,
    source_ref TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_tel_ts ON telemetry(vehicle_id, ts);
CREATE INDEX IF NOT EXISTS idx_tel_trip ON telemetry(trip_id);

CREATE TABLE IF NOT EXISTS diagnostic_events (
    id INTEGER PRIMARY KEY,
    vehicle_id TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    ts TEXT NOT NULL,
    code TEXT NOT NULL,             -- SIM-* codes are synthetic, not real OEM/OBD codes
    subsystem TEXT NOT NULL,
    description TEXT NOT NULL,
    reported_level TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_dtc_ts ON diagnostic_events(vehicle_id, ts);
CREATE INDEX IF NOT EXISTS idx_dtc_code ON diagnostic_events(code);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    ts_end TEXT,
    subsystem TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    metric TEXT,
    observed_value REAL,
    threshold REAL,
    baseline REAL,
    anomaly_score REAL,
    message TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    next_step TEXT NOT NULL,
    acknowledged INTEGER NOT NULL DEFAULT 0,
    ack_ts TEXT,
    fingerprint TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_alert_ts ON alerts(ts);

CREATE TABLE IF NOT EXISTS simulation_runs (
    id INTEGER PRIMARY KEY,
    created_ts TEXT NOT NULL,
    scenario TEXT NOT NULL,
    baseline_json TEXT NOT NULL,
    params_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT 'HYPOTHETICAL ESTIMATE'
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# v2 additions (applied by migrations.py; safe on an existing v1 database)
SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('driver','engineer','admin')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
    must_change_password INTEGER NOT NULL DEFAULT 0,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_vehicles (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    vehicle_id TEXT NOT NULL REFERENCES vehicles(vehicle_id) ON DELETE CASCADE,
    access TEXT NOT NULL CHECK (access IN ('owner','engineer')),
    granted_by INTEGER REFERENCES users(id),
    granted_at TEXT NOT NULL,
    PRIMARY KEY (user_id, vehicle_id)
);
CREATE INDEX IF NOT EXISTS idx_uv_vehicle ON user_vehicles(vehicle_id);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS password_resets (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS maintenance_reminders (
    id INTEGER PRIMARY KEY,
    vehicle_id TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    category TEXT NOT NULL,
    due_date TEXT,
    due_odometer_km REAL,
    interval_days INTEGER,
    interval_km REAL,
    notes TEXT,
    source TEXT NOT NULL CHECK (source IN ('user','generic_suggestion')),
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done')),
    completed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_maint_vehicle ON maintenance_reminders(vehicle_id, user_id);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY,
    vehicle_id TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','investigating','resolved','closed')),
    priority TEXT NOT NULL DEFAULT 'Medium',
    event_key TEXT,
    summary TEXT,
    resolution TEXT,
    root_cause_status TEXT NOT NULL DEFAULT 'none' CHECK (root_cause_status IN ('none','candidate','confirmed')),
    created_by INTEGER NOT NULL REFERENCES users(id),
    assigned_to INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cases_vehicle ON cases(vehicle_id, status);
CREATE TABLE IF NOT EXISTS case_notes (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS case_hypotheses (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    evidence_strength TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (status IN ('candidate','supported','rejected','confirmed')),
    reviewed_by INTEGER REFERENCES users(id),
    review_note TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    user_id INTEGER,
    action TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    user_id INTEGER,
    kind TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    conditions_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS adas_events (
    id INTEGER PRIMARY KEY,
    vehicle_id TEXT NOT NULL REFERENCES vehicles(vehicle_id),
    ts TEXT NOT NULL,
    feature TEXT NOT NULL,          -- ACC | LKA | front_camera
    event TEXT NOT NULL,            -- disengagement | camera_blocked | ...
    reason TEXT,
    speed_kmh REAL,
    duration_s INTEGER,
    source_ref TEXT NOT NULL,
    synthetic INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_adas_ts ON adas_events(vehicle_id, ts);
CREATE TABLE IF NOT EXISTS ingest_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    source TEXT NOT NULL,           -- can | someip | vhal | qnx_slog | dlt | adas | camera_meta
    vehicle_id TEXT,
    frames INTEGER NOT NULL,
    rows_written INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS telemetry_rollup_hourly (
    vehicle_id TEXT NOT NULL,
    hour TEXT NOT NULL,
    samples INTEGER, driving_samples INTEGER, distance_km REAL, energy_kwh REAL,
    avg_speed_kmh REAL, max_battery_temp_c REAL, max_motor_temp_c REAL, avg_ambient_c REAL, min_soc_pct REAL,
    PRIMARY KEY (vehicle_id, hour)
);
CREATE TABLE IF NOT EXISTS data_transformations (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    vehicle_id TEXT,
    record_ids TEXT NOT NULL,
    description TEXT NOT NULL,
    user_id INTEGER
);
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or config.DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def session(path: Path | str | None = None):
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    from . import migrations
    migrations.migrate(conn)


def vehicle_ids(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT vehicle_id FROM vehicles ORDER BY vehicle_id")]


def is_initialized(path: Path | str | None = None) -> bool:
    p = Path(path or config.DB_PATH)
    if not p.exists():
        return False
    with session(p) as c:
        try:
            return c.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] > 0
        except sqlite3.OperationalError:
            return False


# ---------- readers ----------

def read_telemetry(conn, start: str | None = None, end: str | None = None,
                   vehicle_id: str = config.VEHICLE_ID, include_invalid: bool = True) -> pd.DataFrame:
    q = "SELECT * FROM telemetry WHERE vehicle_id = ?"
    args: list = [vehicle_id]
    if start:
        q += " AND ts >= ?"; args.append(start)
    if end:
        q += " AND ts < ?"; args.append(end)
    if not include_invalid:
        q += " AND valid = 1"
    q += " ORDER BY ts"
    df = pd.read_sql_query(q, conn, params=args)
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def read_dtcs(conn, start: str | None = None, end: str | None = None,
              vehicle_id: str = config.VEHICLE_ID) -> pd.DataFrame:
    q = "SELECT * FROM diagnostic_events WHERE vehicle_id = ?"
    args: list = [vehicle_id]
    if start:
        q += " AND ts >= ?"; args.append(start)
    if end:
        q += " AND ts < ?"; args.append(end)
    df = pd.read_sql_query(q + " ORDER BY ts", conn, params=args)
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def read_alerts(conn, start: str | None = None, end: str | None = None,
                vehicle_id: str | None = config.VEHICLE_ID) -> pd.DataFrame:
    q = "SELECT * FROM alerts WHERE 1=1"
    args: list = []
    if vehicle_id is not None:
        q += " AND vehicle_id = ?"; args.append(vehicle_id)
    if start:
        q += " AND ts >= ?"; args.append(start)
    if end:
        q += " AND ts < ?"; args.append(end)
    df = pd.read_sql_query(q + " ORDER BY ts", conn, params=args)
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def read_adas(conn, start: str | None = None, end: str | None = None, vehicle_id: str = config.VEHICLE_ID) -> pd.DataFrame:
    q = "SELECT * FROM adas_events WHERE vehicle_id = ?"
    args: list = [vehicle_id]
    if start:
        q += " AND ts >= ?"; args.append(start)
    if end:
        q += " AND ts < ?"; args.append(end)
    df = pd.read_sql_query(q + " ORDER BY ts", conn, params=args)
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def get_vehicle(conn, vehicle_id: str = config.VEHICLE_ID) -> dict:
    row = conn.execute("SELECT * FROM vehicles WHERE vehicle_id = ?", (vehicle_id,)).fetchone()
    return dict(row) if row else {}


def data_bounds(conn, vehicle_id: str = config.VEHICLE_ID) -> tuple[pd.Timestamp, pd.Timestamp]:
    lo, hi = conn.execute("SELECT MIN(ts), MAX(ts) FROM telemetry WHERE vehicle_id = ?", (vehicle_id,)).fetchone()
    return pd.Timestamp(lo), pd.Timestamp(hi)


def get_thresholds(conn) -> dict[str, float]:
    th = dict(config.DEFAULT_THRESHOLDS)
    row = conn.execute("SELECT value FROM settings WHERE key = 'thresholds'").fetchone()
    if row:
        th.update(json.loads(row[0]))
    return th


def set_thresholds(conn, th: dict[str, float]) -> None:
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('thresholds', ?)", (json.dumps(th),))
    conn.commit()


def units(conn) -> dict[str, str]:
    return {r["column_name"]: r["unit"] for r in conn.execute("SELECT * FROM units")}
