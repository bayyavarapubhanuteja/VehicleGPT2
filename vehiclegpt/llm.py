"""Replaceable local language-model adapter.

Backends (all local; no cloud APIs):
  OllamaAdapter    — HTTP to an Ollama server on 127.0.0.1 (loopback only)
  LlamaCppAdapter  — in-process llama-cpp-python with a quantised GGUF file
  NullAdapter      — deterministic fallback (no model); the app is fully functional with it

The LLM is used ONLY to (a) map unrecognised questions to a supported intent and
(b) rephrase already-verified facts. Its output is grounding-checked: any number
it emits that is not present in the verified facts causes the text to be discarded.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import config


@dataclass
class LLMStatus:
    backend: str
    model: str | None
    available: bool
    detail: str
    init_ms: float | None = None


class BaseAdapter:
    name = "base"

    def status(self) -> LLMStatus:
        raise NotImplementedError

    def generate(self, prompt: str, max_tokens: int = 160, system: str | None = None) -> str | None:
        return None


class NullAdapter(BaseAdapter):
    name = "none"

    def __init__(self, reason: str = "No local model configured"):
        self.reason = reason

    def status(self):
        return LLMStatus("deterministic-fallback", None, False, self.reason, 0.0)


class OllamaAdapter(BaseAdapter):
    name = "ollama"

    def __init__(self, url: str = config.OLLAMA_URL, model: str = config.OLLAMA_MODEL):
        host = urllib.parse.urlparse(url).hostname
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("Ollama URL must be loopback (offline-only policy).")
        self.url, self.model = url.rstrip("/"), model
        t = time.perf_counter()
        self._ok, self._detail = self._probe()
        self.init_ms = (time.perf_counter() - t) * 1000

    def _probe(self):
        try:
            with urllib.request.urlopen(self.url + "/api/tags", timeout=1.0) as r:
                tags = [m["name"] for m in json.load(r).get("models", [])]
            if self.model in tags:
                return True, "ready"
            return False, f"Ollama running but model '{self.model}' not pulled (have: {', '.join(tags) or 'none'})"
        except Exception as e:  # noqa: BLE001
            return False, f"Ollama not reachable at {self.url} ({type(e).__name__})"

    def status(self):
        return LLMStatus("ollama", self.model, self._ok, self._detail, self.init_ms)

    def generate(self, prompt, max_tokens=160, system=None):
        if not self._ok:
            return None
        body = json.dumps({"model": self.model, "prompt": prompt, "system": system or "", "stream": False,
                           "options": {"temperature": 0.1, "num_predict": max_tokens}}).encode()
        req = urllib.request.Request(self.url + "/api/generate", body, {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=config.LLM_TIMEOUT_S) as r:
                return json.load(r).get("response", "").strip()
        except Exception:  # noqa: BLE001
            return None


class LlamaCppAdapter(BaseAdapter):
    name = "llamacpp"

    def __init__(self, path: str = config.GGUF_PATH):
        from pathlib import Path
        self.path = path
        self._llm, self.init_ms = None, None
        if not Path(path).exists():
            self._detail = f"GGUF model not found at {path}"
            return
        try:
            from llama_cpp import Llama  # type: ignore
        except ImportError:
            self._detail = "llama-cpp-python not installed"
            return
        t = time.perf_counter()
        self._llm = Llama(model_path=path, n_ctx=config.LLM_CTX, n_threads=config.LLM_THREADS, n_batch=256,
                          use_mmap=True, verbose=False)
        self.init_ms = (time.perf_counter() - t) * 1000
        self._detail = "ready"

    def status(self):
        return LLMStatus("llama.cpp", self.path, self._llm is not None, self._detail, self.init_ms)

    def generate(self, prompt, max_tokens=160, system: str | None = None):
        if self._llm is None:
            return None
        msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        out = self._llm.create_chat_completion(messages=msgs, max_tokens=max_tokens, temperature=0.0, top_k=1)
        return out["choices"][0]["message"]["content"].strip()


_adapter: BaseAdapter | None = None


def get_adapter(backend: str | None = None, refresh: bool = False) -> BaseAdapter:
    global _adapter
    if _adapter is not None and not refresh and backend is None:
        return _adapter
    backend = backend or config.LLM_BACKEND
    ad: BaseAdapter
    if backend == "none":
        ad = NullAdapter("Disabled by configuration (VEHICLEGPT_LLM=none)")
    elif backend == "ollama":
        ad = OllamaAdapter()
    elif backend == "llamacpp":
        ad = LlamaCppAdapter()
    else:  # auto
        ad = LlamaCppAdapter()
        if not ad.status().available:
            o = OllamaAdapter()
            ad = o if o.status().available else NullAdapter(
                f"No local model available — {ad.status().detail}; {o.status().detail}")
    if ad.status().available:
        classify("warm up the prompt cache", ad)  # pays the one-off prompt-processing cost at start-up, not on the first query
    _adapter = ad
    return ad


# ---------- grounded generation helpers ----------

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def numbers_grounded(text: str, facts: str) -> bool:
    """Every number in `text` must appear (to 1 dp tolerance) among the numbers in `facts`."""
    allowed = {round(float(x), 1) for x in _NUM.findall(facts)}
    allowed |= {round(float(x)) for x in _NUM.findall(facts)}
    for x in _NUM.findall(text):
        v = float(x)
        if round(v, 1) not in allowed and round(v) not in allowed and v not in (0, 1, 2, 7, 14, 10, 100):
            return False
    return True


def explain(facts: str, persona: str, adapter: BaseAdapter | None = None) -> tuple[str | None, str]:
    """Return (text, status). text is None when the LLM is unavailable or its output fails grounding."""
    ad = adapter or get_adapter()
    if not ad.status().available:
        return None, "fallback"
    style = ("plain language for a driver, 2-3 sentences, no jargon" if persona == "driver"
             else "concise technical summary for an engineer, 2-3 sentences")
    prompt = (f"You are VehicleGPT. Rewrite the VERIFIED FACTS below in {style}. Use only these facts. "
              "Do not add numbers, causes or codes that are not in the facts. Say 'possible' for hypotheses.\n\n"
              f"VERIFIED FACTS:\n{facts}\n\nAnswer:")
    out = ad.generate(prompt, max_tokens=90)
    if not out:
        return None, "llm_error"
    if not numbers_grounded(out, facts):
        return None, "rejected_ungrounded"
    return out, "llm"


INTENT_GUIDE = {
    "range_change": "why driving range changed / went down",
    "before_warning": "what happened before or led up to a warning / fault / overheating",
    "unusual_events": "unusual, odd or abnormal things in a period",
    "compare_energy": "compare energy use between two periods (this week vs last week)",
    "consumption_normal": "is energy use higher or lower than normal / usual",
    "repeated_dtc": "warnings or codes that keep recurring / coming back",
    "metric_stat": "a value of one sensor: how hot / max / min / average temperature, speed, charge, tyre pressure",
    "distance": "how far / how many km driven",
    "alerts_summary": "list of current alerts or warnings",
    "adas_events": "driver assistance: lane assist, adaptive cruise, ADAS disengagements, camera blocked",
    "largest_drop": "which day efficiency was worst / dropped the most",
    "city_highway": "city vs highway / town vs motorway efficiency",
    "explain_dtc": "explain what a warning or code means",
    "latest_dtcs": "latest / recent diagnostic codes",
    "data_quality": "missing, implausible, duplicate or stale data",
    "unknown": "anything else",
}
INTENTS = list(INTENT_GUIDE)
CLASSIFY_SYSTEM = ("You route questions about a car's recorded data to one handler. Reply with the handler label only.\n"
                   + "\n".join(f"{k}: {v}" for k, v in INTENT_GUIDE.items()))


def classify(question: str, adapter: BaseAdapter | None = None) -> str | None:
    ad = adapter or get_adapter()
    if not ad.status().available:
        return None
    out = (ad.generate(f"Question: {question[:300]}\nLabel:", max_tokens=8, system=CLASSIFY_SYSTEM) or "").strip().lower()
    out = out.replace("`", "").replace("label:", "").strip()
    for it in sorted(INTENTS, key=len, reverse=True):
        if it in out:
            return it if it != "unknown" else None
    return None
