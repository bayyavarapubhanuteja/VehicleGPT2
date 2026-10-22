import json
import shutil

import can
import pytest

from vehiclegpt import db, runtime
from vehiclegpt.ingest import (Signal, camera_meta, can_source, diag_log, from_epoch, qnx_slog, replay, retention, someip,
                               to_epoch, vhal)
from vehiclegpt.ingest.collector import Collector

ROW = {"state": "driving", "soc_pct": 77.7, "battery_temp_c": 31.4, "power_kw": 22.5, "speed_kmh": 88.2, "motor_temp_c": 61.0,
       "odometer_km": 12500.25, "ambient_temp_c": 12.3, "tire_pressure_kpa": 238.5, "_epoch": to_epoch("2026-09-19T10:00:00")}


@pytest.fixture()
def scratch_db(db_path, tmp_path):
    p = tmp_path / "ingest.db"
    shutil.copy(db_path, p)
    c = db.connect(p)
    yield c
    c.close()


def test_epoch_roundtrip_is_timezone_free():
    assert from_epoch(to_epoch("2026-09-19T10:00:00")) == "2026-09-19T10:00:00"


def test_can_dbc_roundtrip_within_resolution():
    sigs = []
    for m in can_source.encode_row(ROW):
        sigs += can_source.decode(m, "V", "2026-09-19T10:00:00")
    d = {s.name: s.value for s in sigs}
    assert d["state"] == "driving" and abs(d["battery_temp_c"] - 31.4) <= 0.2 and abs(d["speed_kmh"] - 88.2) < 0.01
    assert abs(d["odometer_km"] - 12500.25) < 0.01 and abs(d["soc_pct"] - 77.7) <= 0.1
    assert all(s.ref.startswith("CAN 0x") for s in sigs)


def test_canfd_adas_frame():
    m = can_source.encode_adas("LKA", "disengaged", "lane_markings_lost", 12, ROW["_epoch"])
    assert m.is_fd and len(m.data) == 16
    d = {s.name: s.value for s in can_source.decode(m, "V", "t")}
    assert d["adas_state"] == "disengaged" and d["adas_reason"] == "lane_markings_lost"


def test_unknown_can_id_ignored():
    assert can_source.decode(can.Message(arbitration_id=0x7FF, data=b"\x00" * 8), "V", "t") == []


def test_read_only_bus_cannot_transmit():
    bus = can_source.ReadOnlyBus(can_source.open_bus("ro-test"))
    with pytest.raises(PermissionError):
        bus.send(can.Message(arbitration_id=0x101, data=b"\x00" * 8))
    bus.shutdown()


def test_someip_roundtrip_and_validation():
    d = someip.encode(ROW["_epoch"], ROW, 7)
    sigs = someip.decode(d, "V")
    assert {s.name for s in sigs} >= {"speed_kmh", "battery_temp_c"} and sigs[0].ts == "2026-09-19T10:00:00"
    assert "0x4C01.0x8001 s7" in sigs[0].ref
    with pytest.raises(ValueError):
        someip.decode(d[:10], "V")
    with pytest.raises(ValueError):
        someip.decode(b"\x12\x34" + d[2:], "V")  # wrong service id


def test_someip_over_loopback_udp():
    a, b = someip.UdpEndpoint(port=0), someip.UdpEndpoint(port=0)
    assert a.addr[0] == "127.0.0.1"
    a.send(someip.encode(ROW["_epoch"], ROW), b.addr)
    assert someip.decode(b.recv(), "V")
    a.close(); b.close()


def test_vhal_parsing_units_and_errors():
    lines = [vhal.to_line(0x11600308, 60000, ROW["_epoch"]), vhal.to_line(0x11600309, 30000, ROW["_epoch"]),
             vhal.to_line(0x11600207, 25.0, ROW["_epoch"]), "not json", vhal.to_line(0x99999999, 1, ROW["_epoch"])]
    sigs, err = vhal.parse_lines(lines, "V")
    d = {s.name: s.value for s in sigs}
    assert d["speed_kmh"] == 90.0 and d["soc_pct"] == 50.0 and err == 1


def test_qnx_slog_parsing():
    lines = ["Sep 19 10:20:00.123 thermal_mgr.4021 3 0 SIM-THM-P02 coolant flow low (synthetic)",
             "Sep 19 10:21:00.000 io-pkt.12 6 0 link up", "garbage"]
    ev, skipped, err = qnx_slog.parse(lines, "V", 2026)
    assert len(ev) == 1 and ev[0].fields["code"] == "SIM-THM-P02" and ev[0].fields["subsystem"] == "thermal"
    assert ev[0].ts == "2026-09-19T10:20:00" and skipped == 2


def test_uds_dtc_parsing():
    line = diag_log.encode("2026-09-19T10:20:00", "SIM-THM-P02")
    ev, err = diag_log.parse([line, "2026-09-19T10:20:00 7E8 7F 19 31", "bad"], "V")
    assert ev[0].fields["code"] == "SIM-THM-P02" and err == 2
    ev, _ = diag_log.parse(["2026-09-19T10:20:00 7E8 59 02 FF 12 34 56 09"], "V")
    assert ev[0].fields["code"] == "RAW-123456" and "no description" in ev[0].fields["description"]


