"""Early-warning engine: rule-based + lightweight statistical anomaly detection.

Severity rules (documented; demonstration thresholds, NOT manufacturer specifications)
  R-BATT-TEMP   battery_temp_c episode max >= high -> High, >= medium -> Medium, >= low -> Low
  R-MOTOR-ROC   motor temp rise >= rate C per 10 min -> Medium; if episode max >= motor_temp_high -> High
  R-CONS-DEV    daily consumption vs rolling baseline: >= medium% -> Medium, >= low% -> Low
                (anomaly_score = z-score vs baseline days, reported for engineers)
  R-DTC-REPEAT  same code >= N times within window days -> Medium
  R-DTC-SINGLE  any isolated warning-level DTC -> Low (info-level DTC -> Informational)
  R-DATA-GAP    logging gap > gap_minutes -> Low
  R-DATA-MISS   NULL readings in a key channel: >= 3 consecutive -> Low, else Informational
  R-DATA-IMPL   physically implausible reading (validator) -> Medium (sensor/data quality)
No rule claims a component will fail; next steps are conservative.
"""
from __future__ import annotations

import hashlib
import json

import numpy as np
import pandas as pd

from . import analytics, config, db


def _alert(ts, subsystem, category, severity, rule_id, message, next_step, evidence,
           metric=None, observed=None, threshold=None, baseline=None, score=None, ts_end=None) -> dict:
    ts = pd.Timestamp(ts).isoformat()
    fp = hashlib.sha1(f"{rule_id}|{ts}|{metric}".encode()).hexdigest()[:16]
    return dict(vehicle_id=config.VEHICLE_ID, ts=ts, ts_end=pd.Timestamp(ts_end).isoformat() if ts_end is not None else None,
                subsystem=subsystem, category=category, severity=severity, rule_id=rule_id, metric=metric,
                observed_value=None if observed is None else round(float(observed), 2),
                threshold=None if threshold is None else round(float(threshold), 2),
                baseline=None if baseline is None else round(float(baseline), 2),
                anomaly_score=None if score is None else round(float(score), 2),
                message=message, evidence_json=json.dumps(evidence, default=str), next_step=next_step,
                fingerprint=fp)


def _episodes(mask: pd.Series) -> list[tuple[int, int]]:
    """Contiguous True runs as (start_pos, end_pos) inclusive."""
    m = mask.to_numpy()
    out, start = [], None
    for i, v in enumerate(m):
        if v and start is None:
            start = i
        if not v and start is not None:
            out.append((start, i - 1)); start = None
    if start is not None:
        out.append((start, len(m) - 1))
    return out


def detect_battery_temp(df: pd.DataFrame, th: dict) -> list[dict]:
    v = df[df["valid"] == 1].reset_index(drop=True)
    out = []
    for a, b in _episodes(v["battery_temp_c"] >= th["battery_temp_low_c"]):
        seg = v.iloc[a:b + 1]
        peak_i = seg["battery_temp_c"].idxmax()
        peak = seg.loc[peak_i]
        mx = peak["battery_temp_c"]
        sev = ("High" if mx >= th["battery_temp_high_c"] else
               "Medium" if mx >= th["battery_temp_medium_c"] else "Low")
        thr = {"High": th["battery_temp_high_c"], "Medium": th["battery_temp_medium_c"], "Low": th["battery_temp_low_c"]}[sev]
        out.append(_alert(
            seg["ts"].iloc[0], "battery", "battery_temp", sev, "R-BATT-TEMP",
            f"Battery temperature reached {mx:.1f} °C (state: {peak['state']}).",
            "Avoid further DC fast charging until the pack cools; if the warning recurs, have the battery cooling system inspected.",
            {"record_ids": seg["id"].tolist(), "peak_record_id": int(peak["id"]), "peak_ts": peak["ts"].isoformat(),
             "source_ref": peak["source_ref"], "states": sorted(seg["state"].unique().tolist())},
            "battery_temp_c", mx, thr, ts_end=seg["ts"].iloc[-1]))
    return out


