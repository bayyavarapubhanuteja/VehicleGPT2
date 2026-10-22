import socket

import pytest

from vehiclegpt import llm, query_engine as qe


class FakeLLM(llm.BaseAdapter):
    def __init__(self, reply):
        self.reply = reply

    def status(self):
        return llm.LLMStatus("fake", "fake", True, "ready", 0)

    def generate(self, prompt, max_tokens=160):
        return self.reply


def test_grounding_rejects_invented_numbers():
    facts = "- Range: 354.0 km\n- New range: 334.0 km"
    assert llm.explain(facts, "driver", FakeLLM("Range fell from 354 km to 334 km."))[1] == "llm"
    assert llm.explain(facts, "driver", FakeLLM("Range fell to 250 km due to a failing cell."))[1] == "rejected_ungrounded"


def test_null_adapter_fallback():
    assert llm.explain("x", "driver", llm.NullAdapter())[0] is None


def test_ollama_rejects_remote_url():
    with pytest.raises(ValueError):
        llm.OllamaAdapter(url="http://example.com:11434")


@pytest.fixture()
def no_network(monkeypatch):
    def guard(*a, **k):
        raise OSError("network disabled by test")
    monkeypatch.setattr(socket, "create_connection", guard)
    real_connect = socket.socket.connect

    def connect(self, addr):
        if isinstance(addr, tuple) and addr[0] not in ("127.0.0.1", "localhost", "::1"):
            raise OSError("network disabled by test")
        return real_connect(self, addr)
    monkeypatch.setattr(socket.socket, "connect", connect)


def test_core_workflows_offline(conn, no_network):
    from vehiclegpt import anomaly, detective, simulator, runtime
    assert runtime.network_available() in (False, None)
    for q in ["Why did my driving range decrease over the last seven days?", "Which diagnostic warnings occurred repeatedly?"]:
        assert qe.answer(conn, q).supported
    assert anomaly.run_detection(conn, persist=False)
    k = detective.list_events(conn)["key"].iloc[0]
    assert detective.investigate(conn, k)["timeline"] is not None
    b = simulator.baseline(conn)
    assert simulator.simulate(b, simulator.Params(consumption_change_pct=10))["range"]["simulated"] > 0
