"""Collector: normalised signals -> live ring buffer (real-time) + 10-minute historical rows (SQLite).

Tiering
  live    in-memory ring buffer of the last `live_seconds` of raw signals (answers "what is it now?")
  hot     10-minute telemetry rows in SQLite, kept `raw_days` (default 90) — what analytics read
  cold    hourly roll-ups kept indefinitely (retention.py)
Writes are idempotent (vehicle_id + timestamp); every batch is logged in `ingest_log`.
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
from statistics import mean

import pandas as pd

from .. import config
from ..validation import validate_telemetry
from . import Event, Signal

MEAN_COLS = ["speed_kmh", "battery_temp_c", "motor_temp_c", "ambient_temp_c", "power_kw", "tire_pressure_kpa"]
LAST_COLS = ["soc_pct", "odometer_km"]


class Collector:
    def __init__(self, conn, vehicle_id: str, bucket_minutes: int = config.SAMPLE_MINUTES, live_seconds: int = 3600):
        self.conn, self.vid = conn, vehicle_id
        self.bucket = timedelta(minutes=bucket_minutes)
        self.live = deque(maxlen=200_000)
        self.live_seconds = live_seconds
        self.pending: dict[datetime, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        self.sources: dict[datetime, set] = defaultdict(set)
        self.stats = defaultdict(lambda: {"frames": 0, "rows": 0, "errors": 0})
        self.prev_odo = self._last_odometer()
        self.max_ts: datetime | None = None  # optional upper bound for accepted timestamps

    def _last_odometer(self):
        r = self.conn.execute("SELECT odometer_km FROM telemetry WHERE vehicle_id = ? ORDER BY ts DESC LIMIT 1", (self.vid,)).fetchone()
        return r[0] if r else None

    def _bucket_of(self, ts: datetime) -> datetime:
        m = int(self.bucket.total_seconds() // 60)
        return ts.replace(minute=(ts.minute // m) * m, second=0, microsecond=0)

    # ------------------------------------------------------------------ signals
    def add_signals(self, signals: list[Signal], source: str, errors: int = 0):
        st = self.stats[source]
        st["frames"] += len(signals)
        st["errors"] += errors
        for s in signals:
            if s.vehicle_id != self.vid:
                st["errors"] += 1
                continue
            ts = datetime.fromisoformat(s.ts)
            if self.max_ts and ts > self.max_ts:
                st["errors"] += 1  # reject timestamps from the future (clock faults) — never shift 'now'
                continue
            self.live.append(s)
            b = self._bucket_of(ts)
            self.pending[b][s.name].append(s.value)
            self.sources[b].add(s.source)
        self._trim_live()

    def _trim_live(self):
        if not self.live:
            return
        newest = datetime.fromisoformat(self.live[-1].ts)
        while self.live and (newest - datetime.fromisoformat(self.live[0].ts)).total_seconds() > self.live_seconds:
            self.live.popleft()

    def snapshot(self) -> dict:
        """Latest value per signal in the live buffer (labelled LIVE with its age relative to the newest sample)."""
        latest = {}
        for s in self.live:
            latest[s.name] = s
        newest = max((datetime.fromisoformat(s.ts) for s in latest.values()), default=None)
        return {k: {"value": s.value, "unit": s.unit, "ts": s.ts, "source": s.source, "ref": s.ref,
                    "age_s": (newest - datetime.fromisoformat(s.ts)).total_seconds() if newest else None}
                for k, s in sorted(latest.items())}

    def flush(self, force: bool = False) -> int:
        """Aggregate completed buckets into telemetry rows. Returns rows written."""
        if not self.pending:
            return 0
        newest = max(self.pending)
        written = 0
        for b in sorted(self.pending):
            if b == newest and not force:
                continue
            vals = self.pending.pop(b)
            srcs = self.sources.pop(b)
            row = {c: (round(mean(map(float, vals[c])), 3) if vals.get(c) else None) for c in MEAN_COLS}
            row.update({c: (round(float(vals[c][-1]), 3) if vals.get(c) else None) for c in LAST_COLS})
            states = vals.get("state") or (["driving"] if (row["speed_kmh"] or 0) > 2 else ["parked"])
            state = max(set(states), key=states.count)
            dt_h = self.bucket.total_seconds() / 3600
            if row["odometer_km"] is not None and self.prev_odo is not None:
                dist = max(0.0, row["odometer_km"] - self.prev_odo)
            else:
                dist = (row["speed_kmh"] or 0) * dt_h if state == "driving" else 0.0
            if row["odometer_km"] is not None:
                self.prev_odo = row["odometer_km"]
            energy = max(row["power_kw"] or 0, 0) * dt_h if state == "driving" else 0.0
            ts = b.isoformat(timespec="seconds")
            if self.conn.execute("SELECT 1 FROM telemetry WHERE vehicle_id = ? AND ts = ?", (self.vid, ts)).fetchone():
                self.stats["collector"]["errors"] += 1  # duplicate bucket: keep existing raw row, do not overwrite
                continue
            self.conn.execute(
                "INSERT INTO telemetry (vehicle_id, ts, trip_id, state, speed_kmh, soc_pct, battery_temp_c, motor_temp_c,"
                " ambient_temp_c, power_kw, distance_km, energy_kwh, odometer_km, tire_pressure_kpa, valid, validity_note,"
                " source_ref, synthetic) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,NULL,?,1)",
                (self.vid, ts, None, state, row["speed_kmh"], row["soc_pct"], row["battery_temp_c"], row["motor_temp_c"],
                 row["ambient_temp_c"], row["power_kw"], round(dist, 4), round(energy, 5), row["odometer_km"],
                 row["tire_pressure_kpa"], f"INGEST/{'+'.join(sorted(srcs))}/{ts}"))
            written += 1
        self.conn.commit()
        if written:
            validate_telemetry(self.conn)
        self.stats["collector"]["rows"] += written
        return written

    # ------------------------------------------------------------------ events
    def add_events(self, events: list[Event], source: str, errors: int = 0) -> int:
        st = self.stats[source]
        st["frames"] += len(events)
        st["errors"] += errors
        n = 0
        for e in events:
            if e.vehicle_id != self.vid:
                st["errors"] += 1
                continue
            f = e.fields
            if e.kind == "dtc":
                if self.conn.execute("SELECT 1 FROM diagnostic_events WHERE vehicle_id=? AND ts=? AND code=?",
                                     (self.vid, e.ts, f["code"])).fetchone():
                    continue
                self.conn.execute("INSERT INTO diagnostic_events (vehicle_id, ts, code, subsystem, description, reported_level,"
                                  " source_ref, synthetic) VALUES (?,?,?,?,?,?,?,1)",
                                  (self.vid, e.ts, f["code"], f["subsystem"], f["description"], f["reported_level"], e.ref))
            elif e.kind == "adas":
                if self.conn.execute("SELECT 1 FROM adas_events WHERE vehicle_id=? AND ts=? AND event=? AND feature=?",
                                     (self.vid, e.ts, f["event"], f["feature"])).fetchone():
                    continue
                self.conn.execute("INSERT INTO adas_events (vehicle_id, ts, feature, event, reason, speed_kmh, duration_s,"
                                  " source_ref, synthetic) VALUES (?,?,?,?,?,?,?,?,1)",
                                  (self.vid, e.ts, f["feature"], f["event"], f.get("reason"), f.get("speed_kmh"),
                                   f.get("duration_s"), e.ref))
            n += 1
        st["rows"] += n
        self.conn.commit()
        return n

    def write_log(self) -> pd.DataFrame:
        now = datetime.now().isoformat(timespec="seconds")
        for src, s in self.stats.items():
            self.conn.execute("INSERT INTO ingest_log (ts, source, vehicle_id, frames, rows_written, errors, detail)"
                              " VALUES (?,?,?,?,?,?,?)", (now, src, self.vid, s["frames"], s["rows"], s["errors"], ""))
        self.conn.commit()
        return pd.DataFrame([{"source": k, **v} for k, v in self.stats.items()])
