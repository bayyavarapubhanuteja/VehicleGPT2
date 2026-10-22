"""Authorised service layer — the ONLY entry point the UI uses for data access.

Every function takes the authenticated `user` (re-loaded from the DB for each request by
auth.validate_session) and enforces role and vehicle-assignment checks server-side before touching
data. UI state (page names, session_state values, query parameters) is never trusted for authorisation.
"""
from __future__ import annotations

import json

import pandas as pd

from . import (analytics, anomaly, audit, auth, cases, config, dataquality, db, detective, health, maintenance,
               simulator)
from . import query_engine as qe
from .data_gen import hash_code


class PermissionDenied(Exception):
    pass


# ---------------- primitives ----------------

def _fresh(conn, user: dict) -> dict:
    """Re-read the user from the DB so a stale/tampered dict cannot carry a different role."""
    if not user or "id" not in user:
        raise PermissionDenied("Not authenticated.")
    r = conn.execute("SELECT * FROM users WHERE id = ? AND status = 'active'", (int(user["id"]),)).fetchone()
    if not r:
        raise PermissionDenied("Account not active.")
    return auth.public_user(r)


def require_role(conn, user: dict, *roles: str) -> dict:
    u = _fresh(conn, user)
    if u["role"] not in roles:
        audit.log(conn, u["id"], "authz.denied", "role", None, f"needed={roles} had={u['role']}")
        raise PermissionDenied("You do not have permission for this action.")
    return u


def vehicles_for(conn, user: dict) -> list[str]:
    u = _fresh(conn, user)
    access = "owner" if u["role"] == "driver" else "engineer"
    if u["role"] == "admin":
        return []  # admins manage accounts; they do not read vehicle data
    return [r[0] for r in conn.execute("SELECT vehicle_id FROM user_vehicles WHERE user_id = ? AND access = ? ORDER BY vehicle_id",
                                       (u["id"], access))]


def require_vehicle(conn, user: dict, vehicle_id: str) -> dict:
    u = _fresh(conn, user)
    if vehicle_id not in vehicles_for(conn, u):
        audit.log(conn, u["id"], "authz.denied", "vehicle", vehicle_id, "")
        raise PermissionDenied("You are not authorised to access this vehicle.")
    return u


def vehicle_list(conn, user: dict) -> pd.DataFrame:
    vids = vehicles_for(conn, user)
    if not vids:
        return pd.DataFrame(columns=["vehicle_id", "nickname", "name", "vehicle_type", "vin", "data_mode"])
    return pd.read_sql_query(
        f"SELECT vehicle_id, nickname, name, vehicle_type, vin, usable_battery_kwh, data_mode FROM vehicles "
        f"WHERE vehicle_id IN ({','.join('?' * len(vids))}) ORDER BY vehicle_id", conn, params=vids)


# ---------------- shared reads (owner or assigned engineer) ----------------

def overview(conn, user, vid) -> dict:
    require_vehicle(conn, user, vid)
    lo, hi = db.data_bounds(conn, vid)
    tel = db.read_telemetry(conn, (hi - pd.Timedelta(hours=48)).isoformat(), None, vid, include_invalid=False)
    last = tel.iloc[-1] if len(tel) else None
    soc = tel["soc_pct"].dropna()
    wk = analytics.period_stats(conn, analytics.last_n_days(conn, 7), vehicle_id=vid)
    v = db.get_vehicle(conn, vid)
    rng = (soc.iloc[-1] / 100 * v["usable_battery_kwh"] / (wk.consumption_kwh_per_100km / 100)
           if len(soc) and wk.consumption_kwh_per_100km else None)
    st = dataquality.staleness(conn, vid)
    return {"vehicle": v, "first_ts": lo, "last_ts": hi, "telemetry_48h": tel, "last": last,
            "soc": float(soc.iloc[-1]) if len(soc) else None, "soc_ts": tel.loc[soc.index[-1], "ts"] if len(soc) else None,
            "est_range_km": rng, "week": wk, "staleness": st,
            "data_status": "historical (synthetic)" + (" — STALE" if st["stale"] else "")}


def alerts(conn, user, vid, start=None, end=None) -> pd.DataFrame:
    require_vehicle(conn, user, vid)
    return db.read_alerts(conn, start, end, vid)


