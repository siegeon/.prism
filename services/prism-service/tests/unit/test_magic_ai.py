"""Tests for the OpenAI-free Magic AI substrate (services/magic_ai).

PRISM is the memory substrate (retrieve) + a local OpenAI-wire model
(complete). The invariant these tests pin: no OpenAI, no Anthropic —
generation runs entirely on injected local pieces.
"""

from __future__ import annotations

import json

import pytest

from prism_service.services import magic_ai as ai


def test_ai_generate_uses_context_and_is_openai_free():
    seen = {}

    def retrieve(q, k=5):
        seen["query"] = q
        return ["snippet A", "snippet B"]

    def complete(messages):
        seen["messages"] = messages
        return "GENERATED", {"total_tokens": 42}

    out = ai.ai_generate("build a list endpoint", retrieve, complete=complete)
    assert out["answer"] == "GENERATED"
    assert out["context_used"] == 2
    assert out["local_tokens"] == 42
    assert out["openai_tokens"] == 0 and out["anthropic_tokens"] == 0
    # context actually reached the model
    user = seen["messages"][-1]["content"]
    assert "snippet A" in user and "snippet B" in user
    assert "build a list endpoint" in user


def test_ai_generate_with_no_context():
    out = ai.ai_generate("hi", lambda q, k=5: [],
                         complete=lambda m: ("ok", {}))
    assert out["context_used"] == 0
    assert out["openai_tokens"] == 0


def test_make_brain_retriever_extracts_text_fields():
    def brain_search(q, limit):
        return [{"text": "T1"}, {"snippet": "S2"},
                {"description": "D3"}, "raw4"]

    retrieve = ai.make_brain_retriever(brain_search)
    got = retrieve("q", 5)
    assert got == ["T1", "S2", "D3", "raw4"]


def test_local_complete_parses_openai_shape(monkeypatch):
    import urllib.request

    class _R:
        def __init__(self, payload):
            self._b = json.dumps(payload).encode()
        def read(self):
            return self._b
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode())
        return _R({"choices": [{"message": {"content": "hello"}}],
                   "usage": {"total_tokens": 7}})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    text, usage = ai.local_complete([{"role": "user", "content": "hi"}])
    assert text == "hello" and usage["total_tokens"] == 7
    # hits the OpenAI-compatible path, never api.openai.com
    assert "/chat/completions" in captured["url"]
    assert "openai.com" not in captured["url"]


def test_local_complete_maps_http_error(monkeypatch):
    import urllib.error
    import urllib.request

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "err", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(ai.LocalLLMError):
        ai.local_complete([{"role": "user", "content": "hi"}])


def test_generate_verified_repairs_until_ok():
    from prism_service.services import magic_ai as ai
    attempts = {"n": 0}

    def complete(messages):
        attempts["n"] += 1
        # first try bad, second try good (4-space indent -> normalized)
        if attempts["n"] == 1:
            return "```hl\nsqlite.connect:db\n  bad\n```", {"total_tokens": 10}
        return "```hl\nsqlite.connect:db\n    sqlite.select:SELECT 1\n```", {"total_tokens": 12}

    def verify(hl):
        # accept once the select is present
        return ("sqlite.select" in hl, "needs a select")

    out = ai.generate_verified("build it", lambda q, k=5: ["ctx"],
                               verify=verify, complete=complete, max_rounds=3)
    assert out["ok"] is True
    assert out["rounds"] == 2
    assert out["openai_tokens"] == 0 and out["anthropic_tokens"] == 0
    # normalized to 3-space multiples
    assert all((len(l) - len(l.lstrip())) % 3 == 0
               for l in out["hyperlambda"].splitlines() if l.strip())


def test_liberate_training_corpus_writes_and_ingests(tmp_path):
    from prism_service.services import magic_ai as ai
    snippets = [{"prompt": "P1", "completion": "C1"},
                {"prompt": "P2", "completion": "C2", "type": "hl"}]
    ingested = {}

    def ingest(paths):
        ingested["paths"] = paths
        return 2

    out = ai.liberate_training_corpus(lambda: snippets,
                                      str(tmp_path / "corpus"), ingest)
    assert out["snippets"] == 2 and out["docs_indexed"] == 2
    assert out["openai_tokens"] == 0
    files = list((tmp_path / "corpus").glob("*.md"))
    assert len(files) == 2
    assert "P1" in files[0].read_text() or "P1" in files[1].read_text()
