import json

from vehiclegpt import config, data_gen, db


def test_reproducible(tmp_path):
    a = data_gen.generate(tmp_path / "a.db", seed=42)
    b = data_gen.generate(tmp_path / "b.db", seed=42)
    assert a == b
    ca, cb = db.connect(tmp_path / "a.db"), db.connect(tmp_path / "b.db")
    q = "SELECT ts, battery_temp_c, soc_pct, energy_kwh FROM telemetry ORDER BY id"
    assert ca.execute(q).fetchall() == cb.execute(q).fetchall()


def test_all_records_synthetic_and_referenced(conn):
    assert conn.execute("SELECT COUNT(*) FROM telemetry WHERE vehicle_id='SYN-EV-001' AND synthetic != 1 OR source_ref IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM diagnostic_events WHERE vehicle_id='SYN-EV-001' AND code NOT LIKE 'SIM-%'").fetchone()[0] == 0


def test_schema_tables_and_indexes(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    for t in ["vehicles", "telemetry", "diagnostic_events", "alerts", "simulation_runs", "idx_tel_ts", "idx_dtc_ts"]:
        assert t in names


def test_scenarios_present(conn):
    # implausible spike flagged invalid, gap present, SOC dropout present
    r = conn.execute("SELECT valid, validity_note FROM telemetry WHERE vehicle_id='SYN-EV-001' AND battery_temp_c > 100").fetchall()
    assert len(r) == 1 and r[0][0] == 0 and "implausible" in r[0][1]
    assert conn.execute("SELECT COUNT(*) FROM telemetry WHERE vehicle_id='SYN-EV-001' AND soc_pct IS NULL").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM telemetry WHERE vehicle_id='SYN-EV-001' AND ts >= '2026-09-08T03:00' AND ts < '2026-09-08T06:00'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM diagnostic_events WHERE vehicle_id='SYN-EV-001' AND code='SIM-TPMS-L01'").fetchone()[0] == 4


def test_ground_truth_written(db_path):
    gt = json.loads(config.GROUND_TRUTH_PATH.read_text())
    assert len(gt["events"]) == 8 and gt["expected_answers"]