def acknowledge_alert(conn, user, alert_id: int, ack: bool = True) -> None:
    r = conn.execute("SELECT vehicle_id FROM alerts WHERE id = ?", (int(alert_id),)).fetchone()
    if not r:
        raise PermissionDenied("Alert not found.")
    u = require_vehicle(conn, user, r[0])
    anomaly.acknowledge(conn, int(alert_id), ack)
    audit.log(conn, u["id"], "alert.ack" if ack else "alert.unack", "alert", alert_id, r[0])


def dtcs(conn, user, vid, start=None, end=None) -> pd.DataFrame:
    require_vehicle(conn, user, vid)
    return db.read_dtcs(conn, start, end, vid)


def telemetry(conn, user, vid, start=None, end=None) -> pd.DataFrame:
    require_vehicle(conn, user, vid)
    return db.read_telemetry(conn, start, end, vid)


def records_by_id(conn, user, vid, table: str, ids: list[int]) -> pd.DataFrame:
    require_vehicle(conn, user, vid)
    if table not in ("telemetry", "diagnostic_events") or not ids:
        return pd.DataFrame()
    ids = [int(i) for i in ids][:500]
    return pd.read_sql_query(f"SELECT * FROM {table} WHERE vehicle_id = ? AND id IN ({','.join('?' * len(ids))}) ORDER BY ts",
                             conn, params=[vid, *ids])


def llm_available() -> bool:
    from . import llm
    return llm.get_adapter().status().available


def explain_answer(conn, user, a: qe.Answer) -> qe.Answer:
    """On-demand local-LLM rephrasing of an already-authorised answer (re-checks vehicle access)."""
    require_vehicle(conn, user, a.vehicle_id)
    return qe.add_explanation(a)


def ingest_replay(conn, user, source_vid: str, start: str, hours: float) -> dict:
    from .ingest import replay
    u = eng(conn, user)
    require_vehicle(conn, u, source_vid)
    hours = max(0.5, min(float(hours), 24))
    res = replay.run(conn, source_vid, start, hours)
    if replay.BENCH_ID not in vehicles_for(conn, u):
        conn.execute("INSERT OR IGNORE INTO user_vehicles VALUES (?,?, 'engineer', ?, datetime('now'))", (u["id"], replay.BENCH_ID, u["id"]))
        conn.commit()
    audit.log(conn, u["id"], "ingest.replay", "vehicle", replay.BENCH_ID, f"source={source_vid} start={start} hours={hours}")
    return res


def ingest_status(conn, user) -> dict:
    from .ingest import retention
    eng(conn, user)
    return {"log": pd.read_sql_query("SELECT * FROM ingest_log ORDER BY id DESC LIMIT 100", conn),
            "storage": retention.storage_estimate(conn)}


def retention_preview(conn, user, vid, raw_days: int) -> dict:
    from .ingest import retention
    eng(conn, user)
    require_vehicle(conn, user, vid)
    return retention.apply_retention(conn, vid, raw_days=int(raw_days), dry_run=True)


def ask(conn, user, vid, question: str, use_llm: bool = True, context_event: str | None = None) -> qe.Answer:
    u = require_vehicle(conn, user, vid)
    if context_event:
        _check_event(conn, u, context_event, vid)
    a = qe.answer(conn, question, use_llm=use_llm, context_event=context_event, vehicle_id=vid,
                  allowed_vehicles=vehicles_for(conn, u))
    audit.log(conn, u["id"], "query", "vehicle", vid, f"intent={a.intent}")
    return a


def _check_event(conn, u, key: str, vid: str | None = None) -> str:
    try:
        ev_vid = detective.event_vehicle(conn, key)
    except (KeyError, ValueError):
        raise PermissionDenied("Event not found.")
    if ev_vid not in vehicles_for(conn, u) or (vid and ev_vid != vid):
        audit.log(conn, u["id"], "authz.denied", "event", key, "")
        raise PermissionDenied("You are not authorised to access this event.")
    return ev_vid


def events(conn, user, vid) -> pd.DataFrame:
    require_vehicle(conn, user, vid)
    return detective.list_events(conn, vid)


def investigate(conn, user, key: str, before_min: int = 120, after_min: int = 60) -> dict:
    u = _fresh(conn, user)
    _check_event(conn, u, key)
    before_min, after_min = max(1, min(int(before_min), 1440)), max(1, min(int(after_min), 1440))
    return detective.investigate(conn, key, before_min, after_min)


def health_score(conn, user, vid, as_of=None) -> dict:
    require_vehicle(conn, user, vid)
    return health.compute(conn, vid, as_of)


def health_trend(conn, user, vid) -> pd.DataFrame:
    require_vehicle(conn, user, vid)
    return health.trend(conn, vid)


