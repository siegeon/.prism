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