def test_camera_metadata_blockage_event_no_images():
    lines = [json.dumps({"ts": f"2026-09-20T10:{m:02d}:{s:02d}", "camera": "front", "fps": 30, "blockage_pct": 90})
             for m in (12, 13) for s in range(0, 60, 5)] + [json.dumps({"ts": "2026-09-20T10:14:00", "blockage_pct": 5})]
    sigs, ev, err = camera_meta.parse(lines, "V")
    assert len(ev) == 1 and ev[0].fields["event"] == "camera_blocked" and ev[0].fields["duration_s"] >= 30
    assert all(isinstance(s.value, float) for s in sigs)  # numeric metadata only


def test_collector_aggregation_idempotence_and_future_guard(scratch_db):
    replay.ensure_bench_vehicle(scratch_db)
    col = Collector(scratch_db, "SYN-LAB-01")
    col.max_ts = __import__("datetime").datetime(2026, 9, 22)
    sigs = [Signal(f"2026-09-01T00:0{i}:00", "SYN-LAB-01", "speed_kmh", 50.0 + i, "km/h", "can", "r") for i in range(5)]
    sigs += [Signal("2030-01-01T00:00:00", "SYN-LAB-01", "speed_kmh", 1.0, "km/h", "can", "r"),
             Signal("2026-09-01T00:01:00", "OTHER", "speed_kmh", 1.0, "km/h", "can", "r")]
    col.add_signals(sigs, "can")
    assert col.flush(force=True) == 1
    r = scratch_db.execute("SELECT speed_kmh, source_ref FROM telemetry WHERE vehicle_id='SYN-LAB-01'").fetchone()
    assert r[0] == 52.0 and r[1].startswith("INGEST/can/")
    assert col.stats["can"]["errors"] == 2
    col.add_signals(sigs[:2], "can")
    assert col.flush(force=True) == 0  # duplicate bucket not overwritten


def test_replay_end_to_end(scratch_db):
    before_now = scratch_db.execute("SELECT MAX(ts) FROM telemetry").fetchone()[0]
    r = replay.run(scratch_db, "SYN-EV-001", "2026-09-19T08:00:00", 4)
    assert r["telemetry_rows_written"] >= 20 and r["alerts"] >= 1
    src = {p["source"]: p for p in r["per_source"]}
    assert src["can"]["frames"] > 0 and src["someip"]["frames"] > 0 and src["vhal"]["frames"] > 0
    assert src["qnx_slog"]["rows"] == 2
    assert scratch_db.execute("SELECT MAX(ts) FROM telemetry").fetchone()[0] == before_now  # 'now' not shifted
    rules = {x[0] for x in scratch_db.execute("SELECT rule_id FROM alerts WHERE vehicle_id='SYN-LAB-01'")}
    assert "R-MOTOR-ROC" in rules  # the recorded fault is re-detected from ingested frames
    ing = db.read_telemetry(scratch_db, "2026-09-19T10:20", "2026-09-19T10:40", "SYN-LAB-01")
    src_ = db.read_telemetry(scratch_db, "2026-09-19T10:20", "2026-09-19T10:40", "SYN-EV-001")
    assert abs(ing["motor_temp_c"].mean() - src_["motor_temp_c"].mean()) < 8


def test_replay_adas_window(scratch_db):
    r = replay.run(scratch_db, "SYN-EV-001", "2026-09-20T09:00:00", 3)
    n = scratch_db.execute("SELECT COUNT(*) FROM adas_events WHERE vehicle_id='SYN-LAB-01'").fetchone()[0]
    assert n >= 2 and r["can_frames_sent"] > 0


def test_retention_dry_run_and_apply(scratch_db):
    n0 = scratch_db.execute("SELECT COUNT(*) FROM telemetry WHERE vehicle_id='SYN-EV-002'").fetchone()[0]
    prev = retention.apply_retention(scratch_db, "SYN-EV-002", raw_days=30)
    assert prev["dry_run"] and prev["rows_older_than_cutoff"] > 0
    assert scratch_db.execute("SELECT COUNT(*) FROM telemetry WHERE vehicle_id='SYN-EV-002'").fetchone()[0] == n0
    retention.apply_retention(scratch_db, "SYN-EV-002", raw_days=30, dry_run=False)
    assert scratch_db.execute("SELECT COUNT(*) FROM telemetry WHERE vehicle_id='SYN-EV-002'").fetchone()[0] < n0
    assert scratch_db.execute("SELECT COUNT(*) FROM telemetry_rollup_hourly WHERE vehicle_id='SYN-EV-002'").fetchone()[0] > 0
    assert scratch_db.execute("SELECT COUNT(*) FROM data_transformations WHERE vehicle_id='SYN-EV-002'").fetchone()[0] == 1


def test_history_covers_eight_weeks(conn):
    est = retention.storage_estimate(conn)
    assert est["history_weeks"] >= 7.8
