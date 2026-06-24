"""Tests for deterministic MCP-side context pack assembly."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


@pytest.fixture
def project(tmp_path, monkeypatch):
    from prism_service import config as cfg
    from prism_service import project_context as pc

    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield "context-pack-test"
    pc._contexts.clear()


def _call(tool_name, arguments=None, project_id="context-pack-test"):
    from prism_service.mcp.tools import handle_tool

    return asyncio.run(
        handle_tool(tool_name, arguments or {}, project_id=project_id)
    )


def _json_text(result):
    assert len(result) == 1
    return json.loads(result[0].text)


def test_context_bundle_keeps_legacy_fields_and_adds_context_pack(project):
    payload = _json_text(_call("context_bundle", {"persona": "dev"}, project))

    for legacy_key in (
        "brain_context",
        "relevant_memory",
        "active_tasks",
        "workflow_state",
        "health",
    ):
        assert legacy_key in payload

    pack = payload["context_pack"]
    assert pack["schema"] == "prism.context_pack.v1"
    assert pack["request"]["persona"] == "dev"
    assert pack["determinism"]["llm_generated"] is False
    assert payload["role_card"]["id"] == "role-card:dev"
    assert payload["template"]["id"] == "template:dev-implementation"


def test_context_bundle_is_deterministic_for_same_inputs(project):
    first = _json_text(_call("context_bundle", {"persona": "qa"}, project))
    second = _json_text(_call("context_bundle", {"persona": "qa"}, project))

    assert first["asset_versions"] == second["asset_versions"]
    assert first["context_pack"]["role_card"] == second["context_pack"]["role_card"]
    assert first["context_pack"]["rules"] == second["context_pack"]["rules"]
    assert first["context_pack"]["template"] == second["context_pack"]["template"]


def test_context_bundle_persona_changes_role_card_and_template(project):
    dev = _json_text(_call("context_bundle", {"persona": "dev"}, project))
    qa = _json_text(_call("context_bundle", {"persona": "qa"}, project))

    assert dev["role_card"]["id"] != qa["role_card"]["id"]
    assert dev["template"]["id"] != qa["template"]["id"]


def test_context_bundle_keeps_single_index_brain_context(project):
    _call(
        "brain_index_doc",
        {
            "path": "src/context_pack_gate.py",
            "content": (
                "Developer context for dev persona.\n"
                "DEV_CONTEXT_SINGLE_INDEX should remain in system context."
            ),
            "domain": "py",
        },
        project,
    )

    payload = _json_text(_call("context_bundle", {"persona": "dev"}, project))

    assert "DEV_CONTEXT_SINGLE_INDEX" in payload["brain_context"]


def test_request_context_resets_after_block():
    from prism_service.config import DEFAULT_PROJECT
    from prism_service.mcp.request_context import (
        PrismRequestContext,
        get_request_context,
        use_request_context,
    )

    assert get_request_context().project_id == DEFAULT_PROJECT
    with use_request_context(PrismRequestContext(project_id="inside")):
        assert get_request_context().project_id == "inside"
    assert get_request_context().project_id == DEFAULT_PROJECT


# ── task 0c811636: push-inject importance-ranked conventions ───────────────
# These pin the USER-FACING seam end-to-end: a feedback-domain memory stored
# through the real `memory_store` MCP verb must reach the agent via the
# `context_bundle` MCP verb under a distinct bundle["conventions"] key,
# importance-ranked, deduped, and top-N capped. Asserting through the
# dispatcher (handle_tool) — NOT a ContextBuilder mock — so a dead/unwired
# code path cannot false-green.


def _store_feedback(name, importance, project, *, desc=None):
    # Descriptions must be DISTINCT enough to stay below the memory store's
    # 0.85 description-similarity dedup (memory_service.store), otherwise each
    # store invalidates the prior near-identical entry and only the last
    # survives. The name is embedded verbatim so each body is unique.
    return _json_text(_call(
        "memory_store",
        {
            "domain": "feedback",
            "name": name,
            "description": desc or _distinct_body(name),
            "type": "convention",
            "classification": "tactical",
            "importance": importance,
        },
        project,
    ))


# The memory store dedups on >0.85 description similarity (memory_service.store),
# so fixture bodies must be genuinely distinct prose — a shared template with
# only the name swapped stays ~0.97 similar and collapses to one entry.
_DISTINCT_PHRASES = [
    "Always render structured Hermes primitives, never a raw JSON blob.",
    "Every feature ships a visible UI surface; headless plumbing is not done.",
    "Long receipts collapse to a one-line summary with click-to-expand.",
    "Completed work leaves the active board entirely once a session exists.",
    "Conductor tiles animate every task phase including the initializing claim.",
    "Patch-bump PRISM_VERSION on every user-visible commit for the SPA footer.",
    "Gates are proof-carrying; the conductor task is the proof container.",
    "Done tasks absorb into Memory; never let finished work pile on the board.",
    "Borrow the source structure but reskin to the Hermes visual identity.",
    "One consolidated PR per workstream; do not stack a PR per subtask.",
    "Progressive disclosure is required for any wall of validation text.",
    "A red test is always your fault; find the root cause, never wave it off.",
]


def _distinct_body(name):
    # Deterministic, content-unique body per fixture name so no two collide.
    h = sum(ord(c) for c in name)
    phrase = _DISTINCT_PHRASES[h % len(_DISTINCT_PHRASES)]
    return f"[{name}] {phrase} Marker token {name}-{h} keeps this body unique."


def test_bundle_surfaces_feedback_conventions(project):
    """A feedback-domain memory must appear in bundle['conventions'] — proving
    domain='feedback' is recalled in addition to domain=persona (the old
    _recall_memory recalled domain=persona ONLY, so feedback never surfaced)."""
    _store_feedback("ui-first-marker", 9, project)

    payload = _json_text(_call("context_bundle", {"persona": "dev"}, project))

    assert "conventions" in payload, (
        "context_bundle must expose a distinct 'conventions' key"
    )
    names = [_conv_name(c) for c in payload["conventions"]]
    assert "ui-first-marker" in names, (
        f"feedback-domain convention missing from bundle['conventions']: {names}"
    )


def _conv_name(c):
    if isinstance(c, dict):
        return c.get("name")
    return getattr(c, "name", None)


def _conv_importance(c):
    if isinstance(c, dict):
        return c.get("importance")
    return getattr(c, "importance", None)


def test_conventions_ranked_by_importance_desc(project):
    """Conventions are returned highest-importance first."""
    _store_feedback("low-conv", 2, project)
    _store_feedback("high-conv", 10, project)
    _store_feedback("mid-conv", 6, project)

    payload = _json_text(_call("context_bundle", {"persona": "dev"}, project))
    imps = [_conv_importance(c) for c in payload["conventions"]]

    assert imps == sorted(imps, reverse=True), (
        f"conventions not importance-descending: {imps}"
    )
    assert _conv_name(payload["conventions"][0]) == "high-conv"


def test_conventions_top_n_cap_env_tunable(project, monkeypatch):
    """PRISM_CONTEXT_CONVENTIONS_N caps the count; the highest-importance N
    survive. Fails if the cap is removed (all N+1 returned)."""
    monkeypatch.setenv("PRISM_CONTEXT_CONVENTIONS_N", "3")
    for i in range(4):  # N+1 = 4 conventions, importances 1..4
        _store_feedback(f"cap-conv-{i}", i + 1, project)

    payload = _json_text(_call("context_bundle", {"persona": "dev"}, project))
    names = [_conv_name(c) for c in payload["conventions"]]

    assert len(payload["conventions"]) == 3, (
        f"top-N cap not applied (expected 3, got {len(payload['conventions'])})"
    )
    assert "cap-conv-0" not in names, (
        "lowest-importance convention should be dropped by the cap"
    )


def test_conventions_default_cap_is_8(project):
    """Default cap (no env) is 8."""
    for i in range(10):
        _store_feedback(f"def-conv-{i}", i + 1, project)

    payload = _json_text(_call("context_bundle", {"persona": "dev"}, project))
    assert len(payload["conventions"]) <= 8, (
        f"default cap should be 8, got {len(payload['conventions'])}"
    )


def test_conventions_deduped(project):
    """No duplicate convention entries across persona + feedback recalls."""
    _store_feedback("dupe-conv", 7, project)

    payload = _json_text(_call("context_bundle", {"persona": "dev"}, project))
    names = [_conv_name(c) for c in payload["conventions"]]

    assert len(names) == len(set(names)), f"duplicate conventions: {names}"
