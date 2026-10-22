"""Event Detective: reconstructs what happened around a warning or abnormal reading.

Output strictly separates CONFIRMED observations (directly read from records) from
POSSIBLE explanations (hypotheses ranked by heuristic support; never asserted as root cause).
"""
from __future__ import annotations

import json

import pandas as pd

from . import config, db

CHANNELS = ["battery_temp_c", "motor_temp_c", "ambient_temp_c", "speed_kmh", "power_kw", "soc_pct", "tire_pressure_kpa"]


def list_events(conn, vehicle_id: str = config.VEHICLE_ID) -> pd.DataFrame:
    """Investigable events: diagnostic events + persisted alerts for one vehicle."""
    d = db.read_dtcs(conn, vehicle_id=vehicle_id)
    d = pd.DataFrame({"key": "dtc:" + d["id"].astype(str), "ts": d["ts"], "kind": "Diagnostic event",
                      "label": d["code"] + " — " + d["description"], "subsystem": d["subsystem"]})
    a = db.read_alerts(conn, vehicle_id=vehicle_id)
    a = a[a["rule_id"] != "R-DTC-SINGLE"]
    a = pd.DataFrame({"key": "alert:" + a["id"].astype(str), "ts": a["ts"], "kind": "Alert (" + a["severity"] + ")",
                      "label": a["rule_id"] + " — " + a["message"], "subsystem": a["subsystem"]})
    return pd.concat([d, a]).sort_values("ts", ascending=False).reset_index(drop=True)


def event_vehicle(conn, key: str) -> str:
    return _resolve(conn, key)["vehicle_id"]


def _resolve(conn, key: str) -> dict:
    kind, _id = str(key).split(":")
    if kind == "dtc":
        r = conn.execute("SELECT * FROM diagnostic_events WHERE id = ?", (int(_id),)).fetchone()
        if not r:
            raise KeyError(key)
        return {"kind": "dtc", "vehicle_id": r["vehicle_id"], "ts": pd.Timestamp(r["ts"]), "subsystem": r["subsystem"], "title": f"{r['code']} — {r['description']}",
                "code": r["code"], "record": dict(r)}
    r = conn.execute("SELECT * FROM alerts WHERE id = ?", (int(_id),)).fetchone()
    if not r:
        raise KeyError(key)
    return {"kind": "alert", "vehicle_id": r["vehicle_id"], "ts": pd.Timestamp(r["ts"]), "subsystem": r["subsystem"], "title": r["message"],
            "code": r["rule_id"], "record": dict(r)}


def _focus_channel(subsystem: str) -> str | None:
    return {"battery": "battery_temp_c", "thermal": "motor_temp_c", "tyres": "tire_pressure_kpa",
            "energy": "power_kw", "data": "battery_temp_c"}.get(subsystem)


def investigate(conn, key: str, before_min: int = 120, after_min: int = 60) -> dict:
    ev = _resolve(conn, key)
    t0 = ev["ts"]
    start, end = t0 - pd.Timedelta(minutes=before_min), t0 + pd.Timedelta(minutes=after_min)
    vid = ev["vehicle_id"]
    tel = db.read_telemetry(conn, start.isoformat(), end.isoformat(), vid).drop_duplicates("ts")
    valid = tel[tel["valid"] == 1]
    before = valid[valid["ts"] < t0]
    after = valid[valid["ts"] >= t0]
    focus = _focus_channel(ev["subsystem"])

    comparison = []
    for ch in CHANNELS:
        b, a = before[ch].dropna(), after[ch].dropna()
        comparison.append({"channel": ch,
                           "before_mean": round(b.mean(), 2) if len(b) else None,
                           "before_max": round(b.max(), 2) if len(b) else None,
                           "after_mean": round(a.mean(), 2) if len(a) else None,
                           "after_max": round(a.max(), 2) if len(a) else None})

    # chronological timeline: state transitions, DTCs, alerts, peaks
    timeline = []
    prev = None
    for _, r in tel.iterrows():
        if r["state"] != prev:
            desc = {"driving": f"Driving started ({r['trip_id']}, ~{r['speed_kmh']:.0f} km/h)",
                    "charging": f"Charging started ({-r['power_kw']:.0f} kW)", "parked": "Vehicle parked"}[r["state"]]
            timeline.append({"ts": r["ts"].isoformat(), "type": "state", "text": desc, "record_id": int(r["id"]),
                             "source": "measured"})
            prev = r["state"]
        if r["valid"] == 0:
            timeline.append({"ts": r["ts"].isoformat(), "type": "data_quality",
                             "text": f"Invalid reading excluded ({r['validity_note']})", "record_id": int(r["id"]), "source": "measured"})
    dtcs = db.read_dtcs(conn, start.isoformat(), end.isoformat(), vid)
    for _, r in dtcs.iterrows():
        timeline.append({"ts": r["ts"].isoformat(), "type": "dtc", "text": f"{r['code']}: {r['description']}",
                         "record_id": int(r["id"]), "source": "measured"})
    if focus and focus in valid and valid[focus].notna().any():
        i = valid[focus].idxmax()
        timeline.append({"ts": valid.loc[i, "ts"].isoformat(), "type": "peak",
                         "text": f"Peak {focus} = {valid.loc[i, focus]:.1f}", "record_id": int(valid.loc[i, "id"]),
                         "source": "calculated"})
    gaps = tel["ts"].diff().dt.total_seconds().div(60)
    for i in gaps[gaps > 30].index:
        timeline.append({"ts": tel.loc[i - 1, "ts"].isoformat() if i - 1 in tel.index else tel.loc[i, "ts"].isoformat(),
                         "type": "data_quality", "text": f"Logging gap of {gaps[i]:.0f} min", "record_id": int(tel.loc[i, "id"]),
                         "source": "measured"})
    timeline.sort(key=lambda x: x["ts"])

    # prior occurrences
    history = []
    if ev["kind"] == "dtc":
        for r in conn.execute("SELECT id, ts FROM diagnostic_events WHERE code = ? AND id != ? AND vehicle_id = ? ORDER BY ts",
                              (ev["code"], ev["record"]["id"], vid)):
            history.append({"id": r["id"], "ts": r["ts"]})

    confirmed, hypotheses, limitations = _reason(ev, tel, valid, before, after, focus, dtcs, gaps)
    if history:
        confirmed.append(f"The same code occurred {len(history)} other time(s): " + ", ".join(h["ts"][:16] for h in history) + ".")

    return {
        "vehicle_id": vid,
        "event": {"key": key, "ts": t0.isoformat(), "title": ev["title"], "subsystem": ev["subsystem"],
                  "code": ev["code"], "kind": ev["kind"]},
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "focus_channel": focus,
        "telemetry": tel,
        "comparison": comparison,
        "timeline": timeline,
        "history": history,
        "confirmed": confirmed,
        "possible_explanations": hypotheses,
        "limitations": limitations,
        "evidence_record_ids": tel["id"].tolist(),
    }


