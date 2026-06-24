"""task 0c811636: pin the implement.js convention push-injection seam.

implement.js must STOP shipping a frozen CONVENTIONS array to every agent and
instead pull the capped, importance-ordered conventions from context_bundle
once per drive, injecting them into the rendered preamble. These tests read
the workflow script source and assert the wiring facts that prove live
memories now reach the agent prompt:

  1. the drive calls context_bundle (the deterministic MCP assembler) — today
     it never does (pull-not-push);
  2. preamble() injects bundle-derived conventions, not a hardcoded feedback
     list frozen at edit time;
  3. the STATIC procedural spine (never-commit-to-main, ~30-line writes,
     hooks-advisory) is retained;
  4. memory_recall self-heal remains present as the fallback path.

We assert on source because the workflow script is an executable drive (top-
level await + an agent() runtime) that cannot be imported standalone. The
behavioral data path (memory -> bundle['conventions'] -> agent) is pinned
end-to-end through the MCP dispatcher in test_context_builder.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
# tests/unit -> tests -> prism-service -> services -> .prism
_REPO_ROOT = _HERE.parents[4]
_IMPLEMENT_JS = _REPO_ROOT / ".claude" / "workflows" / "implement.js"


@pytest.fixture
def src() -> str:
    assert _IMPLEMENT_JS.exists(), f"missing workflow script: {_IMPLEMENT_JS}"
    return _IMPLEMENT_JS.read_text(encoding="utf-8")


def test_drive_calls_context_bundle(src):
    """The locate step (or the drive) must call context_bundle once so the
    capped conventions are pulled from the deterministic assembler. Today the
    script never invokes it (pull-not-push) — this fails until wired."""
    assert "context_bundle" in src, (
        "implement.js never calls context_bundle — conventions are still "
        "pull-not-push; the locate step must invoke it once per drive."
    )


def test_preamble_injects_conventions_not_frozen_list(src):
    """preamble() must consume injected conventions, not emit a hardcoded
    feedback list. The frozen marker phrases that should now come from live
    memory (UI-FIRST / render-structured) must NOT be hardcoded in the
    script, and preamble must reference a conventions parameter/variable."""
    # The living feedback conventions must NOT be frozen into the script.
    for frozen_marker in ("UI-FIRST", "render-structured", "progressive-disclosure"):
        assert frozen_marker not in src, (
            f"feedback convention '{frozen_marker}' is hardcoded in "
            "implement.js — it must arrive via context_bundle, not be frozen."
        )
    # preamble must take/consume an injected conventions argument.
    assert "function preamble(role, conventions" in src or (
        "preamble(" in src and "conventions" in src and "CONVENTIONS = [" not in src
    ), (
        "preamble() must inject bundle-derived conventions; the frozen "
        "`const CONVENTIONS = [...]` array must be gone."
    )


def test_static_procedural_spine_retained(src):
    """The hard procedural rules stay static in the preamble even after the
    feedback conventions move to the bundle."""
    for spine in (
        "Never commit to main",
        "30",          # ~30-line writes rule
        "Hooks are advisory",
    ):
        assert spine in src, (
            f"static procedural spine rule missing from implement.js: {spine!r}"
        )


def test_self_heal_memory_recall_fallback_retained(src):
    """memory_recall self-heal stays the FALLBACK path (SELF_HEAL block),
    not the primary convention source."""
    assert "SELF_HEAL" in src and "memory_recall" in src, (
        "SELF_HEAL block with memory_recall fallback must remain present."
    )
