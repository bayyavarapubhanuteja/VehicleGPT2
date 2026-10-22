import json

from vehiclegpt import anomaly, config, db


def _alerts(conn):
    return anomaly.run_detection(conn, persist=False, vehicle_id="SYN-EV-001")


def test_every_alert_has_required_fields(conn):
    for a in _alerts(conn):
        for k in ["ts", "subsystem", "severity", "rule_id", "message", "evidence_json", "next_step"]:
            assert a[k] not in (None, ""), (k, a)
        assert a["severity"] in config.SEVERITIES
        assert json.loads(a["evidence_json"])


def test_battery_temp_high_severity(conn):
    b = [a for a in _alerts(conn) if a["rule_id"] == "R-BATT-TEMP"]
    assert len(b) == 1 and b[0]["severity"] == "High" and b[0]["ts"].startswith("2026-09-11T13")


def test_severity_follows_thresholds(conn):
    th = db.get_thresholds(conn)
    th2 = dict(th, battery_temp_high_c=60.0, battery_temp_medium_c=55.0)
    db.set_thresholds(conn, th2)
    try:
        b = [a for a in _alerts(conn) if a["rule_id"] == "R-BATT-TEMP"]
        assert b[0]["severity"] == "Low"
    finally:
        db.set_thresholds(conn, th)


def test_implausible_not_counted_as_overheat(conn):
    for a in _alerts(conn):
        if a["rule_id"] == "R-BATT-TEMP":
            assert a["observed_value"] < 90


def test_repeated_dtc_and_motor(conn):
    al = _alerts(conn)
    assert any(a["rule_id"] == "R-DTC-REPEAT" and "SIM-TPMS-L01" in a["message"] for a in al)
    assert any(a["rule_id"] == "R-MOTOR-ROC" and a["ts"].startswith("2026-09-19") for a in al)


def test_precision_recall_against_labels(conn):
    gt = json.loads(config.GROUND_TRUTH_PATH.read_text())
    ev = anomaly.evaluate(_alerts(conn), gt)
    assert ev["recall"] >= 0.85 and ev["precision"] >= 0.8


def test_acknowledge_persists_across_rerun(conn):
    anomaly.run_detection(conn)
    aid = conn.execute("SELECT id FROM alerts WHERE vehicle_id='SYN-EV-001' AND rule_id='R-BATT-TEMP'").fetchone()[0]
    anomaly.acknowledge(conn, aid)
    anomaly.run_detection(conn)
    assert conn.execute("SELECT acknowledged FROM alerts WHERE vehicle_id='SYN-EV-001' AND rule_id='R-BATT-TEMP'").fetchone()[0] == 1
    aid = conn.execute("SELECT id FROM alerts WHERE vehicle_id='SYN-EV-001' AND rule_id='R-BATT-TEMP'").fetchone()[0]
    anomaly.acknowledge(conn, aid, False)
