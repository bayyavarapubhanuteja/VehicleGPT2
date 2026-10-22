"""Historical retention: hot 10-minute rows + cold hourly roll-ups.

Policy (configurable): keep raw 10-minute telemetry for RAW_DAYS (90 d ≈ 13 weeks), keep hourly roll-ups forever.
Pruning is explicit (never automatic), first writes roll-ups, and is recorded in `data_transformations`.
Storage budget is estimated from the actual database so the 'several weeks searchable' requirement can be checked.
"""
from __future__ import annotations

import json
import os
from datetime import datetime

import pandas as pd

from .. import config, db

RAW_DAYS = 90


def rollup_hourly(conn, vehicle_id: str) -> int:
    conn.execute("DELETE FROM telemetry_rollup_hourly WHERE vehicle_id = ?", (vehicle_id,))
    conn.execute("""
        INSERT INTO telemetry_rollup_hourly
        SELECT vehicle_id, substr(ts, 1, 13) || ':00:00' AS hour, COUNT(*), SUM(state = 'driving'),
               SUM(distance_km), SUM(energy_kwh), AVG(CASE WHEN state = 'driving' THEN speed_kmh END),
               MAX(battery_temp_c), MAX(motor_temp_c), AVG(ambient_temp_c), MIN(soc_pct)
        FROM telemetry WHERE vehicle_id = ? AND valid = 1 GROUP BY vehicle_id, hour""", (vehicle_id,))
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM telemetry_rollup_hourly WHERE vehicle_id = ?", (vehicle_id,)).fetchone()[0]


def apply_retention(conn, vehicle_id: str, now: datetime | None = None, raw_days: int = RAW_DAYS,
                    dry_run: bool = True, user_id: int | None = None) -> dict:
    now = pd.Timestamp(now) if now is not None else pd.Timestamp(conn.execute("SELECT MAX(ts) FROM telemetry").fetchone()[0])
    cutoff = (now - pd.Timedelta(days=raw_days)).isoformat()
    n = conn.execute("SELECT COUNT(*) FROM telemetry WHERE vehicle_id = ? AND ts < ?", (vehicle_id, cutoff)).fetchone()[0]
    res = {"vehicle_id": vehicle_id, "cutoff": cutoff, "rows_older_than_cutoff": n, "dry_run": dry_run}
    if not dry_run and n:
        rollup_hourly(conn, vehicle_id)
        ids = [r[0] for r in conn.execute("SELECT id FROM telemetry WHERE vehicle_id = ? AND ts < ?", (vehicle_id, cutoff))]
        conn.execute("DELETE FROM telemetry WHERE vehicle_id = ? AND ts < ?", (vehicle_id, cutoff))
        conn.execute("INSERT INTO data_transformations (ts, vehicle_id, record_ids, description, user_id) VALUES (?,?,?,?,?)",
                     (datetime.now().isoformat(timespec="seconds"), vehicle_id, json.dumps([ids[0], ids[-1], len(ids)]),
                      f"Retention: {len(ids)} raw rows older than {cutoff} moved to hourly roll-ups", user_id))
        conn.commit()
    return res


def storage_estimate(conn) -> dict:
    size = os.path.getsize(config.DB_PATH) if os.path.exists(config.DB_PATH) else None
    rows = conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]
    vdays = conn.execute("SELECT SUM(d) FROM (SELECT COUNT(DISTINCT substr(ts,1,10)) AS d FROM telemetry GROUP BY vehicle_id)").fetchone()[0] or 1
    lo, hi = conn.execute("SELECT MIN(ts), MAX(ts) FROM telemetry").fetchone()
    per_day = size / vdays if size else None
    return {"db_bytes": size, "telemetry_rows": rows, "vehicle_days": vdays, "history_from": lo, "history_to": hi,
            "history_weeks": round((pd.Timestamp(hi) - pd.Timestamp(lo)).days / 7, 1) if lo else 0,
            "bytes_per_vehicle_day": round(per_day) if per_day else None,
            "projected_mb_for_raw_retention": round(per_day * RAW_DAYS / 1e6, 1) if per_day else None,
            "projected_mb_per_year_rollups": round(per_day / 6 * 365 / 1e6, 1) if per_day else None}
