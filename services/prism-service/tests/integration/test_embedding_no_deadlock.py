"""Red scaffold (integration) — embedding path can no longer DEADLOCK (GH #162).

Root cause lineage: #155 (v6.5.0, env pin) + #157 (v6.5.2, _ENCODE_LOCK
single-flight) + #162 (v6.5.6/6.5.7, out-of-process supervisor) hardened
RECOVERY but never closed the two remaining DEADLOCK doors:

  1. The model LOAD (brain_engine.py:444-452, StaticModel.from_pretrained /
     SentenceTransformer) runs UNPINNED under _MODEL_LOCK — the encode sites
     are wrapped in _threadpool_limit_1(), but the load is not, so the first
     native allocation can still oversubscribe the BLAS/OMP pools.
  2. The process-wide env pin (thread_limits.apply_thread_limits) only fills
     vars "if unset", so a customer who pre-sets OPENBLAS_NUM_THREADS=8 in
     their environment DEFEATS it entirely — there is no AUTHORITATIVE
     runtime pin that OVERRIDES a hostile pre-set env.
  3. torch + sentence-transformers are CORE deps (pyproject.toml:34-35), so a
     plain install pulls a 2nd OpenMP runtime (libgomp via torch) even on the
     default model2vec path that never needs it — two OpenMP runtimes is the
     classic deadlock multiplier.

These tests pin the ACCEPTANCE oracle and FAIL today (red):
  - FR-1   : load is wrapped in threadpoolctl.threadpool_limits(1).
  - FR-1b  : every encode site is wrapped (already true; regression guard).
  - FR-1c  : the runtime pin OVERRIDES a pre-set OPENBLAS_NUM_THREADS=8 —
             threadpool_info() after a real default load reports num_threads==1.
  - FR-1d  : threadpoolctl is a DECLARED runtime dependency in pyproject.toml.
  - FR-2   : torch + sentence-transformers are NOT in core deps; they live in
             an optional [neural]/[reranker] extra.
  - FR-2b  : the DEFAULT embedder path does NOT import torch (default encode
             AND default search leave 'torch' out of sys.modules).
  - FR-2c  : the CrossEncoder reranker degrades gracefully when unavailable.
  - FR-3   : threadpool_info() is logged at startup AND after model load.
  - NFR-2  : a concurrency stress test hammering encode() never hangs.
"""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from prism_service.engines import brain_engine as be

_REPO = Path(__file__).resolve().parents[2]
_PYPROJECT = _REPO / "pyproject.toml"


# ---------------------------------------------------------------------------
# Source helpers
# ---------------------------------------------------------------------------
def _module_src() -> str:
    return inspect.getsource(be)


def _names_in_with(with_node: ast.With) -> set[str]:
    """Bare Name / Call-of-Name context-manager identifiers on a `with`."""
    out: set[str] = set()
    for it in with_node.items:
        ctx = it.context_expr
        if isinstance(ctx, ast.Name):
            out.add(ctx.id)
        elif isinstance(ctx, ast.Call):
            f = ctx.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _call_to_loader_under_pin(func_or_src) -> bool:
    """True iff a `with`-block that names the threadpool-limit context manager
    (threadpool_limit_1 / threadpool_limits / threadpool) transitively calls a
    model loader (_load_model2vec / _load_sentence_transformer /
    StaticModel.from_pretrained / SentenceTransformer(...))."""
    src = func_or_src if isinstance(func_or_src, str) else inspect.getsource(func_or_src)
    tree = ast.parse(textwrap.dedent(src))
    loader_attrs = {"from_pretrained"}
    loader_names = {"_load_model2vec", "_load_sentence_transformer",
                    "SentenceTransformer", "StaticModel"}
    pin_names = {"_threadpool_limit_1", "threadpool_limits", "threadpool_limit_1"}

    def _calls_loader(node) -> bool:
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                f = n.func
                if isinstance(f, ast.Name) and f.id in loader_names:
                    return True
                if isinstance(f, ast.Attribute) and f.attr in loader_attrs:
                    return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.With) and (_names_in_with(node) & pin_names):
            if _calls_loader(node):
                return True
    return False


# ---------------------------------------------------------------------------
# FR-1 — model LOAD is wrapped in the threadpool-limit(1) context.
# ---------------------------------------------------------------------------
def test_model_load_is_threadpool_pinned():
    """`_try_enable_vector` (brain_engine.py:455) must run the model LOAD
    (`_load_model2vec` / `_load_sentence_transformer`) INSIDE the
    threadpool-limit(1) context, not just under the bare `_MODEL_LOCK`.

    RED today: the load block at :484-491 calls the loaders with NO
    threadpool pin around them."""
    assert _call_to_loader_under_pin(be._try_enable_vector), (
        "model LOAD is NOT wrapped in threadpool_limit_1() — the "
        "StaticModel/SentenceTransformer load at brain_engine.py:444-452 runs "
        "unpinned under _MODEL_LOCK, so the first native allocation can still "
        "oversubscribe the BLAS/OMP pools (GH #162 FR-1)."
    )