def efficiency(conn, user, vid) -> dict:
    require_vehicle(conn, user, vid)
    dc = analytics.daily_consumption(conn, vehicle_id=vid)
    tel = db.read_telemetry(conn, vehicle_id=vid)
    drv = tel[tel["state"] == "driving"]
    trips = drv.groupby("trip_id").agg(start=("ts", "min"), end=("ts", "max"), distance_km=("distance_km", "sum"),
                                       energy_kwh=("energy_kwh", "sum"), avg_speed=("speed_kmh", "mean"),
                                       avg_ambient=("ambient_temp_c", "mean")).reset_index()
    trips["kwh_per_100km"] = trips["energy_kwh"] / trips["distance_km"] * 100
    trips["end"] = trips["end"] + pd.Timedelta(minutes=config.SAMPLE_MINUTES)
    cmp = analytics.compare_periods(conn, analytics.last_n_days(conn, 7, 7, "previous 7 days"),
                                    analytics.last_n_days(conn, 7), vid)
    return {"daily": dc, "trips": trips.sort_values("start", ascending=False), "comparison": cmp}


def simulate(conn, user, vid, period, params: simulator.Params) -> tuple[dict, dict]:
    require_vehicle(conn, user, vid)
    base = simulator.baseline(conn, period, vid)
    return base, simulator.simulate(base, params)


def save_simulation(conn, user, vid, name: str, base, params, result) -> int:
    u = require_vehicle(conn, user, vid)
    if base.get("vehicle_id") != vid:
        raise PermissionDenied("Baseline does not belong to this vehicle.")
    rid = simulator.save_run(conn, (name or "scenario")[:80], base, params, result)
    audit.log(conn, u["id"], "simulation.save", "simulation_run", rid, vid)
    return rid


def simulation_runs(conn, user, vid) -> list[dict]:
    require_vehicle(conn, user, vid)
    return simulator.list_runs(conn, 30, [vid])


# ---------------- driver-only ----------------

def maintenance_list(conn, user, vid):
    u = require_role(conn, user, "driver")
    require_vehicle(conn, u, vid)
    return maintenance.list_for(conn, vid, u["id"])


def maintenance_add(conn, user, vid, **kw) -> int:
    u = require_role(conn, user, "driver")
    require_vehicle(conn, u, vid)
    rid = maintenance.add(conn, vid, u["id"], **kw)
    audit.log(conn, u["id"], "maintenance.add", "maintenance", rid, vid)
    return rid


def maintenance_suggest(conn, user, vid, idx: int) -> int:
    u = require_role(conn, user, "driver")
    require_vehicle(conn, u, vid)
    rid = maintenance.add_suggestion(conn, vid, u["id"], int(idx))
    audit.log(conn, u["id"], "maintenance.add_suggestion", "maintenance", rid, vid)
    return rid


def _own_reminder(conn, u, rid):
    r = maintenance.get(conn, int(rid))
    if not r or r["user_id"] != u["id"] or r["vehicle_id"] not in vehicles_for(conn, u):
        raise PermissionDenied("Reminder not found.")
    return r


def maintenance_complete(conn, user, rid) -> int | None:
    u = require_role(conn, user, "driver")
    _own_reminder(conn, u, rid)
    audit.log(conn, u["id"], "maintenance.complete", "maintenance", rid, "")
    return maintenance.complete(conn, int(rid))


def maintenance_delete(conn, user, rid) -> None:
    u = require_role(conn, user, "driver")
    _own_reminder(conn, u, rid)
    maintenance.delete(conn, int(rid))
    audit.log(conn, u["id"], "maintenance.delete", "maintenance", rid, "")


def claim_vehicle(conn, user, vin: str, pairing_code: str) -> str:
    """A driver adds a vehicle by proving possession of its VIN + pairing code."""
    u = require_role(conn, user, "driver")
    r = conn.execute("SELECT vehicle_id, pairing_code_hash FROM vehicles WHERE vin = ?", ((vin or "").strip().upper(),)).fetchone()
    if not r or not r["pairing_code_hash"] or r["pairing_code_hash"] != hash_code(pairing_code or ""):
        audit.log(conn, u["id"], "vehicle.claim_failed", "vehicle", None, "")
        raise PermissionDenied("VIN or pairing code is incorrect.")
    owner = conn.execute("SELECT user_id FROM user_vehicles WHERE vehicle_id = ? AND access = 'owner'", (r[0],)).fetchone()
    if owner and owner[0] != u["id"]:
        raise PermissionDenied("This vehicle is already registered to another account.")
    conn.execute("INSERT OR IGNORE INTO user_vehicles VALUES (?,?, 'owner', ?, datetime('now'))", (u["id"], r[0], u["id"]))
    conn.commit()
    audit.log(conn, u["id"], "vehicle.claim", "vehicle", r[0], "")
    return r[0]


