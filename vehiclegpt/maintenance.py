"""Maintenance planner. All schedules are USER-ENTERED or GENERIC SUGGESTIONS — never manufacturer schedules
(no manufacturer data is available in this prototype)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd

from . import analytics, db

GENERIC_SUGGESTIONS = [
    {"title": "Check tyre pressures", "category": "tyres", "interval_days": 30, "interval_km": None},
    {"title": "Rotate tyres", "category": "tyres", "interval_days": None, "interval_km": 10000.0},
    {"title": "Replace cabin air filter", "category": "hvac", "interval_days": 365, "interval_km": None},
    {"title": "Inspect brake fluid", "category": "brakes", "interval_days": 730, "interval_km": None},
]
CATEGORIES = ["tyres", "brakes", "hvac", "battery", "cooling", "inspection", "other"]


def latest_odometer(conn, vehicle_id: str) -> float | None:
    r = conn.execute("SELECT odometer_km FROM telemetry WHERE vehicle_id = ? AND odometer_km IS NOT NULL ORDER BY ts DESC LIMIT 1",
                     (vehicle_id,)).fetchone()
    return float(r[0]) if r else None


def add(conn, vehicle_id: str, user_id: int, title: str, category: str, due_date: str | None = None,
        due_odometer_km: float | None = None, interval_days: int | None = None, interval_km: float | None = None,
        notes: str = "", source: str = "user") -> int:
    title = (title or "").strip()
    if not title or len(title) > 120:
        raise ValueError("Title is required (max 120 characters).")
    if category not in CATEGORIES:
        raise ValueError("Unknown category.")
    if not due_date and due_odometer_km is None:
        raise ValueError("Set a due date and/or a due odometer reading.")
    if due_date:
        datetime.fromisoformat(due_date)
    for v in (due_odometer_km, interval_km):
        if v is not None and not 0 <= float(v) <= 2_000_000:
            raise ValueError("Odometer values must be between 0 and 2,000,000 km.")
    if interval_days is not None and not 1 <= int(interval_days) <= 3650:
        raise ValueError("Interval days must be 1–3650.")
    if source not in ("user", "generic_suggestion"):
        raise ValueError("Invalid source.")
    cur = conn.execute(
        "INSERT INTO maintenance_reminders (vehicle_id, user_id, title, category, due_date, due_odometer_km, interval_days,"
        " interval_km, notes, source, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'open', ?)",
        (vehicle_id, user_id, title, category, due_date, due_odometer_km, interval_days, interval_km, (notes or "")[:1000],
         source, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    return cur.lastrowid


def add_suggestion(conn, vehicle_id: str, user_id: int, idx: int) -> int:
    s = GENERIC_SUGGESTIONS[idx]
    now = analytics.reference_now(conn)
    odo = latest_odometer(conn, vehicle_id)
    due_date = (now + timedelta(days=s["interval_days"])).strftime("%Y-%m-%d") if s["interval_days"] else None
    due_km = round(odo + s["interval_km"]) if s["interval_km"] and odo else None
    return add(conn, vehicle_id, user_id, s["title"], s["category"], due_date, due_km, s["interval_days"], s["interval_km"],
               "Generic suggestion — not a manufacturer-recommended interval.", "generic_suggestion")


def get(conn, rid: int):
    return conn.execute("SELECT * FROM maintenance_reminders WHERE id = ?", (rid,)).fetchone()


def list_for(conn, vehicle_id: str, user_id: int) -> pd.DataFrame:
    df = pd.read_sql_query("SELECT * FROM maintenance_reminders WHERE vehicle_id = ? AND user_id = ? ORDER BY status, due_date",
                           conn, params=(vehicle_id, user_id))
    now = analytics.reference_now(conn)
    odo = latest_odometer(conn, vehicle_id)

    def due_state(r):
        if r["status"] == "done":
            return "done"
        if r["due_date"] and pd.Timestamp(r["due_date"]) < now:
            return "overdue"
        if r["due_odometer_km"] and odo and odo >= r["due_odometer_km"]:
            return "overdue"
        if r["due_date"] and pd.Timestamp(r["due_date"]) < now + pd.Timedelta(days=14):
            return "due soon"
        if r["due_odometer_km"] and odo and odo >= r["due_odometer_km"] - 500:
            return "due soon"
        return "scheduled"
    df["due_state"] = df.apply(due_state, axis=1) if len(df) else []
    return df


def complete(conn, rid: int) -> int | None:
    """Mark done; if an interval is set, schedule the next occurrence. Returns new id or None."""
    r = get(conn, rid)
    conn.execute("UPDATE maintenance_reminders SET status = 'done', completed_at = ? WHERE id = ?",
                 (datetime.now().isoformat(timespec="seconds"), rid))
    conn.commit()
    if r["interval_days"] or r["interval_km"]:
        now = analytics.reference_now(conn)
        odo = latest_odometer(conn, r["vehicle_id"])
        return add(conn, r["vehicle_id"], r["user_id"], r["title"], r["category"],
                   (now + timedelta(days=r["interval_days"])).strftime("%Y-%m-%d") if r["interval_days"] else None,
                   round(odo + r["interval_km"]) if r["interval_km"] and odo else None,
                   r["interval_days"], r["interval_km"], r["notes"] or "", r["source"])
    return None


def delete(conn, rid: int) -> None:
    conn.execute("DELETE FROM maintenance_reminders WHERE id = ?", (rid,))
    conn.commit()