# ---------------------------------------------------------------------------
# FR-1b — every encode site stays wrapped in the threadpool pin (regression).
# ---------------------------------------------------------------------------
def _encode_under_pin(func) -> bool:
    src = inspect.getsource(func)
    tree = ast.parse(textwrap.dedent(src))
    pin_names = {"_threadpool_limit_1", "threadpool_limits", "threadpool_limit_1"}

    def _calls_encode(node) -> bool:
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "encode"
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "_MODEL"):
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.With) and (_names_in_with(node) & pin_names):
            if _calls_encode(node):
                return True
    return False


def test_every_encode_site_is_threadpool_pinned():
    """FR-1b: encode_task_text (:92) and Brain._embed (:1191) must each wrap
    `_MODEL.encode(...)` in the threadpool-limit(1) context. The probe encode
    (:812) is covered by the source text of the enclosing class."""
    assert _encode_under_pin(be.encode_task_text), (
        "encode_task_text must wrap _MODEL.encode in _threadpool_limit_1()"
    )
    assert _encode_under_pin(be.Brain._embed), (
        "Brain._embed must wrap _MODEL.encode in _threadpool_limit_1()"
    )
    # probe site
    src = textwrap.dedent(inspect.getsource(be.Brain))
    assert _call_under_pin_has_probe(src), (
        "health-probe encode (:812) must wrap _MODEL.encode(['probe']) in the "
        "threadpool-limit(1) context"
    )


def _call_under_pin_has_probe(src: str) -> bool:
    tree = ast.parse(src)
    pin_names = {"_threadpool_limit_1", "threadpool_limits", "threadpool_limit_1"}
    for node in ast.walk(tree):
        if isinstance(node, ast.With) and (_names_in_with(node) & pin_names):
            for n in ast.walk(node):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "encode" and n.args
                        and isinstance(n.args[0], ast.List) and n.args[0].elts
                        and isinstance(n.args[0].elts[0], ast.Constant)
                        and n.args[0].elts[0].value == "probe"):
                    return True
    return False


# ---------------------------------------------------------------------------
# FR-1c — the RUNTIME pin OVERRIDES a hostile pre-set OPENBLAS_NUM_THREADS=8.
#
# Run in a clean subprocess so the parent's already-pinned env / already-loaded
# numpy doesn't mask the failure. The child pre-sets OPENBLAS_NUM_THREADS=8
# (the customer-hostile case) BEFORE importing brain_engine, loads the default
# model, and reports threadpool_info() after load. The fix must clamp every
# BLAS/OpenMP pool to 1 regardless of the pre-set env.
# ---------------------------------------------------------------------------
_FR1C_CHILD = r'''
import os, json, sys
os.environ["OPENBLAS_NUM_THREADS"] = "8"
os.environ["OMP_NUM_THREADS"] = "8"
os.environ.pop("PRISM_EMBEDDER", None)  # default = potion/model2vec
import sqlite3, tempfile
from prism_service.engines import brain_engine as be
import threadpoolctl
tmp = tempfile.mkdtemp()
b = be.Brain(brain_db=tmp + "/brain.db", graph_db=tmp + "/graph.db",
             scores_db=tmp + "/scores.db")
# force a real load + encode through the public path
_ = be.encode_task_text("hello world")
info = threadpoolctl.threadpool_info()
print("THREADPOOL_INFO=" + json.dumps(info))
print("VECTOR_ENABLED=" + str(bool(b.vector_enabled)))
'''


def test_runtime_pin_overrides_preset_openblas_env():
    """FR-1c: with OPENBLAS_NUM_THREADS=8 already in the env, threadpool_info()
    AFTER a real default model load must report num_threads==1 for EVERY pool.

    RED today: thread_limits only fills vars 'if unset' (operator-wins) and the
    LOAD is unpinned, so a pre-set 8 survives -> a pool reports num_threads==8."""
    env = dict(os.environ)
    env["OPENBLAS_NUM_THREADS"] = "8"
    env["OMP_NUM_THREADS"] = "8"
    proc = subprocess.run(
        [sys.executable, "-c", _FR1C_CHILD],
        capture_output=True, text=True, timeout=170, cwd=str(_REPO), env=env,
    )
    out = proc.stdout
    assert "THREADPOOL_INFO=" in out, (
        f"child did not report threadpool_info; stderr=\n{proc.stderr[-2000:]}"
    )
    import json as _json
    line = [l for l in out.splitlines() if l.startswith("THREADPOOL_INFO=")][-1]
    info = _json.loads(line[len("THREADPOOL_INFO="):])
    assert info, "threadpool_info() empty — no BLAS/OMP runtime detected to pin"
    bad = [p for p in info if int(p.get("num_threads", 0)) != 1]
    assert not bad, (
        "runtime pin did NOT override pre-set OPENBLAS_NUM_THREADS=8 — these "
        f"pools still report num_threads!=1: "
        f"{[(p.get('user_api'), p.get('num_threads')) for p in bad]}"
    )