def detect_motor_roc(df: pd.DataFrame, th: dict) -> list[dict]:
    v = df[df["valid"] == 1].reset_index(drop=True)
    dt = v["ts"].diff().dt.total_seconds() / 60
    rate = v["motor_temp_c"].diff() / dt * 10  # °C per 10 min
    mask = (rate >= th["motor_temp_rate_c_per_10min"]) & (dt <= 15)
    out = []
    # merge episodes separated by <= 2 samples
    eps = _episodes(mask.fillna(False))
    merged = []
    for e in eps:
        if merged and e[0] - merged[-1][1] <= 3:
            merged[-1] = (merged[-1][0], e[1])
        else:
            merged.append(e)
    for a, b in merged:
        seg = v.iloc[max(a - 1, 0):b + 2]
        mx = seg["motor_temp_c"].max()
        max_rate = rate.iloc[a:b + 1].max()
        sev = "High" if mx >= th["motor_temp_high_c"] else "Medium"
        out.append(_alert(
            v["ts"].iloc[a], "thermal", "motor_temp", sev, "R-MOTOR-ROC",
            f"Motor temperature rose rapidly (up to {max_rate:.1f} °C per 10 min, peak {mx:.1f} °C).",
            "Reduce load and stop safely if a temperature warning is shown; have the motor cooling circuit checked.",
            {"record_ids": seg["id"].tolist(), "max_rate_c_per_10min": round(float(max_rate), 2),
             "peak_c": round(float(mx), 2), "source_ref": v["source_ref"].iloc[a]},
            "motor_temp_c", max_rate, th["motor_temp_rate_c_per_10min"], ts_end=seg["ts"].iloc[-1]))
    return out


def detect_consumption(conn, th: dict, vehicle_id: str = config.VEHICLE_ID) -> list[dict]:
    d = analytics.daily_consumption(conn, vehicle_id=vehicle_id)
    d = d[d["distance_km"] >= 5].reset_index(drop=True)
    n = int(th["consumption_baseline_days"])
    out = []
    for i in range(n, len(d)):
        base = d["kwh_per_100km"].iloc[i - n:i]
        mu, sd = base.mean(), base.std(ddof=1) or 1e-6
        x = d["kwh_per_100km"].iloc[i]
        dev = (x - mu) / mu * 100
        if dev < th["consumption_low_pct"]:
            continue
        sev = "Medium" if dev >= th["consumption_medium_pct"] else "Low"
        z = (x - mu) / sd
        row = d.iloc[i]
        out.append(_alert(
            row["date"], "energy", "energy", sev, "R-CONS-DEV",
            f"Energy consumption {x:.1f} kWh/100 km was {dev:.0f}% above the prior {n}-day baseline ({mu:.1f}).",
            "Check tyre pressures, HVAC use and driving speed; consumption usually returns to baseline when these normalise.",
            {"date": row["date"].isoformat(), "distance_km": round(row["distance_km"], 1),
             "energy_kwh": round(row["energy_kwh"], 2), "avg_speed_kmh": round(row["avg_speed"], 1),
             "avg_ambient_c": round(row["avg_ambient"], 1), "record_id_range": [int(row["first_id"]), int(row["last_id"])]},
            "kwh_per_100km", x, mu * (1 + th["consumption_low_pct"] / 100), mu, z,
            ts_end=row["date"] + pd.Timedelta(days=1)))
    return out


def detect_dtcs(conn, th: dict, vehicle_id: str = config.VEHICLE_ID) -> list[dict]:
    d = db.read_dtcs(conn, vehicle_id=vehicle_id)
    out, repeated_ids = [], set()
    win = pd.Timedelta(days=th["repeat_dtc_window_days"])
    for code, g in d.groupby("code"):
        g = g.sort_values("ts")
        for i in range(len(g)):
            w = g[(g["ts"] >= g["ts"].iloc[i]) & (g["ts"] < g["ts"].iloc[i] + win)]
            if len(w) >= th["repeat_dtc_count"]:
                repeated_ids.update(w["id"])
                out.append(_alert(
                    w["ts"].iloc[-1], w["subsystem"].iloc[0], "repeated_dtc", "Medium", "R-DTC-REPEAT",
                    f"{code} ({w['description'].iloc[0]}) occurred {len(w)} times between "
                    f"{w['ts'].iloc[0]:%Y-%m-%d} and {w['ts'].iloc[-1]:%Y-%m-%d}.",
                    "Recurring code: inspect the related component (e.g. check and correct tyre pressures) and monitor for recurrence.",
                    {"dtc_ids": w["id"].tolist(), "timestamps": [t.isoformat() for t in w["ts"]]},
                    "dtc_count", len(w), th["repeat_dtc_count"], ts_end=w["ts"].iloc[-1]))
                break
    for _, r in d.iterrows():
        if r["id"] in repeated_ids:
            continue
        sev = "Low" if r["reported_level"] == "warning" else "Informational"
        out.append(_alert(
            r["ts"], r["subsystem"], "dtc", sev, "R-DTC-SINGLE",
            f"Diagnostic event {r['code']}: {r['description']}.",
            "Note the event; investigate in Event Detective for surrounding conditions.",
            {"dtc_ids": [int(r["id"])], "source_ref": r["source_ref"]}, "dtc", None))
    return out


