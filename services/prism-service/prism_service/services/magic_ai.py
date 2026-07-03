"""PRISM as the AI substrate for Magic — OpenAI-free.

The AI generator's two halves are re-homed onto local infrastructure:
  * MEMORY  -> PRISM's Brain (retrieval over the vectorized training
    content). `retrieve(query)` returns the relevant context snippets.
  * MODEL   -> a local model served by ollama, which speaks the OpenAI
    wire format (/v1/chat/completions), so no api.openai.com and no key.

Net: PRISM holds the vectors and orchestrates; the local model
generates. Zero Anthropic and zero OpenAI tokens.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

# ollama's OpenAI-compatible endpoint; override for a remote host.
OLLAMA_BASE = os.environ.get("PRISM_LOCAL_LLM_BASE", "http://localhost:11434/v1")
LOCAL_MODEL = os.environ.get("PRISM_LOCAL_LLM_MODEL", "qwen2.5-coder:7b")
_TIMEOUT = 120


class LocalLLMError(Exception):
    """Local model call failed (network or non-2xx)."""


def local_complete(messages: list[dict], model: str | None = None,
                   base: str | None = None) -> tuple[str, dict]:
    """Call the local OpenAI-compatible chat endpoint. Returns
    (content, usage). No OpenAI, no key."""
    body = json.dumps({"model": model or LOCAL_MODEL,
                       "messages": messages,
                       "temperature": 0.1}).encode()
    url = f"{(base or OLLAMA_BASE).rstrip('/')}/chat/completions"
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise LocalLLMError(f"local llm http {e.code}: "
                            f"{e.read().decode('utf-8','replace')[:200]}") from e
    except urllib.error.URLError as e:
        raise LocalLLMError(f"local llm unreachable: {e.reason}") from e
    content = d["choices"][0]["message"]["content"]
    return content, d.get("usage", {})


def _build_messages(query: str, context: list[str], system: str) -> list[dict]:
    ctx = "\n\n".join(f"[context {i+1}]\n{c}" for i, c in enumerate(context))
    user = (f"Relevant knowledge from the memory substrate:\n{ctx}\n\n"
            f"Task: {query}") if context else query
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


DEFAULT_SYSTEM = (
    "You are PRISM's Magic builder. Using ONLY the provided memory-substrate "
    "context, produce a correct answer or Hyperlambda. Do not invent slots or "
    "syntax that are not grounded in the context.")


def ai_generate(query: str, retrieve, complete=local_complete,
                system: str = DEFAULT_SYSTEM, k: int = 5) -> dict:
    """The OpenAI-free AI generate loop: PRISM memory substrate supplies
    context, the local model generates.

    retrieve: callable(query, k) -> list[str] of context snippets from the
    Brain / memory substrate. complete: callable(messages) -> (text, usage).
    """
    context = list(retrieve(query, k) or [])
    messages = _build_messages(query, context, system)
    text, usage = complete(messages)
    return {"answer": text, "context_used": len(context),
            "local_tokens": usage.get("total_tokens", 0),
            "openai_tokens": 0, "anthropic_tokens": 0, "model": LOCAL_MODEL}


def make_brain_retriever(brain_search):
    """Adapt a brain_search(query, limit)->results callable into a
    retrieve(query, k)->list[str] the substrate can use. Each result's
    text/snippet becomes a context string."""
    def _retrieve(query: str, k: int = 5) -> list[str]:
        out = []
        for r in (brain_search(query, k) or [])[:k]:
            if isinstance(r, dict):
                out.append(str(r.get("text") or r.get("snippet")
                                or r.get("content") or r.get("description") or r))
            else:
                out.append(str(r))
        return out
    return _retrieve