# ---------------------------------------------------------------------------
# pyproject dependency contract (FR-1d + FR-2).
# ---------------------------------------------------------------------------
def _load_pyproject() -> dict:
    try:
        import tomllib  # py311+
    except ModuleNotFoundError:  # pragma: no cover
        import tomli as tomllib  # type: ignore
    with open(_PYPROJECT, "rb") as fh:
        return tomllib.load(fh)


def _dep_names(reqs: list[str]) -> set[str]:
    import re
    out = set()
    for r in reqs or []:
        m = re.match(r"^\s*([A-Za-z0-9_.\-]+)", r)
        if m:
            out.add(m.group(1).lower().replace("_", "-"))
    return out


def test_threadpoolctl_is_a_declared_runtime_dependency():
    """FR-1d: threadpoolctl must be a DECLARED core runtime dependency — it is
    the authoritative runtime pin, no longer a best-effort optional import.

    RED today: threadpoolctl appears nowhere in pyproject.toml."""
    proj = _load_pyproject()
    core = _dep_names(proj["project"]["dependencies"])
    assert "threadpoolctl" in core, (
        "threadpoolctl is not a declared core dependency in pyproject.toml "
        f"(core deps: {sorted(core)})"
    )


def test_torch_and_sentence_transformers_are_not_core_deps():
    """FR-2: torch + sentence-transformers must move OUT of core deps into an
    optional extra so a plain install does not pull torch (a 2nd OpenMP
    runtime). They must still be available via an extra (neural/reranker).

    RED today: both are listed in [project] dependencies (pyproject.toml:34-35)."""
    proj = _load_pyproject()
    core = _dep_names(proj["project"]["dependencies"])
    assert "torch" not in core, "torch must NOT be a core dependency (FR-2)"
    assert "sentence-transformers" not in core, (
        "sentence-transformers must NOT be a core dependency (FR-2)"
    )
    extras = proj["project"].get("optional-dependencies", {})
    all_extra = set()
    for reqs in extras.values():
        all_extra |= _dep_names(reqs)
    assert "torch" in all_extra and "sentence-transformers" in all_extra, (
        "torch + sentence-transformers must be declared in an optional extra "
        f"(e.g. [neural]/[reranker]); extras seen: {sorted(extras)}"
    )


# ---------------------------------------------------------------------------
# FR-2b — the DEFAULT embedder path does NOT import torch.
#
# Subprocess-isolated: the parent process may already have torch in sys.modules
# (the reranker test, or a transitive import), so a clean child is the only
# honest oracle. PRISM_RERANK is forced off so the reranker never loads.
# ---------------------------------------------------------------------------
_FR2B_CHILD = r'''
import os, sys, json, tempfile
os.environ.pop("PRISM_EMBEDDER", None)   # default = potion / model2vec
os.environ["PRISM_RERANK"] = "off"
from prism_service.engines import brain_engine as be
tmp = tempfile.mkdtemp()
b = be.Brain(brain_db=tmp + "/brain.db", graph_db=tmp + "/graph.db",
             scores_db=tmp + "/scores.db")
b.index_doc(doc_id="d1", filepath="x.py", content="def hello(): return 1",
            doc_type="code") if hasattr(b, "index_doc") else None
after_encode = "torch" in sys.modules
_ = be.encode_task_text("default path encode")
after_encode2 = "torch" in sys.modules
res = b.search("hello", limit=3)
after_search = "torch" in sys.modules
print("TORCH_AFTER=" + json.dumps({
    "after_index": after_encode,
    "after_encode": after_encode2,
    "after_search": after_search,
}))
'''


