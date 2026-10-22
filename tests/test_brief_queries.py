"""The five example questions from the challenge brief, plus routing behaviour."""
import pytest

from vehiclegpt import query_engine as qe


def test_range_two_weeks(conn):
    a = qe.answer(conn, "Why has my driving range reduced over the last two weeks?", use_llm=False)
    assert a.intent == "range_change" and a.supported and a.periods[1]["label"] == "last 14 days"


def test_fuel_efficiency_largest_drop(conn):
    a = qe.answer(conn, "When did fuel efficiency show the largest drop?", use_llm=False)
    f = {x["label"]: x["value"] for x in a.facts}
    assert a.intent == "largest_drop" and f["Date of largest drop"] == "2026-09-11" and "electric vehicle" in a.driver


def test_adas_disengagements_yesterday(conn):
    a = qe.answer(conn, "Show all ADAS disengagement events from yesterday.", use_llm=False)
    f = {x["label"]: x["value"] for x in a.facts}
    assert a.intent == "adas_events" and f["ADAS disengagements"] == 2 and f["Camera blockage events"] == 1
    assert all(e["ts"].startswith("2026-09-20") for e in a.evidence)


def test_adas_absent_vehicle(conn):
    a = qe.answer(conn, "Show ADAS disengagements this week", use_llm=False, vehicle_id="SYN-EV-003")
    assert not a.supported and "no ADAS data" in a.driver


def test_battery_overheating(conn):
    a = qe.answer(conn, "What happened before the battery overheating warning?", use_llm=False)
    assert a.intent == "before_warning" and "Charging started" in a.driver


def test_city_vs_highway(conn):
    a = qe.answer(conn, "Compare energy consumption between city and highway driving.", use_llm=False)
    f = {x["label"]: x["value"] for x in a.facts}
    assert a.intent == "city_highway" and f["Highway consumption"] > f["City consumption"]


def test_strong_rule_wins_over_model(monkeypatch):
    from vehiclegpt import llm

    class Fake(llm.BaseAdapter):
        def status(self):
            return llm.LLMStatus("fake", "fake", True, "ready", 0)

        def generate(self, prompt, max_tokens=160, system=None):
            return "compare_energy"
    monkeypatch.setattr(llm, "_adapter", Fake())
    intent, src, _ = qe.route("Compare energy consumption between city and highway driving.")
    assert intent == "city_highway"
    intent, src, _ = qe.route("Is the car using more juice than usual?")
    assert intent == "compare_energy" and src == "llm"   # model label used for paraphrases
    intent, _, _ = qe.route("What is the brake pad wear?")
    assert intent == "unsupported"                         # refusal always enforced
    monkeypatch.setattr(llm, "_adapter", None)
