import pytest

from vehiclegpt import query_engine as qe

CASES = [
    ("Why did my driving range decrease over the last seven days?", "range_change"),
    ("What happened before the battery temperature warning?", "before_warning"),
    ("Why did my driving range decrease this week?", "range_change"),
    ("Show unusual events from yesterday.", "unusual_events"),
    ("Compare this week's energy consumption with last week.", "compare_energy"),
    ("Which diagnostic warnings occurred repeatedly?", "repeated_dtc"),
    ("What was the highest motor temperature on Sep 19?", "metric_stat"),
    ("How far did I drive last week?", "distance"),
    ("Any alerts in the last 3 days?", "alerts_summary"),
    ("What is the brake pad wear?", "unsupported"),
]


@pytest.mark.parametrize("q,intent", CASES)
def test_intent(conn, q, intent):
    a = qe.answer(conn, q, use_llm=False)
    assert a.intent == intent
    assert a.driver and a.engineer


def test_range_answer_matches_expected(conn):
    a = qe.answer(conn, CASES[0][0], use_llm=False)
    f = {x["label"]: x["value"] for x in a.facts}
    assert f["Range change"] < 0
    assert "decreased" in a.driver and "tyre" in a.driver
    assert a.limitations  # range is labelled as estimate


def test_before_warning_answer(conn):
    a = qe.answer(conn, CASES[1][0], use_llm=False)
    assert "2026-09-11" in a.driver and "Charging started" in a.driver and a.safety


def test_yesterday(conn):
    a = qe.answer(conn, CASES[3][0], use_llm=False)
    assert a.periods[0]["start"].startswith("2026-09-20") and "SIM-TPMS-L01" in a.driver


def test_repeated(conn):
    a = qe.answer(conn, CASES[5][0], use_llm=False)
    assert "4 times" in a.driver


def test_metric_value(conn):
    a = qe.answer(conn, CASES[6][0], use_llm=False)
    v = a.facts[0]["value"]
    m = conn.execute("SELECT MAX(motor_temp_c) FROM telemetry WHERE vehicle_id='SYN-EV-001' AND valid=1 AND ts>='2026-09-19' AND ts<'2026-09-20'").fetchone()[0]
    assert v == pytest.approx(m, abs=0.01)


def test_no_fabrication_for_missing_data(conn):
    for q in ["What is the brake pad wear?", "What was the oil level yesterday?", "What is my tyre tread depth?"]:
        a = qe.answer(conn, q, use_llm=False)
        assert not a.supported and "does not include" in a.driver and not a.facts


def test_out_of_range_period(conn):
    a = qe.answer(conn, "Show unusual events on 2026-06-01", use_llm=False)
    assert not a.supported and any("No telemetry" in l for l in a.limitations)


def test_persona_same_numbers(conn):
    a = qe.answer(conn, CASES[4][0], use_llm=False)
    import re
    cons = [str(x["value"]) for x in a.facts if "Consumption (" in x["label"]]
    for c in cons:
        assert c in a.engineer
    assert f"{float(cons[0]):.1f}" in a.driver


def test_evidence_integrity(conn):
    a = qe.answer(conn, CASES[5][0], use_llm=False)
    for e in a.evidence:
        r = conn.execute("SELECT ts, code FROM diagnostic_events WHERE vehicle_id='SYN-EV-001' AND id=?", (e["id"],)).fetchone()
        assert r and r[0] == e["ts"] and r[1] in e["text"]


def test_followup_with_context(conn):
    i = conn.execute("SELECT id FROM diagnostic_events WHERE vehicle_id='SYN-EV-001' AND code='SIM-BMS-T01'").fetchone()[0]
    k = f"dtc:{i}"
    a = qe.answer(conn, "Why did this happen?", use_llm=False, context_event=k)
    assert a.intent == "event_followup" and "Possible reasons" in a.driver
