"""Scenario recreation by replaying recorded data through the real protocol paths.

Recorded 10-minute telemetry (or a simulator such as CARLA via carla_bridge.py) is up-sampled to 1 Hz-style frames and
published on every transport the collector supports, then decoded back into the store for a bench vehicle:

  BMS/MCU/BCM signals  -> CAN frames on a python-can bus (DBC-encoded)
  VehicleState         -> SOME/IP notifications over loopback UDP
  odometer/tyres/temp  -> Android VHAL property lines
  DTCs                 -> QNX slog2 lines + UDS 0x19 responses
  ADAS                 -> CAN-FD ADAS_Status_FD frames; camera health -> metadata lines
This exercises the full decode -> normalise -> aggregate -> validate -> detect pipeline end to end.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import pandas as pd

from .. import anomaly, config, db
from . import Event, from_epoch, to_epoch, can_source, camera_meta, diag_log, qnx_slog, someip, vhal
from .collector import Collector

BENCH_ID = "SYN-LAB-01"


def ensure_bench_vehicle(conn, vehicle_id: str = BENCH_ID):
    if not conn.execute("SELECT 1 FROM vehicles WHERE vehicle_id = ?", (vehicle_id,)).fetchone():
        conn.execute("INSERT INTO vehicles (vehicle_id, name, vehicle_type, usable_battery_kwh, nominal_battery_kwh, synthetic,"
                     " notes, nickname, vin, data_mode) VALUES (?,?,?,?,?,1,?,?,?, 'ingested-replay')",
                     (vehicle_id, "Bench replay unit (synthetic)", "Battery electric (HIL bench)", config.USABLE_BATTERY_KWH,
                      config.NOMINAL_BATTERY_KWH, "Populated only through the ingestion pipeline (CAN/SOME-IP/VHAL/QNX/UDS).",
                      "Lab Bench 1", "SYNVINLAB00000001"))
        conn.commit()


def _interp(rows: pd.DataFrame, step_s: int) -> list[dict]:
    rows = rows.drop_duplicates("ts").set_index("ts").sort_index()
    num = rows.select_dtypes("number")
    idx = pd.date_range(rows.index[0], rows.index[-1] + pd.Timedelta(minutes=config.SAMPLE_MINUTES) - pd.Timedelta(seconds=step_s),
                        freq=f"{step_s}s")
    up = num.reindex(num.index.union(idx)).interpolate("time").reindex(idx).ffill()
    up["state"] = rows["state"].reindex(idx, method="ffill")
    up["_epoch"] = [to_epoch(t) for t in up.index]
    up["_ts"] = up.index
    return up.reset_index(drop=True).to_dict("records")


def run(conn, source_vehicle: str = "SYN-EV-001", start: str = "2026-09-19T08:00:00", hours: float = 4,
        target: str = BENCH_ID, step_s: int = 60, bus_interface: str = "virtual") -> dict:
    t0 = time.perf_counter()
    ensure_bench_vehicle(conn, target)
    end = (pd.Timestamp(start) + pd.Timedelta(hours=hours)).isoformat()
    rec = db.read_telemetry(conn, start, end, source_vehicle, include_invalid=False)
    if rec.empty:
        raise ValueError("No recorded data in that window.")
    samples = _interp(rec, step_s)
    col = Collector(conn, target)

    # --- CAN / CAN-FD over a python-can bus
    # tx = the scenario simulator (stands in for the vehicle's ECUs); rx = VehicleGPT's receive-only tap
    tx = can_source.open_bus("vgpt-replay", bus_interface)
    rx = can_source.ReadOnlyBus(can_source.open_bus("vgpt-replay", bus_interface))
    adas = db.read_adas(conn, start, end, source_vehicle)
    can_frames = 0
    try:
        # A CAN frame carries no timestamp; the receiver stamps it. The virtual bus delivers in FIFO order, so the
        # replay pairs each received frame with the scenario time it was sent for.
        sent_ts = []
        for s in samples:
            for m in can_source.encode_row(s):
                tx.send(m); can_frames += 1; sent_ts.append(s["_epoch"])
        for a in adas[adas["event"] == "disengagement"].itertuples():
            tx.send(can_source.encode_adas(a.feature, "disengaged", a.reason, 0, to_epoch(a.ts)))
            can_frames += 1; sent_ts.append(to_epoch(a.ts))
        sigs, adas_events = [], []
        k = 0
        while (m := rx.recv(timeout=0.2)) is not None:
            ts = from_epoch(sent_ts[k]); k += 1
            dec = can_source.decode(m, target, ts)
            d = {x.name: x.value for x in dec}
            if d.get("adas_state") == "disengaged":
                adas_events.append(Event(ts, target, "adas", {"feature": d["adas_feature"], "event": "disengagement",
                                                              "reason": d["adas_reason"], "speed_kmh": None, "duration_s": None},
                                         "canfd", dec[0].ref))
            else:
                sigs += dec
    finally:
        tx.shutdown(); rx.shutdown()
    col.add_signals([s for s in sigs if s.source == "can"], "can")
    col.add_events(adas_events, "canfd")

    # --- SOME/IP over loopback UDP
    pub, sub = someip.UdpEndpoint(port=0), someip.UdpEndpoint(port=0)
    sip, sip_err = [], 0
    try:
        for i, s in enumerate(samples[::2]):
            pub.send(someip.encode(s["_epoch"], s, i + 1), sub.addr)
            d = sub.recv()
            try:
                sip += someip.decode(d, target) if d else []
            except ValueError:
                sip_err += 1
    finally:
        pub.close(); sub.close()
    col.add_signals(sip, "someip", sip_err)

    # --- Android VHAL property stream
    lines = [vhal.to_line(0x11600308, config.USABLE_BATTERY_KWH * 1000, samples[0]["_epoch"])]
    for s in samples[::5]:
        lines += [vhal.to_line(0x11600207, s["speed_kmh"] / 3.6, s["_epoch"]),
                  vhal.to_line(0x11600703, s["ambient_temp_c"], s["_epoch"]),
                  vhal.to_line(0x15600309, s["tire_pressure_kpa"], s["_epoch"], 1)]
    vs, verr = vhal.parse_lines(lines, target)
    col.add_signals(vs, "vhal", verr)

    # --- Diagnostic events via QNX slog2 and UDS
    dt = db.read_dtcs(conn, start, end, source_vehicle)
    slog = [f"{r.ts:%b %d %H:%M:%S}.000 {r.subsystem}_mgr.4021 {3 if r.reported_level == 'warning' else 6} 0 {r.code} {r.description}"
            for r in dt.itertuples()] + ["Sep 19 10:00:00.000 io-pkt.12 6 0 link up (noise line)"]
    ev, skipped, qerr = qnx_slog.parse(slog, target, pd.Timestamp(start).year)
    col.add_events(ev, "qnx_slog", qerr)
    uds = [diag_log.encode(r.ts.isoformat(), r.code) for r in dt.itertuples() if r.code in diag_log.CODE_TO_DTC]
    ev2, derr = diag_log.parse(uds, target)
    col.add_events(ev2, "diag", derr)

    # --- Camera health metadata
    cam = []
    for a in adas[adas["event"] == "camera_blocked"].itertuples():
        for k in range(int(a.duration_s or 60)):
            cam.append(f'{{"ts": "{(a.ts + timedelta(seconds=k)).isoformat()}", "camera": "front", "fps": 29.9, "blockage_pct": 85}}')
        cam.append(f'{{"ts": "{(a.ts + timedelta(seconds=int(a.duration_s or 60) + 1)).isoformat()}", "camera": "front", "fps": 29.9, "blockage_pct": 3}}')
    cs, cev, cerr = camera_meta.parse(cam, target)
    col.add_signals(cs, "camera", cerr)
    col.add_events(cev, "camera")

    rows = col.flush(force=True)
    log = col.write_log()
    alerts = anomaly.run_detection(conn, vehicle_id=target)
    el = time.perf_counter() - t0
    total_frames = int(log["frames"].sum())
    return {"target": target, "source": source_vehicle, "window": [start, end], "samples": len(samples),
            "can_frames_sent": can_frames, "telemetry_rows_written": rows, "alerts": len(alerts),
            "elapsed_s": round(el, 2), "frames_per_s": round(total_frames / el), "per_source": log.to_dict("records"),
            "live_snapshot": col.snapshot()}
