"""A cold PRISM answers without calling the internet (task b0138f17).

Pins the offline-first embedder load: a CACHED model resolves to its local
HuggingFace snapshot path (local_files_only=True — zero network by contract)
and the loader receives that PATH, so the hub is never consulted on boot;
an uncached model falls back to the online repo id exactly once. Also pins
that main.py warms the embedder on a boot thread so the first request never
pays the load.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.engines import brain_engine as be  # noqa: E402


def test_cached_model_resolves_to_local_snapshot_path(monkeypatch):
    calls = {}

    def fake_snapshot(model_id, local_files_only=False):
        calls["local_files_only"] = local_files_only
        return "/hf-cache/snapshots/abc123"

    import huggingface_hub
    monkeypatch.setattr(huggingface_hub, "snapshot_download", fake_snapshot)
    out = be._local_snapshot_or_id("minishlab/potion-retrieval-32M")
    assert out == "/hf-cache/snapshots/abc123"
    assert calls["local_files_only"] is True, (
        "the cache probe must pass local_files_only=True — anything else "
        "may open a network connection on a cold start")


def test_uncached_model_falls_back_to_repo_id(monkeypatch):
    import huggingface_hub

    def raising_snapshot(model_id, local_files_only=False):
        raise FileNotFoundError("not in local cache")

    monkeypatch.setattr(huggingface_hub, "snapshot_download", raising_snapshot)
    assert be._local_snapshot_or_id("some/uncached-model") == "some/uncached-model"


def test_warm_embedder_loads_from_the_resolved_local_source(monkeypatch):
    seen = {}
    monkeypatch.setattr(be, "_MODEL", None)
    monkeypatch.setattr(be, "_local_snapshot_or_id", lambda mid: "/local/snap")
    monkeypatch.setattr(be, "_load_model2vec",
                        lambda src: seen.setdefault("src", src) or object())
    monkeypatch.setenv("PRISM_EMBEDDER", "potion")
    assert be.warm_embedder() is True
    assert seen["src"] == "/local/snap", (
        "warm_embedder must hand the loader the LOCAL snapshot path, not "
        "the repo id — a repo id makes huggingface_hub phone home on boot")
    # A second warm is a no-op on the already-loaded model.
    monkeypatch.setattr(be, "_load_model2vec",
                        lambda src: (_ for _ in ()).throw(AssertionError("reloaded")))
    assert be.warm_embedder() is True


def test_main_warms_the_embedder_on_a_boot_thread():
    src = (_SERVICE_ROOT / "prism_service" / "main.py").read_text(encoding="utf-8")
    assert "warm_embedder" in src, (
        "main.py no longer warms the embedder at boot — the first request "
        "after a restart pays the model load again (task b0138f17)")
    import re
    assert re.search(r"threading\.Thread\(target=warm_embedder", src), (
        "warm_embedder must run on its own boot thread, never inline in "
        "the lifespan (it would block startup)")
