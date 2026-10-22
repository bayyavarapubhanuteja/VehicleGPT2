from vehiclegpt import detective


def _key(conn, code):
    r = conn.execute("SELECT id FROM diagnostic_events WHERE vehicle_id='SYN-EV-001' AND code=?", (code,)).fetchone()[0]
    return f"dtc:{r}"


def test_battery_warning_reconstruction(conn):
    inv = detective.investigate(conn, _key(conn, "SIM-BMS-T01"))
    texts = " ".join(t["text"] for t in inv["timeline"])
    assert "Charging started" in texts and "Driving started" in texts
    assert inv["possible_explanations"] and inv["possible_explanations"][0]["support"] == "strong"
    assert any("not causation" in l for l in inv["limitations"])
    ts = [t["ts"] for t in inv["timeline"]]
    assert ts == sorted(ts)


def test_evidence_ids_exist(conn):
    inv = detective.investigate(conn, _key(conn, "SIM-THM-P02"))
    ids = inv["evidence_record_ids"]
    n = conn.execute(f"SELECT COUNT(*) FROM telemetry WHERE vehicle_id='SYN-EV-001' AND id IN ({','.join('?'*len(ids))})", ids).fetchone()[0]
    assert n == len(ids) > 0
    for h in inv["possible_explanations"]:
        for i in h["evidence"]:
            assert conn.execute("SELECT 1 FROM telemetry WHERE vehicle_id='SYN-EV-001' AND id=? UNION SELECT 1 FROM diagnostic_events WHERE vehicle_id='SYN-EV-001' AND id=?",
                                (i, i)).fetchone()


def test_insufficient_evidence(conn):
    inv = detective.investigate(conn, _key(conn, "SIM-INF-U03"))
    assert inv["possible_explanations"] == []
    assert any("Insufficient evidence" in l for l in inv["limitations"])