def test_default_path_does_not_import_torch():
    """FR-2b: 'torch' must NOT be in sys.modules after a default encode AND
    after a default search. The default model2vec path is numpy-only; torch is
    a 2nd OpenMP runtime that must never load on the default path.

    RED today: torch is a CORE dep and the reranker/sentence-transformers
    import chain pulls it onto the default path."""
    env = dict(os.environ)
    env.pop("PRISM_EMBEDDER", None)
    env["PRISM_RERANK"] = "off"
    proc = subprocess.run(
        [sys.executable, "-c", _FR2B_CHILD],
        capture_output=True, text=True, timeout=170, cwd=str(_REPO), env=env,
    )
    out = proc.stdout
    assert "TORCH_AFTER=" in out, (
        f"child failed before reporting; stderr=\n{proc.stderr[-2000:]}"
    )
    import json as _json
    line = [l for l in out.splitlines() if l.startswith("TORCH_AFTER=")][-1]
    flags = _json.loads(line[len("TORCH_AFTER="):])
    assert flags["after_encode"] is False, (
        "torch was imported on the DEFAULT encode path (FR-2b)"
    )
    assert flags["after_search"] is False, (
        "torch was imported on the DEFAULT search path (FR-2b)"
    )


# ---------------------------------------------------------------------------
# FR-2c — the CrossEncoder reranker degrades gracefully when unavailable.
# ---------------------------------------------------------------------------
def test_reranker_degrades_gracefully_when_unavailable(monkeypatch, capsys):
    """FR-2c: when sentence-transformers/torch are not installed, _load_reranker
    must SKIP the neural rerank (return None) and log a clear message — never
    raise. Default search must never load the 2nd OpenMP runtime."""
    import builtins
    real_import = builtins.__import__

    def _no_st(name, *a, **k):
        if name == "sentence_transformers" or name.startswith("sentence_transformers."):
            raise ImportError("sentence_transformers not installed (simulated)")
        return real_import(name, *a, **k)

    # reset cache so the load is attempted fresh
    monkeypatch.setattr(be, "_RERANKER", None, raising=False)
    monkeypatch.setattr(be, "_RERANKER_KEY", "", raising=False)
    monkeypatch.setattr(builtins, "__import__", _no_st)

    out = be._load_reranker("bge-v2")
    assert out is None, "reranker must return None when unavailable (graceful)"
    captured = capsys.readouterr()
    blob = (captured.out + captured.err).lower()
    assert ("rerank" in blob and ("disabl" in blob or "fail" in blob or "skip" in blob)), (
        "graceful degradation must emit a clear reranker-disabled log line"
    )


# ---------------------------------------------------------------------------
# FR-3 — threadpool_info() logged at startup AND after model load.
# ---------------------------------------------------------------------------
def test_threadpool_info_logged_at_startup_and_after_load():
    """FR-3: the logs must PROVE the pools are pinned. main.py must log
    threadpool_info() at startup, and the model-load path (brain_engine) must
    log threadpool_info() after the model loads.

    RED today: main.py calls apply_thread_limits() but never logs
    threadpool_info(); brain_engine never logs it after load."""
    import prism_service.main as main_mod
    main_src = inspect.getsource(main_mod)
    assert "threadpool_info" in main_src, (
        "main.py never logs threadpoolctl.threadpool_info() at startup (FR-3)"
    )
    be_src = _module_src()
    assert "threadpool_info" in be_src, (
        "brain_engine never logs threadpool_info() after model load (FR-3)"
    )


# ---------------------------------------------------------------------------
# NFR-2 — concurrency stress: hammering encode() from many threads never hangs.
#
# Uses the REAL default model so the native BLAS/OMP path is exercised end to
# end (not a fake). A join timeout is the hang detector: if the single-flight
# lock + threadpool pin are wrong, the threads wedge and the joins time out.
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def _real_default_model():
    # Force the default (model2vec / potion) embedder, real load.
    os.environ.pop("PRISM_EMBEDDER", None)
    if be._MODEL is None:
        import sqlite3, tempfile
        tmp = tempfile.mkdtemp()
        conn = sqlite3.connect(tmp + "/probe.db")
        be._try_enable_vector(conn)
    if be._MODEL is None:
        pytest.skip("no embedding model available in this environment")
    return be._MODEL


def test_concurrent_encode_stress_does_not_hang(_real_default_model):
    """NFR-2: 32 threads hammering encode_task_text() concurrently must all
    finish (the join timeout is the hang detector) and every result is valid.

    This is the real-seam deadlock oracle: the GH #162 wedge is exactly this
    scenario (many request threads entering native math at once)."""
    n = 32
    errors: list[BaseException] = []
    done = [False] * n
    barrier = threading.Barrier(n)

    def _worker(i: int):
        try:
            barrier.wait()
            for _ in range(3):
                blob = be.encode_task_text(f"stress text {i}")
                assert blob is not None
            done[i] = True
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    alive = [t for t in threads if t.is_alive()]
    assert not alive, (
        f"{len(alive)}/{n} encode threads still RUNNING after 60s — the "
        f"embedding path WEDGED (GH #162 deadlock). elapsed={time.time()-t0:.1f}s"
    )
    assert not errors, f"worker raised under concurrency: {errors[:3]}"
    assert all(done), f"only {sum(done)}/{n} workers completed"