def register_driver(conn, username, email, display_name, password, vin, pairing_code) -> int:
    """Public registration — always role 'driver'; requires a valid vehicle pairing code."""
    r = conn.execute("SELECT vehicle_id, pairing_code_hash FROM vehicles WHERE vin = ?", ((vin or "").strip().upper(),)).fetchone()
    if not r or r["pairing_code_hash"] != hash_code(pairing_code or ""):
        raise auth.AuthError("VIN or pairing code is incorrect.")
    if conn.execute("SELECT 1 FROM user_vehicles WHERE vehicle_id = ? AND access = 'owner'", (r[0],)).fetchone():
        raise auth.AuthError("This vehicle is already registered to another account.")
    uid = auth.create_user(conn, username, email, display_name, password, "driver")
    conn.execute("INSERT INTO user_vehicles VALUES (?,?, 'owner', ?, datetime('now'))", (uid, r[0], uid))
    conn.commit()
    return uid


def update_profile(conn, user, display_name: str, email: str) -> None:
    u = _fresh(conn, user)
    display_name, email = display_name.strip(), email.strip().lower()
    if not display_name or len(display_name) > 80 or not auth.EMAIL_RE.match(email):
        raise ValueError("Enter a valid name and email.")
    if conn.execute("SELECT 1 FROM users WHERE email = ? AND id != ?", (email, u["id"])).fetchone():
        raise ValueError("That email is already in use.")
    conn.execute("UPDATE users SET display_name = ?, email = ?, updated_at = datetime('now') WHERE id = ?",
                 (display_name, email, u["id"]))
    conn.commit()
    audit.log(conn, u["id"], "user.profile_update", "user", u["id"], "")


