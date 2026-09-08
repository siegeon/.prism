"""The Workflows endpoint must survive an unreachable workflow engine.

LIVE REGRESSION this pins. Every pull-request CI run in this repository
failed from at least 2026-09-06 to 2026-09-08 -- 12 of 12 runs across four
different task branches -- with five identical errors:

    fastapi.exceptions.HTTPException: 503: workflow engine unavailable:
    <urlopen error [Errno 111] Connection refused>

A GitHub runner has no AosWorkflows engine to reach, and
`_project_validation_workflow` let that raise, which took the WHOLE
get_workflows endpoint down. Because `.github/workflows/pr-checks.yml`
runs only on pull_request, nothing on main ever exercised it, so the break
stayed invisible while it blocked ship_worker at ci_wait for every task in
the repo. No task could ship.

Its sibling `_conductor_behavior_workflows` already degrades the same
failure to an empty list. This pins the same contract for the validation
entry: the engine is optional infrastructure, never a dependency of the
page.
"""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import HTTPException

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _dead_engine(monkeypatch):
    """Every engine read refuses, exactly as it does on a CI runner."""
    from prism_service.api import workflows as workflows_api

    def _refuse(*_a, **_k):
        raise HTTPException(
            503, "workflow engine unavailable: <urlopen error "
                 "[Errno 111] Connection refused>")

    monkeypatch.setattr(workflows_api, "_workflow_engine_json", _refuse)
    return workflows_api


def test_the_validation_entry_degrades_instead_of_raising(monkeypatch):
    api = _dead_engine(monkeypatch)

    entry = api._project_validation_workflow("prism")

    assert entry["steps"] == [], "no engine means no readable scripted steps"
    assert entry.get("unavailable") is True, (
        "the entry must SAY it could not be read, never look like a real "
        "workflow that genuinely has no steps")


def test_the_degraded_description_still_says_when_it_runs(monkeypatch):
    """The skill-description-says-when SHACL rule reads this text and fires
    without a real 'when' clause (task 408138e8), so the fallback carries
    the trigger sentence too."""
    api = _dead_engine(monkeypatch)

    description = api._project_validation_workflow("prism")["description"]

    assert "when" in description.lower(), (
        f"the fallback description states no trigger: {description!r}")


def test_the_whole_endpoint_still_answers_without_an_engine(monkeypatch):
    """The page is built from local constants apart from this one entry, so
    a dead engine must cost the reader that entry's steps and nothing else.
    This is the assertion that would have caught the CI break."""
    api = _dead_engine(monkeypatch)

    body = api.get_workflows("prism")

    ids = {w["id"] for w in body["workflows"]}
    assert "conductor" in ids, (
        "the conductor is built from WORKFLOW_STEPS, a local constant, and "
        "must survive an engine that is not answering")
    assert body["workflows"], "the endpoint returned no workflows at all"
