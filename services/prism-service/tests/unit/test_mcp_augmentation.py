"""LL-09 tests — MCP-response augmentation middleware.

When a reflection candidate is pending, every PRISM MCP tool response
gets prefixed with a nudge header pointing at the prism-reflect sub-
agent. Rate-limited per session (5 min). Disable via env.

Parent task: 37932f3f · Sub-task LL-09.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


@pytest.fixture
def project(tmp_path, monkeypatch):
    from app import config as cfg
    from app import project_context as pc
    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    monkeypatch.delenv("PRISM_MCP_AUGMENT_NUDGES", raising=False)
    yield "test-ll-09"
    pc._contexts.clear()


def _call(tool_name, arguments=None, project_id="test-ll-09"):
    from app.mcp.tools import handle_tool
    return asyncio.run(
        handle_tool(tool_name, arguments or {}, project_id=project_id)
    )


def _text(result):
    assert len(result) == 1
    return result[0].text


def _seed_pending(project_id, task_id="T-pending"):
    """Stamp a pending candidate so augmentation has something to fire on."""
    _call("janitor_enqueue", {
        "task_id": task_id,
        "trigger": "task_done",
        "scope": {"task_ids": [task_id]},
    }, project_id=project_id)


# ----------------------------------------------------------------------


def test_mcp_response_augmented_when_pending(project):
    _seed_pending(project)
    out = _text(_call("task_list", project_id=project))
    # Prefix should be present
    assert "PRISM_REFLECTION_PENDING" in out, (
        "pending candidate exists — response should be prefixed with nudge"
    )
    # Original response body (JSON array) still present after the
    # delimiter.
    assert "---" in out


def test_mcp_response_not_augmented_when_none(project):
    # No candidates seeded
    out = _text(_call("task_list", project_id=project))
    assert "PRISM_REFLECTION_PENDING" not in out


def test_augmentation_rate_limited_5min_per_session(project):
    _seed_pending(project)
    first = _text(_call("task_list", project_id=project))
    assert "PRISM_REFLECTION_PENDING" in first
    # Second call within 5 min — should NOT re-nudge
    second = _text(_call("task_list", project_id=project))
    assert "PRISM_REFLECTION_PENDING" not in second, (
        "second call within the 5-min rate-limit window should not re-nudge"
    )


def test_augmentation_disabled_via_env(project, monkeypatch):
    monkeypatch.setenv("PRISM_MCP_AUGMENT_NUDGES", "false")
    _seed_pending(project)
    out = _text(_call("task_list", project_id=project))
    assert "PRISM_REFLECTION_PENDING" not in out


def test_augmentation_adds_under_10ms_overhead(project):
    # Baseline: no pending candidates
    n = 10
    t0 = time.perf_counter()
    for _ in range(n):
        _call("task_list", project_id=project)
    baseline = (time.perf_counter() - t0) / n

    # With pending candidate — augmentation kicks in
    _seed_pending(project)
    t0 = time.perf_counter()
    for _ in range(n):
        _call("task_list", project_id=project)
    with_aug = (time.perf_counter() - t0) / n

    overhead = with_aug - baseline
    # 10ms is forgiving on the rate-limited fast path.
    assert overhead < 0.010, (
        f"augmentation added {overhead * 1000:.2f}ms/call > 10ms budget"
    )