def detect_data_quality(df: pd.DataFrame, th: dict) -> list[dict]:
    out = []
    for g in analytics.data_gaps(df, th["gap_minutes"]):
        out.append(_alert(
            g["from"], "data", "missing_data", "Low", "R-DATA-GAP",
            f"No telemetry logged for {g['minutes']:.0f} minutes ({g['from'][11:16]}–{g['to'][11:16]}).",
            "Treat analyses covering this window as incomplete; check the data logger connection.",
            {"before_id": g["before_id"], "after_id": g["after_id"]}, "gap_minutes", g["minutes"], th["gap_minutes"],
            ts_end=g["to"]))
    for col in ["soc_pct", "battery_temp_c", "motor_temp_c"]:
        for a, b in _episodes(df[col].isna().reset_index(drop=True)):
            seg = df.iloc[a:b + 1]
            out.append(_alert(
                seg["ts"].iloc[0], "data", "missing_data", "Low" if len(seg) >= 3 else "Informational", "R-DATA-MISS",
                f"{len(seg)} consecutive readings of {col} are missing.",
                "Values in this window are unknown and were excluded from calculations.",
                {"record_ids": seg["id"].tolist()}, col, len(seg), ts_end=seg["ts"].iloc[-1]))
    v = df.reset_index(drop=True)
    for a, b in _episodes(v["valid"] == 0):
        seg = v.iloc[a:b + 1]
        r = seg.iloc[0]
        out.append(_alert(
            r["ts"], "data", "implausible", "Medium", "R-DATA-IMPL",
            f"{len(seg)} implausible reading(s) flagged ({r['validity_note']}).",
            "Readings excluded from analytics (source records preserved). If implausible readings repeat, have the sensor checked.",
            {"record_ids": seg["id"].tolist(), "source_ref": r["source_ref"]},
            (r["validity_note"] or "").replace("implausible ", "") or None, len(seg), ts_end=seg["ts"].iloc[-1]))
    return out


def detect_sensor_quality(conn, df: pd.DataFrame, vehicle_id: str) -> list[dict]:
    from . import dataquality as dq
    out = []
    dup = dq.duplicate_timestamps(df)
    if len(dup):
        out.append(_alert(dup["ts"].iloc[0], "data", "duplicate", "Low", "R-DATA-DUP",
                          f"{dup['ts'].nunique()} timestamp(s) logged more than once ({len(dup)} rows).",
                          "Duplicates are kept as raw records; analytics use the first record per timestamp.",
                          {"record_ids": dup["id"].tolist()}, "duplicate_rows", len(dup), ts_end=dup["ts"].iloc[-1]))
    for u in dq.unit_suspects(df):
        out.append(_alert(u["start"], "data", "unit", "Medium", "R-DATA-UNIT",
                          f"{u['channel']} jumped from ~{u['median_before']} to ~{u['median_in_segment']} for {u['samples']} samples — "
                          f"consistent with °F logged as °C (≈{u['fahrenheit_if_converted_c']} °C if converted).",
                          "Check the logger's unit configuration. Values were not converted automatically.",
                          {"record_ids": u["record_ids"][:50]}, u["channel"], u["median_in_segment"], ts_end=u["end"]))
    for f in dq.flatlines(df):
        out.append(_alert(f["start"], "data", "flatline", "Medium", "R-DATA-FLAT",
                          f"{f['channel']} stayed at exactly {f['value']} for {f['samples']} samples while driving — possible stuck sensor.",
                          "Treat this channel as unreliable in that window; inspect sensor and wiring.",
                          {"record_ids": f["record_ids"]}, f["channel"], f["samples"], ts_end=f["end"]))
    st = dq.staleness(conn, vehicle_id)
    if st["stale"]:
        out.append(_alert(st["last_record"], "data", "stale", "Medium", "R-DATA-STALE",
                          f"No data received for {st['age_hours']:.0f} h (last record {st['last_record'][:16]}).",
                          "Latest readings are out of date; check telematics connectivity before relying on them.",
                          {"last_record": st["last_record"]}, "age_hours", st["age_hours"], 24))
    return out


RULESET_VERSION = "2.0"
RULE_VERSIONS = {"R-BATT-TEMP": "1.0", "R-MOTOR-ROC": "1.0", "R-CONS-DEV": "1.0", "R-DTC-REPEAT": "1.0",
                 "R-DTC-SINGLE": "1.0", "R-DATA-GAP": "1.0", "R-DATA-MISS": "1.1", "R-DATA-IMPL": "1.1",
                 "R-DATA-DUP": "1.0", "R-DATA-UNIT": "1.0", "R-DATA-FLAT": "1.0", "R-DATA-STALE": "1.0"}


