import pytest

from vehiclegpt import simulator

BASE = {"usable_kwh": 60.0, "consumption_kwh_per_100km": 15.0, "avg_speed_kmh": 50.0, "ambient_c": 21.0,
        "peak_battery_temp_c": 45.0, "peak_ambient_c": 30.0}


def test_identity():
    r = simulator.simulate(BASE, simulator.Params())
    assert r["range"]["baseline"] == r["range"]["simulated"] == pytest.approx(400.0)


def test_consumption_plus_10pct():
    r = simulator.simulate(BASE, simulator.Params(consumption_change_pct=10))
    assert r["range"]["simulated"] == pytest.approx(400 / 1.1, abs=0.1)
    assert r["range"]["pct"] == pytest.approx(-9.1, abs=0.1)


def test_speed_model():
    r = simulator.simulate(BASE, simulator.Params(speed_change_pct=20))
    f = 0.6 + 0.4 * 1.2 ** 2
    assert r["consumption"]["simulated"] == pytest.approx(15 * f, abs=0.01)


def test_miles_conversion():
    r = simulator.simulate(BASE, simulator.Params(unit="mi"))
    assert r["range"]["baseline"] == pytest.approx(400 / 1.609344, abs=0.1)


def test_cooling_model():
    r = simulator.simulate(BASE, simulator.Params(cooling_improvement_pct=20))
    assert r["thermal"]["sim_peak_c"] == pytest.approx(30 + 15 * 0.8, abs=0.05)


def test_validation():
    with pytest.raises(ValueError):
        simulator.simulate(BASE, simulator.Params(consumption_change_pct=500))
    assert simulator.Params(unit="ft").validate()


def test_history_preserved_and_runs_saved(conn):
    before = conn.execute("SELECT COUNT(*), SUM(energy_kwh) FROM telemetry").fetchone()
    b = simulator.baseline(conn)
    p = simulator.Params(consumption_change_pct=10)
    rid = simulator.save_run(conn, "t", b, p, simulator.simulate(b, p))
    assert conn.execute("SELECT COUNT(*), SUM(energy_kwh) FROM telemetry").fetchone() == before
    assert any(r["id"] == rid for r in simulator.list_runs(conn))
    assert "HYPOTHETICAL" in simulator.simulate(b, p)["label"]
