import json

import pandas as pd
import pytest

from vehiclegpt import analytics, data_gen, db, dataquality, health, maintenance, migrations, reports, runtime, services as S


def test_migration_preserves_v1_database(tmp_path):
    p = tmp_path / "v1.db"
    data_gen.generate(p, fleet=data_gen.FLEET[:1])
    c = db.connect(p)
    c.execute("DROP TABLE users")  # simulate a v1 database (no v2 tables)
    c.execute("DROP TABLE schema_version")
    c.commit()
    before = c.execute("SELECT COUNT(*), SUM(energy_kwh) FROM telemetry").fetchone()
    c.close()
    info = runtime.ensure_database(p)
    c = db.connect(p)
    assert info["migrated_to"] == migrations.CURRENT_VERSION
    assert tuple(c.execute("SELECT COUNT(*), SUM(energy_kwh) FROM telemetry WHERE vehicle_id='SYN-EV-001'").fetchone()) == tuple(before)
    assert set(db.vehicle_ids(c)) == {"SYN-EV-001", "SYN-EV-002", "SYN-EV-003"}
    assert runtime.ensure_database(p)["vehicles_added"] == []  # idempotent


def test_alerts_carry_rule_version(conn):
    assert conn.execute("SELECT COUNT(*) FROM alerts WHERE rule_version IS NULL").fetchone()[0] == 0
    assert conn.execute("SELECT rule_version FROM alerts LIMIT 1").fetchone()[0].count("/") == 2


def test_health_score_explainable(conn, alice):
    h = S.health_score(conn, alice, "SYN-EV-001")
    assert 0 <= h["score"] <= 100
    assert h["score"] == max(0, round(100 + sum(f["points"] for f in h["factors"])))
    assert any("not a manufacturer" in l for l in h["limitations"])


def test_health_low_confidence_when_stale(conn, rao):
    assert S.health_score(conn, rao, "SYN-EV-003")["confidence"] == "low"


def test_data_quality_detects_injected_problems(conn):
    r = dataquality.report(conn, "SYN-EV-003")
    assert len(r["duplicates"]) == 10 and r["unit_suspects"] and r["flatlines"] and r["staleness"]["stale"]
    assert conn.execute("SELECT COUNT(*) FROM telemetry WHERE vehicle_id='SYN-EV-003' AND source_ref LIKE '%-DUP'").fetchone()[0] == 5


def test_fleet_repeated_temperature_query(conn, chen):
    a = S.ask(conn, chen, "SYN-EV-001", "Which vehicles experienced repeated temperature anomalies?")
    rows = {r["vehicle_id"]: r for r in a.table}
    assert set(rows) == {"SYN-EV-001", "SYN-EV-002"}  # only authorised vehicles
    assert rows["SYN-EV-002"]["repeated"] and rows["SYN-EV-002"]["temperature_alerts"] == 3


@pytest.mark.parametrize("q,intent", [
    ("Show diagnostic events associated with unusually high energy consumption.", "dtc_energy"),
    ("Compare sensor readings before and after a warning.", "before_after"),
    ("Identify missing or implausible telemetry.", "data_quality"),
    ("Summarize the latest diagnostic events for this vehicle", "latest_dtcs"),
    ("Is my energy consumption higher than normal?", "consumption_normal"),
    ("Explain this diagnostic warning in simple language.", "explain_dtc"),
    ("What happened before the latest warning?", "before_warning"),
])
def test_new_intents(conn, chen, q, intent):
    a = S.ask(conn, chen, "SYN-EV-001", q)
    assert a.intent == intent and a.supported and a.structured_query["intent"] == intent


def test_maintenance_crud_and_ownership(conn, alice, bob, chen):
    rid = S.maintenance_add(conn, alice, "SYN-EV-001", title="Rotate tyres", category="tyres", due_date="2026-09-10",
                            interval_days=90)
    df = S.maintenance_list(conn, alice, "SYN-EV-001")
    assert df.set_index("id").loc[rid, "due_state"] == "overdue"
    with pytest.raises(S.PermissionDenied):
        S.maintenance_complete(conn, bob, rid)
    with pytest.raises(S.PermissionDenied):
        S.maintenance_list(conn, chen, "SYN-EV-001")
    nxt = S.maintenance_complete(conn, alice, rid)
    assert nxt and S.maintenance_list(conn, alice, "SYN-EV-001").set_index("id").loc[rid, "status"] == "done"
    sid = S.maintenance_suggest(conn, alice, "SYN-EV-001", 0)
    assert maintenance.get(conn, sid)["source"] == "generic_suggestion"
    with pytest.raises(ValueError):
        S.maintenance_add(conn, alice, "SYN-EV-001", title="", category="tyres", due_date="2026-10-01")
    with pytest.raises(ValueError):
        S.maintenance_add(conn, alice, "SYN-EV-001", title="x", category="tyres")


