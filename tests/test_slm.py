"""Real on-device model tests — skipped automatically when the GGUF / llama.cpp runtime is absent."""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODEL = ROOT / "models" / "qwen2.5-1.5b-instruct-q4_k_m.gguf"
pytestmark = pytest.mark.skipif(not MODEL.exists() or os.environ.get("VEHICLEGPT_SKIP_SLM") == "1",
                                reason="local model not installed")


@pytest.fixture(scope="module")
def adapter():
    pytest.importorskip("llama_cpp")
    from vehiclegpt import config, llm
    config.LLM_THREADS = 2
    ad = llm.LlamaCppAdapter(str(MODEL))
    assert ad.status().available
    return ad


@pytest.mark.parametrize("q,intent", [("How come I can't go as far on a full charge lately?", "range_change"),
                                      ("Did the lane assist give up on me yesterday?", "adas_events"),
                                      ("Any trouble codes that keep coming back?", "repeated_dtc")])
def test_slm_routes_paraphrases(adapter, q, intent):
    from vehiclegpt import llm
    assert llm.classify(q, adapter) == intent


def test_slm_explanation_is_grounded_or_rejected(adapter, conn):
    from vehiclegpt import llm, query_engine as qe
    a = qe.answer(conn, "Which diagnostic warnings occurred repeatedly?", use_llm=False)
    facts = "\n".join(f"- {f['label']}: {f['value']} {f['unit']}" for f in a.facts) + "\n" + a.engineer
    text, status = llm.explain(facts, "driver", adapter)
    assert status in ("llm", "rejected_ungrounded")
    if text:
        assert llm.numbers_grounded(text, facts)


def test_slm_memory_budget(adapter):
    import psutil
    assert psutil.Process().memory_info().rss < 2 * 1024 ** 3