def thresholds_hash(th: dict) -> str:
    return hashlib.sha1(json.dumps(th, sort_keys=True).encode()).hexdigest()[:8]


def detect_vehicle(conn, vehicle_id: str, th: dict | None = None) -> list[dict]:
    th = th or db.get_thresholds(conn)
    df = db.read_telemetry(conn, vehicle_id=vehicle_id)
    dedup = df.drop_duplicates("ts")
    alerts = (detect_battery_temp(dedup, th) + detect_motor_roc(dedup, th) + detect_consumption(conn, th, vehicle_id)
              + detect_dtcs(conn, th, vehicle_id) + detect_data_quality(dedup, th) + detect_sensor_quality(conn, df, vehicle_id))
    th_h = thresholds_hash(th)
    for a in alerts:
        a["vehicle_id"] = vehicle_id
        a["rule_version"] = f"{a['rule_id']}@{RULE_VERSIONS.get(a['rule_id'], '1.0')}/ruleset-{RULESET_VERSION}/th-{th_h}"
        a["fingerprint"] = hashlib.sha1(f"{vehicle_id}|{a['rule_id']}|{a['ts']}|{a['metric']}".encode()).hexdigest()[:16]
    alerts.sort(key=lambda a: a["ts"])
    return alerts


def run_detection(conn, persist: bool = True, vehicle_id: str | None = None) -> list[dict]:
    """Run detection for one vehicle or (vehicle_id=None) the whole fleet."""
    vids = [vehicle_id] if vehicle_id else db.vehicle_ids(conn)
    th = db.get_thresholds(conn)
    all_alerts = []
    for vid in vids:
        alerts = detect_vehicle(conn, vid, th)
        all_alerts += alerts
        if persist:
            acked = {r[0]: r[1] for r in conn.execute(
                "SELECT fingerprint, ack_ts FROM alerts WHERE acknowledged = 1 AND vehicle_id = ?", (vid,))}
            conn.execute("DELETE FROM alerts WHERE vehicle_id = ?", (vid,))  # derived data only; recomputed
            for a in alerts:
                a = dict(a)
                a["acknowledged"] = 1 if a["fingerprint"] in acked else 0
                a["ack_ts"] = acked.get(a["fingerprint"])
                conn.execute(f"INSERT INTO alerts ({','.join(a)}) VALUES ({','.join('?' * len(a))})", list(a.values()))
    if persist:
        conn.commit()
    return all_alerts


def acknowledge(conn, alert_id: int, ack: bool = True) -> None:
    conn.execute("UPDATE alerts SET acknowledged = ?, ack_ts = CASE WHEN ? THEN datetime('now') END WHERE id = ?",
                 (1 if ack else 0, 1 if ack else 0, alert_id))
    conn.commit()


# ---------- evaluation against labelled synthetic events ----------

def evaluate(alerts: list[dict], ground_truth: dict, min_severity: str = "Low", include_fleet: bool = False) -> dict:
    """Score alerts against labelled events. Default: demo vehicle only (comparable with v1)."""
    rank = config.SEVERITY_RANK
    gts = [dict(g, vehicle_id=config.VEHICLE_ID) for g in ground_truth["events"]]
    if include_fleet:
        gts += ground_truth.get("fleet_events", [])
    vids = {g["vehicle_id"] for g in gts}
    pool = [a for a in alerts if rank[a["severity"]] >= rank[min_severity] and a["category"] != "dtc"
            and a.get("vehicle_id", config.VEHICLE_ID) in vids]
    tol = pd.Timedelta(minutes=30)

    def match(a, g):
        if a["category"] != g["category"] or a.get("vehicle_id", config.VEHICLE_ID) != g["vehicle_id"]:
            return False
        t = pd.Timestamp(a["ts"])
        return pd.Timestamp(g["start"]) - tol <= t <= pd.Timestamp(g["end"]) + tol

    tp_alerts = [a for a in pool if any(match(a, g) for g in gts)]
    detected = [g for g in gts if any(match(a, g) for a in pool)]
    p = len(tp_alerts) / len(pool) if pool else 0.0
    r = len(detected) / len(gts) if gts else 0.0
    f1 = 2 * p * r / (p + r) if p + r else 0.0
    return {"min_severity": min_severity, "alerts_scored": len(pool), "true_positive_alerts": len(tp_alerts),
            "events": len(gts), "events_detected": len(detected),
            "missed": [g["id"] for g in gts if g not in detected],
            "false_positives": [{"vehicle": a.get("vehicle_id"), "rule": a["rule_id"], "ts": a["ts"], "msg": a["message"]} for a in pool if a not in tp_alerts],
            "precision": round(p, 3), "recall": round(r, 3), "f1": round(f1, 3)}