def test_case_workflow_requires_review_for_root_cause(conn, chen):
    k = "dtc:" + str(conn.execute("SELECT id FROM diagnostic_events WHERE vehicle_id='SYN-EV-001' AND code='SIM-BMS-T01'").fetchone()[0])
    cid = S.case_create(conn, chen, "SYN-EV-001", "Pack overheating", "High", k)
    hy = S.case_hypotheses(conn, chen, cid)
    assert len(hy) >= 1 and set(hy["status"]) == {"candidate"}
    assert S.case_get(conn, chen, cid)["root_cause_status"] == "none"
    with pytest.raises(ValueError):
        S.case_review_hypothesis(conn, chen, int(hy.id[0]), "confirmed", "too short")
    hid = S.case_add_hypothesis(conn, chen, cid, "No evidence idea", "weak", [])
    with pytest.raises(ValueError):
        S.case_review_hypothesis(conn, chen, hid, "confirmed", "A long enough justification text here.")
    S.case_review_hypothesis(conn, chen, int(hy.id[0]), "confirmed", "Charger log confirms 110 kW for 40 minutes.")
    assert S.case_get(conn, chen, cid)["root_cause_status"] == "confirmed"
    with pytest.raises(ValueError):
        S.case_update(conn, chen, cid, status="resolved")  # resolution summary required
    S.case_update(conn, chen, cid, status="resolved", resolution="Limited DC charge power; monitoring.")
    S.case_note(conn, chen, cid, "Inspected coolant pump: OK.")
    acts = S.case_audit(conn, chen, cid)["action"].tolist()
    assert {"case.create", "case.update", "case.note", "case.hypothesis_confirmed"} <= set(acts)


def test_reports(conn, alice, chen):
    v = db.get_vehicle(conn, "SYN-EV-001")
    h = S.health_score(conn, alice, "SYN-EV-001")
    pdf = reports.driver_health_pdf(alice, v, h, S.alerts(conn, alice, "SYN-EV-001"), S.efficiency(conn, alice, "SYN-EV-001"))
    assert pdf[:4] == b"%PDF" and len(pdf) > 2000
    cid = S.case_create(conn, chen, "SYN-EV-001", "Report test",
                        event_key="dtc:" + str(conn.execute("SELECT id FROM diagnostic_events WHERE code='SIM-THM-P02'").fetchone()[0]))
    pdf2 = reports.engineer_case_pdf(chen, v, S.case_report(conn, chen, cid), pd.DataFrame({"rule": ["R"], "version": ["1"]}))
    assert pdf2[:4] == b"%PDF"
    csv = reports.to_csv(S.alerts(conn, alice, "SYN-EV-001")).decode()
    assert csv.splitlines()[0].startswith("id,") and "SYN-EV-002" not in csv


def test_simulation_never_touches_telemetry(conn, chen):
    before = conn.execute("SELECT COUNT(*), SUM(energy_kwh) FROM telemetry").fetchone()
    b, r = S.simulate(conn, chen, "SYN-EV-001", analytics.last_n_days(conn, 7), __import__("vehiclegpt").simulator.Params(20))
    S.save_simulation(conn, chen, "SYN-EV-001", "x", b, __import__("vehiclegpt").simulator.Params(20), r)
    assert conn.execute("SELECT COUNT(*), SUM(energy_kwh) FROM telemetry").fetchone() == before
    with pytest.raises(S.PermissionDenied):
        S.save_simulation(conn, chen, "SYN-EV-002", "x", b, __import__("vehiclegpt").simulator.Params(20), r)


def test_threshold_validation_and_audit(conn, chen):
    th = S.thresholds(conn, chen)
    with pytest.raises(ValueError):
        S.set_thresholds(conn, chen, dict(th, battery_temp_low_c=50.0))
    with pytest.raises(ValueError):
        S.set_thresholds(conn, chen, dict(th, bogus=1))
    S.set_thresholds(conn, chen, th)
    assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE action='thresholds.update'").fetchone()[0] >= 1


def test_detection_eval_against_labels(conn, chen):
    r = S.evaluate_detection(conn, chen)
    assert r["demo_vehicle"]["recall"] == 1.0 and r["fleet"]["recall"] >= 0.9
