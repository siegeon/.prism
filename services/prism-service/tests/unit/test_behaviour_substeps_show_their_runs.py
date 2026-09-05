"""A behaviour's sub-nodes show the runs they actually did.

THE EMPTY CANVAS. Every sub-node of every conductor behaviour rendered
"too few runs (0/20)" on the Workflows page, no matter how often it ran --
so opening the node a task was standing in told you nothing about what was
happening. Measured live 2026-09-05: premise-gather had 149 recorded runs
and premise-citation-check 147, while the canvas showed a sample count of
zero for both.

The rows were never missing. get_workflows calls node_token_trend for the
conductor's own ten FSM steps and for nothing else, so a behaviour's steps
were built without the trend fields at all. This reads the SAME function
over the SAME scores.db, so a sub-node's number means exactly what a
conductor node's number means.
"""
from __future__ import annotations

import types

import pytest

from prism_service.api import workflows as wf


def test_substeps_carry_the_trend_fields(monkeypatch, tmp_path):
    steps = [{"id": "premise-gather"}, {"id": "premise-render"}]
    monkeypatch.setattr(wf, "node_token_trend", lambda db, ids: {
        "premise-gather": {"multiplier": 0.4, "avg_tokens": 900,
                           "sample_count": 149, "window": 20,
                           "indeterminate": False}})

    wf._attach_node_trend(tmp_path / "scores.db", steps)

    g = steps[0]
    assert g["token_sample_count"] == 149, g
    assert g["avg_tokens"] == 900 and g["token_multiplier"] == 0.4
    assert g["token_indeterminate"] is False
    # a step with no measured runs stays HONESTLY indeterminate rather than
    # claiming a fabricated 0 or 1.0
    r = steps[1]
    assert r["token_sample_count"] == 0
    assert r["token_indeterminate"] is True
    assert r["token_multiplier"] is None


def test_a_project_without_scores_degrades_honestly():
    """Several suites pass a bare double with no _data_dir, so no scores
    path reaches this. That must yield an indeterminate trend, never a 500
    and never an invented number."""
    steps = [{"id": "premise-gather"}]

    wf._attach_node_trend(None, steps)

    assert steps[0]["token_indeterminate"] is True
    assert steps[0]["token_sample_count"] == 0


def test_the_behaviour_builder_stamps_its_steps(monkeypatch, tmp_path):
    """The wiring itself: entries coming out of _conductor_behavior_workflows
    carry the fields, so the canvas has something to render."""
    def _engine(path, **_kw):
        if "/behaviors/" in path:
            return {"id": "review-previous-notes-loop", "steps": [
                {"id": "gather", "kind": "http-callback",
                 "url": "http://x/api/workflows/steps/premise-gather", "command": ""},
                {"id": "render", "kind": "http-callback",
                 "url": "http://x/api/workflows/steps/premise-render", "command": ""}]}
        return {"fsms": [{"fsmId": "pipeline",
                          "behaviorIds": ["review-previous-notes-loop"]}]}

    monkeypatch.setattr(wf, "_workflow_engine_json", _engine)
    monkeypatch.setattr(wf, "node_token_trend", lambda db, ids: {
        "gather": {"multiplier": 0.5, "avg_tokens": 800, "sample_count": 149,
                   "window": 20, "indeterminate": False}})

    entries = wf._conductor_behavior_workflows("prism")
    for e in entries:
        wf._attach_node_trend(tmp_path / "scores.db", e["steps"])

    assert entries, "the behaviour must still be built"
    by_id = {s["id"]: s for s in entries[0]["steps"]}
    assert by_id["gather"]["token_sample_count"] == 149, by_id["gather"]
    assert by_id["gather"]["token_indeterminate"] is False
    assert by_id["render"]["token_indeterminate"] is True


def test_the_trend_is_keyed_by_the_ROUTE_not_the_step_id(monkeypatch, tmp_path):
    """A behaviour step is named for its POSITION ("gather") while the run
    it performs is recorded under the ROUTE it calls ("premise-gather").
    Keying by id found nothing, so every sub-node stayed at 0 samples with
    149 real runs on file -- fields present and empty, which reads exactly
    like no data at all."""
    steps = [{"id": "gather",
              "url": "http://x/api/workflows/steps/premise-gather?project=p"},
             {"id": "check",
              "url": "http://x/api/workflows/steps/premise-citation-check?project=p"}]
    asked = {}

    def _trend(db, ids):
        asked["ids"] = list(ids)
        return {"premise-gather": {"multiplier": 0.4, "avg_tokens": 900,
                                   "sample_count": 149, "window": 20,
                                   "indeterminate": False}}

    monkeypatch.setattr(wf, "node_token_trend", _trend)
    wf._attach_node_trend(tmp_path / "scores.db", steps)

    assert asked["ids"] == ["premise-gather", "premise-citation-check"], asked
    assert steps[0]["token_sample_count"] == 149, steps[0]
    assert steps[0]["token_indeterminate"] is False
