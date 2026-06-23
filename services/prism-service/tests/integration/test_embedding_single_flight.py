"""Concurrent Brain embeddings must be single-flight (GH #157, task 1b28cde8).

v6.5.0 (mx-783cb5) pinned OMP/BLAS to 1 thread per embed + a deadlock
watchdog, but did NOTHING about TWO concurrent embeds: `_MODEL_LOCK`
(brain_engine.py:153) serializes only model LOAD (its sole use is at
:459 inside the loader). The three `_MODEL.encode(...)` native-inference
sites run UNLOCKED:
  - encode_task_text()  ~:90   (vec = _MODEL.encode([text[:2048]])[0])
  - health probe         ~:786  (probe = _MODEL.encode(["probe"])[0])
  - _embed() doc-index   ~:1161 (vecs = _MODEL.encode([text[:2048]]))

Two request threads entering model2vec->numpy/BLAS native math at once
invert the GIL<->native-futex and silently wedge every thread. The fix
is a dedicated module-level `_ENCODE_LOCK` (separate from `_MODEL_LOCK`)
wrapping each encode so only ONE encode runs at a time.

This test FAILS today (red): `_ENCODE_LOCK` does not exist and the
encode sites do not serialize, so the in-flight overlap counter exceeds 1.
"""

from __future__ import annotations

import ast
import inspect
import threading
import time

import pytest

from prism_service.engines import brain_engine as be


# ---------------------------------------------------------------------------
# Source assertions: every encode site must acquire _ENCODE_LOCK.
# ---------------------------------------------------------------------------
def _encode_under_encode_lock(func) -> bool:
    """True iff the function body contains a `with _ENCODE_LOCK:` block whose
    body (transitively) calls `_MODEL.encode(...)`."""
    src = inspect.getsource(func)
    tree = ast.parse(_dedent(src))

    def _calls_model_encode(node) -> bool:
        for n in ast.walk(node):
            if isinstance(n, ast.Call):
                f = n.func
                if (
                    isinstance(f, ast.Attribute)
                    and f.attr == "encode"
                    and isinstance(f.value, ast.Name)
                    and f.value.id == "_MODEL"
                ):
                    return True
        return False

    def _uses_encode_lock(items) -> bool:
        for it in items:
            ctx = getattr(it, "context_expr", None)
            if isinstance(ctx, ast.Name) and ctx.id == "_ENCODE_LOCK":
                return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, ast.With) and _uses_encode_lock(node.items):
            if _calls_model_encode(node):
                return True
    return False


def _dedent(src: str) -> str:
    import textwrap
    return textwrap.dedent(src)


def test_encode_lock_symbol_exists():
    lock = getattr(be, "_ENCODE_LOCK", None)
    assert lock is not None, "_ENCODE_LOCK must be a module-level lock"
    # Must be a DISTINCT lock from the model-load lock.
    assert lock is not be._MODEL_LOCK, "_ENCODE_LOCK must differ from _MODEL_LOCK"
    # A lock instance exposes acquire/release.
    assert hasattr(lock, "acquire") and hasattr(lock, "release")


def test_all_three_encode_sites_acquire_encode_lock():
    # ~:90 encode_task_text (module function)
    assert _encode_under_encode_lock(be.encode_task_text), (
        "encode_task_text (~:90) must wrap _MODEL.encode in `with _ENCODE_LOCK:`"
    )
    # ~:1161 BrainEngine._embed (doc-index path)
    assert _encode_under_encode_lock(be.Brain._embed), (
        "BrainEngine._embed (~:1161) must wrap _MODEL.encode in `with _ENCODE_LOCK:`"
    )
    # ~:786 health probe — lives inside a larger method; assert the source
    # text of the enclosing class shows the probe encode under the lock.
    src = _dedent(inspect.getsource(be.Brain))
    tree = ast.parse(src)
    probe_ok = False
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            uses_lock = any(
                isinstance(it.context_expr, ast.Name)
                and it.context_expr.id == "_ENCODE_LOCK"
                for it in node.items
            )
            if not uses_lock:
                continue
            for n in ast.walk(node):
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "encode"
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "_MODEL"
                    and n.args
                    and isinstance(n.args[0], ast.List)
                    and n.args[0].elts
                    and isinstance(n.args[0].elts[0], ast.Constant)
                    and n.args[0].elts[0].value == "probe"
                ):
                    probe_ok = True
    assert probe_ok, (
        "health probe (~:786) must wrap `_MODEL.encode(['probe'])` in "
        "`with _ENCODE_LOCK:`"
    )