def _reason(ev, tel, valid, before, after, focus, dtcs, gaps):
    confirmed, hyp, lim = [], [], []
    if tel.empty:
        lim.append("No telemetry was recorded in the investigation window, so surrounding conditions are unknown.")
        return confirmed, hyp, lim
    t0 = ev["ts"]
    if focus and before[focus].notna().any():
        b0 = before[focus].dropna()
        first, last = b0.iloc[0], b0.iloc[-1]
        confirmed.append(f"{focus} changed from {first:.1f} to {last:.1f} in the {len(b0)} valid samples before the event.")
    states = before.groupby("state")["id"].count().to_dict()
    if states:
        confirmed.append("Operating states before the event (10-min samples): " +
                         ", ".join(f"{k} ×{v}" for k, v in states.items()) + ".")
    chg = before[before["state"] == "charging"]
    drv = before[before["state"] == "driving"]
    amb = valid["ambient_temp_c"].mean()
    confirmed.append(f"Mean ambient temperature in the window was {amb:.1f} °C.")

    sub = ev["subsystem"]
    if sub == "battery":
        if len(chg) and chg["power_kw"].min() <= -50:
            hyp.append({"text": f"High-power DC charging (~{-chg['power_kw'].min():.0f} kW) immediately preceded the warning; "
                                "fast charging increases pack heat generation.", "support": "strong",
                        "evidence": chg["id"].tolist()[:6]})
        if len(drv) and drv["speed_kmh"].mean() > 90:
            hyp.append({"text": f"Sustained high-speed driving (mean {drv['speed_kmh'].mean():.0f} km/h) before charging may have "
                                "raised the starting pack temperature.", "support": "moderate", "evidence": drv["id"].tolist()[:6]})
        if amb > 28:
            hyp.append({"text": f"High ambient temperature ({amb:.0f} °C) reduces cooling headroom.", "support": "moderate",
                        "evidence": []})
    elif sub == "thermal":
        rise = valid["motor_temp_c"].diff().max()
        if pd.notna(rise) and rise > 8:
            hyp.append({"text": f"Motor temperature rose up to {rise:.1f} °C per sample while speed stayed moderate "
                                f"(~{drv['speed_kmh'].mean():.0f} km/h) — consistent with reduced cooling rather than high load.",
                        "support": "moderate", "evidence": valid.loc[valid['motor_temp_c'].diff() > 8, 'id'].tolist()})
        if any(dtcs["code"].str.contains("THM-P")):
            hyp.append({"text": "A coolant-flow diagnostic code was logged in the same window; a cooling-circuit issue is plausible "
                                "but requires inspection to confirm.", "support": "moderate",
                        "evidence": dtcs.loc[dtcs['code'].str.contains('THM-P'), 'id'].tolist()})
    elif sub == "tyres":
        if valid["tire_pressure_kpa"].notna().any():
            confirmed.append(f"Lowest tyre pressure in window: {valid['tire_pressure_kpa'].min():.0f} kPa.")
        if amb < 12:
            hyp.append({"text": f"Cold ambient temperature ({amb:.0f} °C) lowers tyre pressure; a slow leak is also possible.",
                        "support": "weak", "evidence": []})
    if (gaps > 30).any():
        lim.append("A logging gap exists in this window; events during the gap are unknown.")
    if valid.shape[0] < tel.shape[0]:
        lim.append(f"{tel.shape[0] - valid.shape[0]} invalid reading(s) were excluded.")
    if not hyp:
        lim.append("Insufficient evidence: the recorded telemetry does not show a condition that plausibly explains this event. "
                   "No cause is suggested.")
    lim.append("Temporal precedence is not causation; possible explanations are hypotheses, not confirmed root causes.")
    return confirmed, hyp, lim
