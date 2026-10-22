import pytest

from vehiclegpt import analytics


def test_range_formula_and_units():
    assert analytics.estimated_range_km(60, 15) == pytest.approx(400.0)
    assert analytics.estimated_range_km(60, 0) is None
    assert analytics.consumption(3.0, 20.0) == pytest.approx(15.0)


def test_period_stats_match_raw_sql(conn):
    p = analytics.last_n_days(conn, 7)
    st = analytics.period_stats(conn, p)
    d, e = conn.execute("SELECT SUM(distance_km), SUM(energy_kwh) FROM telemetry WHERE vehicle_id='SYN-EV-001' AND state='driving' AND ts>=? AND ts<?",
                        (p.s, p.e)).fetchone()
    assert st.distance_km == pytest.approx(d, abs=0.1)
    assert st.energy_kwh == pytest.approx(e, abs=0.01)
    assert st.consumption_kwh_per_100km == pytest.approx(e / d * 100, abs=0.01)


def test_metric_stat_excludes_invalid(conn):
    p = analytics.day(conn, "2026-09-13")
    r = analytics.metric_stat(conn, "battery_temp_c", p, "max")
    assert r["value"] < 90 and r["excluded_invalid"] == 1


def test_repeated_dtcs(conn):
    rep = analytics.repeated_dtcs(conn, None, 2)
    assert rep.iloc[0]["code"] == "SIM-TPMS-L01" and rep.iloc[0]["count"] == 4


def test_gap_detection(conn):
    from vehiclegpt import db
    df = db.read_telemetry(conn, "2026-09-08", "2026-09-09")
    gaps = analytics.data_gaps(df, 30)
    assert len(gaps) == 1 and gaps[0]["minutes"] == pytest.approx(190)