# ---------------------------------------------------------------------------
# Runtime serialization: the in-flight overlap counter must never exceed 1.
# ---------------------------------------------------------------------------
class _OverlapModel:
    """Fake embedder that brackets every encode() with an in-flight counter.

    The sleep forces threads to actually overlap in time if the code does
    NOT serialize, so the peak counter rises above 1 in the unlocked state.
    Deterministic: the failure mode (peak > 1) reproduces every run because
    every encode holds for HOLD_S, far longer than thread spawn jitter.
    """

    HOLD_S = 0.02

    def __init__(self):
        self._mu = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.calls = 0

    def encode(self, texts):
        with self._mu:
            self.in_flight += 1
            self.calls += 1
            if self.in_flight > self.peak:
                self.peak = self.in_flight
        try:
            time.sleep(self.HOLD_S)
            # Return deterministic numpy vectors per input so BOTH the
            # encode_task_text path (np.asarray(...).tobytes()) and the
            # _embed path (vecs[0].tolist()) accept the output (correctness).
            import numpy as np
            return np.asarray(
                [[float(len(t)), 1.0, 2.0, 3.0] for t in texts],
                dtype=np.float32,
            )
        finally:
            with self._mu:
                self.in_flight -= 1


class _StubEngine:
    """Minimal stand-in so BrainEngine._embed runs without a full init."""
    vector_enabled = True


@pytest.fixture
def _install_overlap_model(monkeypatch):
    model = _OverlapModel()
    monkeypatch.setattr(be, "_MODEL", model)
    return model


@pytest.mark.parametrize("run", range(3))  # determinism across repeated runs
def test_concurrent_encode_is_single_flight(_install_overlap_model, run):
    model = _install_overlap_model
    errors: list[BaseException] = []
    barrier = threading.Barrier(16)

    def _task_worker(i):
        try:
            barrier.wait()
            out = be.encode_task_text(f"task text {i}")
            assert out is not None
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def _doc_worker(i):
        try:
            barrier.wait()
            out = be.Brain._embed(_StubEngine(), f"doc text {i}")
            assert out is not None
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = []
    for i in range(8):
        threads.append(threading.Thread(target=_task_worker, args=(i,)))
        threads.append(threading.Thread(target=_doc_worker, args=(i,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"worker raised: {errors[:3]}"
    assert model.calls == 16, f"every worker must encode once, got {model.calls}"
    assert model.peak == 1, (
        f"encode is NOT single-flight: peak concurrent encodes={model.peak} "
        f"(expected 1). Two threads entered native math at once -> wedge risk."
    )


# ---------------------------------------------------------------------------
# Correctness: serialization must not corrupt the encode output.
# ---------------------------------------------------------------------------
def test_serialized_encode_results_are_correct(_install_overlap_model):
    # Single-threaded reference.
    ref_blob = be.encode_task_text("hello")
    ref = be.decode_task_embedding(ref_blob)
    # _OverlapModel returns [len(t), 1.0, 2.0, 3.0]; "hello" -> 5.0
    assert ref is not None and ref[0] == 5.0 and ref[1:] == [1.0, 2.0, 3.0]

    # Same input under heavy concurrency must yield the SAME vector.
    results: list[list[float]] = []
    lock = threading.Lock()

    def _worker():
        blob = be.encode_task_text("hello")
        vec = be.decode_task_embedding(blob)
        with lock:
            results.append(vec)

    threads = [threading.Thread(target=_worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 12
    for vec in results:
        assert vec == ref, "concurrent encode corrupted the vector output"

    # The doc-index path returns a plain list, also correct under the lock.
    doc_vec = be.Brain._embed(_StubEngine(), "world")
    assert doc_vec == [5.0, 1.0, 2.0, 3.0]


# ---------------------------------------------------------------------------
# Optional threadpoolctl belt-and-suspenders: present only if importable,
# and NEVER a hard dependency (no top-level import of threadpoolctl).
# ---------------------------------------------------------------------------
def test_threadpoolctl_is_optional_not_a_hard_dependency():
    src = inspect.getsource(be)
    # Must not hard-import threadpoolctl at module top level.
    tree = ast.parse(src)
    for node in tree.body:  # module-level statements only
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "threadpoolctl", (
                    "threadpoolctl must be an OPTIONAL (guarded) import, "
                    "never a module-level hard dependency"
                )
        if isinstance(node, ast.ImportFrom):
            assert node.module != "threadpoolctl", (
                "threadpoolctl must be an OPTIONAL (guarded) import"
            )