def my_activity(conn, user, limit=50) -> pd.DataFrame:
    u = _fresh(conn, user)
    return pd.read_sql_query("SELECT ts, action, target_type, target_id FROM audit_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                             conn, params=(u["id"], limit))


# ---------------- engineer-only ----------------

def eng(conn, user) -> dict:
    return require_role(conn, user, "engineer")


def fleet_alerts(conn, user) -> pd.DataFrame:
    eng(conn, user)
    vids = vehicles_for(conn, user)
    return pd.concat([db.read_alerts(conn, vehicle_id=v) for v in vids]) if vids else pd.DataFrame()


def data_quality(conn, user, vid) -> dict:
    eng(conn, user)
    require_vehicle(conn, user, vid)
    return dataquality.report(conn, vid)


def thresholds(conn, user) -> dict:
    eng(conn, user)
    return db.get_thresholds(conn)


def set_thresholds(conn, user, th: dict) -> int:
    u = eng(conn, user)
    clean = {}
    for k, v in th.items():
        if k not in config.DEFAULT_THRESHOLDS:
            raise ValueError(f"Unknown threshold {k}")
        v = float(v)
        if not -100 <= v <= 1000:
            raise ValueError(f"{k} out of range")
        clean[k] = v
    if not clean["battery_temp_low_c"] <= clean["battery_temp_medium_c"] <= clean["battery_temp_high_c"]:
        raise ValueError("Battery thresholds must satisfy low ≤ medium ≤ high.")
    old = db.get_thresholds(conn)
    db.set_thresholds(conn, clean)
    n = len(anomaly.run_detection(conn))
    changed = {k: (old.get(k), v) for k, v in clean.items() if old.get(k) != v}
    audit.log(conn, u["id"], "thresholds.update", "settings", "thresholds", json.dumps(changed)[:480])
    return n


def run_detection(conn, user) -> int:
    u = eng(conn, user)
    n = 0
    for vid in vehicles_for(conn, u):
        n += len(anomaly.run_detection(conn, vehicle_id=vid))
    audit.log(conn, u["id"], "detection.run", "vehicle", ",".join(vehicles_for(conn, u)), f"alerts={n}")
    return n


def evaluate_detection(conn, user, th: dict | None = None) -> dict:
    """Compare detection (current or candidate thresholds) against labelled synthetic events — read-only."""
    u = eng(conn, user)
    import tempfile
    from pathlib import Path
    from . import data_gen
    gt = data_gen.ground_truth()
    th = th or db.get_thresholds(conn)
    alerts_ = []
    for vid in db.vehicle_ids(conn):
        alerts_ += anomaly.detect_vehicle(conn, vid, th)
    res = {"demo_vehicle": anomaly.evaluate(alerts_, gt), "fleet": anomaly.evaluate(alerts_, gt, include_fleet=True)}
    conn.execute("INSERT INTO eval_runs (created_at, user_id, kind, metrics_json, conditions_json) VALUES (datetime('now'),?,?,?,?)",
                 (u["id"], "detection", json.dumps({k: {m: v[m] for m in ("precision", "recall", "f1")} for k, v in res.items()}),
                  json.dumps({"thresholds_hash": anomaly.thresholds_hash(th), "ruleset": anomaly.RULESET_VERSION})))
    conn.commit()
    return res


def eval_history(conn, user) -> pd.DataFrame:
    eng(conn, user)
    return pd.read_sql_query("SELECT * FROM eval_runs ORDER BY id DESC LIMIT 50", conn)


def engineers_for_vehicle(conn, user, vid) -> pd.DataFrame:
    eng(conn, user)
    require_vehicle(conn, user, vid)
    return pd.read_sql_query("SELECT u.id, u.display_name, u.username FROM users u JOIN user_vehicles uv ON uv.user_id = u.id "
                             "WHERE uv.vehicle_id = ? AND uv.access = 'engineer' AND u.status = 'active' AND u.role = 'engineer'",
                             conn, params=(vid,))


def case_list(conn, user) -> pd.DataFrame:
    eng(conn, user)
    return cases.list_cases(conn, vehicles_for(conn, user))


def _case(conn, u, cid) -> dict:
    c = cases.get(conn, int(cid))
    if not c or c["vehicle_id"] not in vehicles_for(conn, u):
        audit.log(conn, u["id"], "authz.denied", "case", cid, "")
        raise PermissionDenied("Case not found or not authorised.")
    return dict(c)


def case_create(conn, user, vid, title, priority="Medium", event_key=None, summary="", assigned_to=None) -> int:
    u = eng(conn, user)
    require_vehicle(conn, u, vid)
    if event_key:
        _check_event(conn, u, event_key, vid)
    if assigned_to and int(assigned_to) not in engineers_for_vehicle(conn, u, vid)["id"].tolist():
        raise PermissionDenied("Assignee must be an engineer authorised for this vehicle.")
    cid = cases.create(conn, vid, title, u["id"], priority, event_key, summary, assigned_to)
    audit.log(conn, u["id"], "case.create", "case", cid, f"vehicle={vid} event={event_key}")
    return cid


def case_get(conn, user, cid) -> dict:
    u = eng(conn, user)
    return _case(conn, u, cid)


def case_update(conn, user, cid, **kw) -> None:
    u = eng(conn, user)
    c = _case(conn, u, cid)
    if kw.get("assigned_to") not in (None, -1) and int(kw["assigned_to"]) not in engineers_for_vehicle(conn, u, c["vehicle_id"])["id"].tolist():
        raise PermissionDenied("Assignee must be an engineer authorised for this vehicle.")
    cases.update(conn, int(cid), **kw)
    audit.log(conn, u["id"], "case.update", "case", cid, json.dumps({k: v for k, v in kw.items() if k != "resolution"}, default=str))


def case_note(conn, user, cid, body) -> int:
    u = eng(conn, user)
    _case(conn, u, cid)
    nid = cases.add_note(conn, int(cid), u["id"], body)
    audit.log(conn, u["id"], "case.note", "case", cid, f"note={nid}")
    return nid


def case_notes(conn, user, cid):
    u = eng(conn, user)
    _case(conn, u, cid)
    return cases.notes(conn, int(cid))


def case_hypotheses(conn, user, cid):
    u = eng(conn, user)
    _case(conn, u, cid)
    return cases.hypotheses(conn, int(cid))


def case_add_hypothesis(conn, user, cid, text, strength, evidence=None) -> int:
    u = eng(conn, user)
    _case(conn, u, cid)
    hid = cases.add_hypothesis(conn, int(cid), text, strength, evidence)
    audit.log(conn, u["id"], "case.hypothesis_add", "case", cid, f"hypothesis={hid}")
    return hid


def case_review_hypothesis(conn, user, hid, status, note) -> None:
    u = eng(conn, user)
    h = conn.execute("SELECT case_id FROM case_hypotheses WHERE id = ?", (int(hid),)).fetchone()
    if not h:
        raise PermissionDenied("Not found.")
    _case(conn, u, h[0])
    cases.review_hypothesis(conn, int(hid), u["id"], status, note)
    audit.log(conn, u["id"], f"case.hypothesis_{status}", "case", h[0], f"hypothesis={hid}")


def case_report(conn, user, cid) -> dict:
    u = eng(conn, user)
    _case(conn, u, cid)
    audit.log(conn, u["id"], "case.report", "case", cid, "")
    return cases.investigation_report(conn, int(cid))


def case_audit(conn, user, cid) -> pd.DataFrame:
    u = eng(conn, user)
    _case(conn, u, cid)
    return audit.read(conn, 200, "case", [str(cid)])


def recent_investigations(conn, user) -> pd.DataFrame:
    u = eng(conn, user)
    ids = [str(i) for i in case_list(conn, u)["id"]]
    return audit.read(conn, 20, "case", ids)


# ---------------- admin-only ----------------

def admin_users(conn, user) -> pd.DataFrame:
    require_role(conn, user, "admin")
    return pd.read_sql_query("SELECT id, username, email, display_name, role, status, must_change_password, failed_attempts,"
                             " locked_until, created_at FROM users ORDER BY id", conn)


def provision_user(conn, user, username, email, display_name, password, role) -> int:
    u = require_role(conn, user, "admin")
    if role not in ("engineer", "driver", "admin"):
        raise ValueError("Invalid role.")
    return auth.create_user(conn, username, email, display_name, password, role, actor_id=u["id"], must_change=True)


def set_user_status(conn, user, uid: int, status: str) -> None:
    u = require_role(conn, user, "admin")
    if status not in ("active", "disabled") or int(uid) == u["id"]:
        raise ValueError("Invalid status change.")
    conn.execute("UPDATE users SET status = ?, updated_at = datetime('now') WHERE id = ?", (status, int(uid)))
    conn.execute("UPDATE sessions SET revoked = 1 WHERE user_id = ?", (int(uid),))
    conn.commit()
    audit.log(conn, u["id"], f"user.{status}", "user", uid, "")


def assign_vehicle(conn, user, uid: int, vid: str, grant: bool = True) -> None:
    u = require_role(conn, user, "admin")
    t = conn.execute("SELECT role FROM users WHERE id = ?", (int(uid),)).fetchone()
    if not t or vid not in db.vehicle_ids(conn):
        raise ValueError("Unknown user or vehicle.")
    access = {"driver": "owner", "engineer": "engineer"}.get(t[0])
    if not access:
        raise ValueError("Admins cannot be assigned vehicles.")
    if grant:
        if access == "owner" and conn.execute("SELECT 1 FROM user_vehicles WHERE vehicle_id = ? AND access='owner' AND user_id != ?",
                                              (vid, int(uid))).fetchone():
            raise ValueError("Vehicle already has an owner.")
        conn.execute("INSERT OR IGNORE INTO user_vehicles VALUES (?,?,?,?, datetime('now'))", (int(uid), vid, access, u["id"]))
    else:
        conn.execute("DELETE FROM user_vehicles WHERE user_id = ? AND vehicle_id = ?", (int(uid), vid))
    conn.commit()
    audit.log(conn, u["id"], "vehicle.grant" if grant else "vehicle.revoke", "user", uid, vid)


def admin_assignments(conn, user) -> pd.DataFrame:
    require_role(conn, user, "admin")
    return pd.read_sql_query("SELECT uv.user_id, u.username, u.role, uv.vehicle_id, uv.access, uv.granted_at FROM user_vehicles uv "
                             "JOIN users u ON u.id = uv.user_id ORDER BY uv.vehicle_id", conn)


def admin_reset_token(conn, user, uid: int) -> str:
    u = require_role(conn, user, "admin")
    r = conn.execute("SELECT username FROM users WHERE id = ?", (int(uid),)).fetchone()
    if not r:
        raise ValueError("Unknown user.")
    return auth.request_reset(conn, r[0], actor_id=u["id"])


def admin_audit(conn, user, limit=300) -> pd.DataFrame:
    require_role(conn, user, "admin")
    return audit.read(conn, limit)


def admin_vehicles(conn, user) -> pd.DataFrame:
    require_role(conn, user, "admin")
    return pd.read_sql_query("SELECT vehicle_id, nickname, name, vin, data_mode FROM vehicles ORDER BY vehicle_id", conn)
