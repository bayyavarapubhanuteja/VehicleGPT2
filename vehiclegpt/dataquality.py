"""Sensor & data-quality checks. Read-only: source records are never modified or deleted here."""
from __future__ import annotations

import pandas as pd

from . import analytics, config, db

KEY_CHANNELS = ["soc_pct", "battery_temp_c", "motor_temp_c", "ambient_temp_c", "speed_kmh", "tire_pressure_kpa"]


def _runs(mask: pd.Series) -> list[tuple[int, int]]:
    out, start = [], None
    m = mask.to_numpy()
    for i, v in enumerate(m):
        if v and start is None:
            start = i
        if not v and start is not None:
            out.append((start, i - 1)); start = None
    if start is not None:
        out.append((start, len(m) - 1))
    return out


def missing_values(df: pd.DataFrame) -> dict:
    return {c: int(df[c].isna().sum()) for c in KEY_CHANNELS}


def duplicate_timestamps(df: pd.DataFrame) -> pd.DataFrame:
    d = df[df.duplicated("ts", keep=False)]
    return d[["id", "ts", "source_ref", "state", "battery_temp_c", "motor_temp_c"]]


def implausible(df: pd.DataFrame) -> pd.DataFrame:
    return df[df["valid"] == 0][["id", "ts", "validity_note", "battery_temp_c", "ambient_temp_c", "source_ref"]]


def unit_suspects(df: pd.DataFrame, jump_c: float = 15.0) -> list[dict]:
    """Ambient temperature step changes > jump_c between consecutive samples (≤15 min apart) that
    persist, with values consistent with °F (x·9/5+32) — a likely unit inconsistency."""
    d = df.sort_values("ts").reset_index(drop=True)
    dt = d["ts"].diff().dt.total_seconds() / 60
    step = d["ambient_temp_c"].diff()
    ups = d.index[(step > jump_c) & (dt <= 15)].tolist()
    out = []
    for i in ups:
        downs = d.index[(d.index > i) & (step < -jump_c)]
        j = int(downs[0]) - 1 if len(downs) else len(d) - 1
        seg = d.iloc[i:j + 1]
        before = d["ambient_temp_c"].iloc[max(i - 3, 0):i].mean()
        out.append({"start": seg["ts"].iloc[0].isoformat(), "end": seg["ts"].iloc[-1].isoformat(), "samples": len(seg),
                    "channel": "ambient_temp_c", "median_in_segment": round(float(seg["ambient_temp_c"].median()), 1),
                    "median_before": round(float(before), 1),
                    "fahrenheit_if_converted_c": round((float(seg["ambient_temp_c"].median()) - 32) * 5 / 9, 1),
                    "record_ids": seg["id"].tolist()})
    return out


def flatlines(df: pd.DataFrame, min_samples: int = 12) -> list[dict]:
    """A channel reporting an identical value for ≥ min_samples consecutive samples while the vehicle is driving."""
    out = []
    d = df.sort_values("ts").reset_index(drop=True)
    for ch in ["motor_temp_c", "battery_temp_c"]:
        same = d[ch].diff().eq(0)
        for a, b in _runs(same):
            seg = d.iloc[max(a - 1, 0):b + 1]
            if len(seg) >= min_samples and (seg["state"] == "driving").any():
                out.append({"channel": ch, "start": seg["ts"].iloc[0].isoformat(), "end": seg["ts"].iloc[-1].isoformat(),
                            "samples": len(seg), "value": float(seg[ch].iloc[0]),
                            "driving_samples": int((seg["state"] == "driving").sum()), "record_ids": seg["id"].tolist()})
    return out


def staleness(conn, vehicle_id: str, stale_hours: float = 24.0) -> dict:
    _, hi = db.data_bounds(conn, vehicle_id)
    now = analytics.reference_now(conn)
    age_h = (now - hi).total_seconds() / 3600
    return {"last_record": hi.isoformat(), "reference_now": now.isoformat(), "age_hours": round(age_h, 1),
            "stale": age_h > stale_hours}


def report(conn, vehicle_id: str) -> dict:
    df = db.read_telemetry(conn, vehicle_id=vehicle_id)
    th = db.get_thresholds(conn)
    lo, hi = db.data_bounds(conn, vehicle_id)
    expected = int((hi - lo) / pd.Timedelta(minutes=config.SAMPLE_MINUTES)) + 1
    gaps = analytics.data_gaps(df.drop_duplicates("ts"), th["gap_minutes"])
    dup = duplicate_timestamps(df)
    imp = implausible(df)
    units = unit_suspects(df)
    flat = flatlines(df)
    stale = staleness(conn, vehicle_id)
    miss = missing_values(df)
    issues = len(gaps) + (1 if len(dup) else 0) + (1 if len(imp) else 0) + len(units) + len(flat) + int(stale["stale"]) + \
        sum(1 for v in miss.values() if v)
    return {
        "vehicle_id": vehicle_id, "rows": len(df), "expected_rows": expected,
        "completeness_pct": round(100 * min(len(df.drop_duplicates('ts')), expected) / expected, 1) if expected else 0,
        "missing_values": miss, "gaps": gaps, "duplicates": dup, "implausible": imp,
        "unit_suspects": units, "flatlines": flat, "staleness": stale, "issue_count": issues,
        "note": "Read-only analysis. Source records are preserved; flagged rows are excluded from analytics, not deleted.",
    }
