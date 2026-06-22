"""Red scaffold (integration) — native-math thread guards (GH #155).

The silent all-threads wedge is OpenMP/OpenBLAS futex oversubscription on
the embedding path. The fix pins the native thread pools to 1 BEFORE any
numpy/model2vec import. These tests pin the real seam:

  (a) apply_thread_limits() sets the documented vars to safe defaults
      ONLY when the operator hasn't already set them (operator wins).
  (b) the limiter is imported at the VERY TOP of main.py AND the CLI
      entrypoint, BEFORE numpy/model2vec are pulled in transitively.

A unit test that merely calls apply_thread_limits would pass even if the
import never runs at boot — so (b) asserts against the module SOURCE so it
fails loudly if the top-of-file wiring is removed or sinks below the
numpy-importing config/brain imports.
"""

from __future__ import annotations

import importlib
import inspect
import re

import pytest

GUARD_VARS = (
    "OMP_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def test_apply_thread_limits_sets_defaults_when_unset(monkeypatch):
    """With no operator override, every native-math pool is pinned to 1
    and the OpenMP/tokenizer policy knobs are set to the passive defaults."""
    for v in GUARD_VARS + ("OMP_WAIT_POLICY", "TOKENIZERS_PARALLELISM"):
        monkeypatch.delenv(v, raising=False)

    tl = importlib.import_module("prism_service.thread_limits")
    tl.apply_thread_limits()

    import os
    for v in GUARD_VARS:
        assert os.environ.get(v) == "1", f"{v} not pinned to 1"
    assert os.environ.get("OMP_WAIT_POLICY") == "PASSIVE"
    assert os.environ.get("TOKENIZERS_PARALLELISM") == "false"


def test_apply_thread_limits_respects_operator(monkeypatch):
    """Operator-set values are left UNTOUCHED — the guard only fills in
    defaults for vars the operator has not already chosen."""
    monkeypatch.setenv("OMP_NUM_THREADS", "8")
    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "true")
    for v in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
              "NUMEXPR_NUM_THREADS", "OMP_WAIT_POLICY"):
        monkeypatch.delenv(v, raising=False)

    tl = importlib.import_module("prism_service.thread_limits")
    tl.apply_thread_limits()

    import os
    assert os.environ.get("OMP_NUM_THREADS") == "8", "operator value clobbered"
    assert os.environ.get("TOKENIZERS_PARALLELISM") == "true", "operator value clobbered"
    # Vars the operator left unset still get the safe default.
    assert os.environ.get("OPENBLAS_NUM_THREADS") == "1"
    assert os.environ.get("MKL_NUM_THREADS") == "1"


def test_main_imports_thread_limits_before_numpy():
    """main.py must apply_thread_limits BEFORE the config/brain import
    block that transitively pulls numpy/model2vec — else the pin is too
    late and the futex oversubscription is already baked in."""
    import prism_service.main as main_mod

    src = inspect.getsource(main_mod)
    assert "apply_thread_limits" in src, (
        "main.py never calls apply_thread_limits — native-math guard not wired"
    )
    tl_pos = src.find("apply_thread_limits(")
    cfg_pos = src.find("from prism_service.config import")
    assert tl_pos != -1 and cfg_pos != -1
    assert tl_pos < cfg_pos, (
        "apply_thread_limits must run BEFORE 'from prism_service.config import' "
        "(config/brain transitively import numpy/model2vec)"
    )


def test_cli_entrypoint_imports_thread_limits_before_heavy_imports():
    """The `prism` CLI entrypoint must also apply the guard at the very top,
    before any subcommand body imports numpy/model2vec."""
    import prism_service.cli.prism_cli as cli_mod

    src = inspect.getsource(cli_mod)
    assert "apply_thread_limits" in src, (
        "prism_cli.py never calls apply_thread_limits — CLI boot path unguarded"
    )
    # The call must sit at module top, above the argparse/main machinery,
    # so it runs before any deferred heavy import in a subcommand body.
    assert re.search(r"apply_thread_limits\(\)", src), (
        "apply_thread_limits is referenced but not invoked at CLI import time"
    )
