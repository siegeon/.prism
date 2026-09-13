"""200ms bar (owner 2026-09-13): GET /api/workflows measured 14.2s under a
live background pass, 0.3s idle -- not an algorithmic cost, but ~20+
synchronous HTTP round trips to the AosWorkflows engine every single
request (one for the scripted validation definition, one for the conductor
bot, one MORE per behavior id). The engine's answers only change when a
`.prism/behaviors/**/*.json` file changes on disk, so `_conductor_behavior_workflows`
and `_project_validation_workflow` now memoize that engine-derived
structure in process (api/workflows.py's `_cached_engine_structure`),
keyed on a behavior-file mtime fingerprint plus a short TTL.

This pins: (1) a second call within the TTL window does NOT repeat the
engine round trip: the caller-facing count of real HTTP calls, not just an
internal counter, must drop; (2) a cache hit still returns an
independent copy, so one caller mutating its result (get_workflows always
does -- occupancy/live/task_count/tier are stamped onto every entry per
request) can never corrupt what the next cache hit serves; (3) touching a
`.prism/behaviors/*.json` file busts the cache so a real edit is picked
up without waiting out the TTL.
"""
from __future__ import annotations

import types

import pytest


@pytest.fixture(autouse=True)
def _reset_cache():
    from prism_service.api import workflows as wf
    wf._reset_catalog_structure_cache_for_tests()
    yield
    wf._reset_catalog_structure_cache_for_tests()


def _fake_bot_and_behaviors(calls):
    def _engine(path, method="GET", body=None):
        calls.append(path)
        if "/behaviors/" in path:
            behavior_id = path.rsplit("/", 1)[-1].split("?")[0]
            return {
                "id": behavior_id, "fsmId": "pipeline", "botId": "conductor",
                "name": behavior_id.title(), "version": 1,
                "steps": [{"id": "only", "kind": "shell", "command": "true",
                           "workingDirectory": "", "timeoutSeconds": 60}],
            }
        return {
            "id": "conductor", "name": "Conductor",
            "fsms": [{"fsmId": "pipeline", "behaviorIds": ["land"]}],
        }
    return _engine


def test_a_second_call_within_the_ttl_does_not_repeat_the_engine_round_trip(
    tmp_path, monkeypatch,
):
    from prism_service.api import workflows as wf

    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(wf, "_workflow_engine_json", _fake_bot_and_behaviors(calls))

    first = wf._conductor_behavior_workflows("prism")
    first_call_count = len(calls)
    assert first_call_count > 0, "the uncached path must still reach the engine once"

    second = wf._conductor_behavior_workflows("prism")

    assert len(calls) == first_call_count, (
        "a second call within the cache TTL must not repeat the engine "
        f"round trip; calls went {first_call_count} -> {len(calls)}"
    )
    assert [e["id"] for e in second] == [e["id"] for e in first]


def test_a_cache_hit_returns_an_independent_copy(tmp_path, monkeypatch):
    """get_workflows mutates the entries it gets back (occupancy, live,
    task_count, parent_id, tier) IN PLACE every request -- a cache that
    handed out the same object twice would let one request's stamped
    occupancy bleed into the next cache hit's answer."""
    from prism_service.api import workflows as wf

    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(wf, "_workflow_engine_json", _fake_bot_and_behaviors(calls))

    first = wf._conductor_behavior_workflows("prism")
    first[0]["occupancy"]["only"] = 999
    first[0]["parent_id"] = "conductor"

    second = wf._conductor_behavior_workflows("prism")

    assert second[0]["occupancy"]["only"] == 0, (
        "a cache hit must not carry forward another caller's in-place mutation"
    )
    assert "parent_id" not in second[0]


def test_editing_a_behavior_file_busts_the_cache(tmp_path, monkeypatch):
    from prism_service.api import workflows as wf

    monkeypatch.setattr(
        "prism_service.services.claude_transcripts._project_source_path",
        lambda project: str(tmp_path))
    calls: list[str] = []
    monkeypatch.setattr(wf, "_workflow_engine_json", _fake_bot_and_behaviors(calls))

    wf._conductor_behavior_workflows("prism")
    first_call_count = len(calls)

    behaviors_dir = tmp_path / ".prism" / "behaviors" / "conductor"
    behaviors_dir.mkdir(parents=True)
    (behaviors_dir / "land.json").write_text("{}", encoding="utf-8")

    wf._conductor_behavior_workflows("prism")

    assert len(calls) > first_call_count, (
        "adding/editing a .prism/behaviors file must bust the memoized "
        "engine structure, not wait out the TTL"
    )


def test_project_validation_workflow_is_also_memoized(monkeypatch):
    from prism_service.api import workflows as wf

    calls: list[str] = []

    def _engine(path, method="GET", body=None):
        calls.append(path)
        return {
            "id": "validation", "name": "Build and test", "project": "prism",
            "description": "d", "projectType": "python", "steps": [],
        }

    monkeypatch.setattr(wf, "_workflow_engine_json", _engine)

    wf._project_validation_workflow("prism")
    wf._project_validation_workflow("prism")

    assert len(calls) == 1, (
        f"a second call within the TTL must not repeat the engine call, saw {len(calls)}"
    )
