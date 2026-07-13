"""fd297cf0: the Builder minimum-change doctrine must reach the dev role ONLY,
and it must be respected in BOTH injection points (context_bundle's rule assets
AND the conductor_work per-job splice). A regression that re-globalizes the
doctrine would make the Verifier lazy / Steward shallow (misfire #2)."""
from types import SimpleNamespace

from prism_service.services.context_builder import role_rule_assets
from prism_service.api import conductor_flow as cf


def _ids(role):
    return {a.id for a in role_rule_assets(role)}


def test_minimum_change_is_dev_only():
    assert "rule:minimum-change" in _ids("dev")
    for role in ("qa", "sm", "architect", "general", None):
        assert "rule:minimum-change" not in _ids(role), role


def test_base_rules_reach_every_role():
    for role in ("dev", "qa", "sm", "architect"):
        assert "rule:mcp-first" in _ids(role)


def _job_for(step_id):
    return cf._job(SimpleNamespace(id="t", workflow_step=step_id,
                                   gate_state="none"))


def test_per_job_splice_matches_context_bundle_scoping():
    # dev agent step carries the doctrine; qa/sm agent steps do not.
    assert any("climb the ladder" in d
               for d in _job_for("implement_tasks")["doctrine"])
    assert not any("climb the ladder" in d
                   for d in _job_for("write_failing_tests")["doctrine"])
    assert not any("climb the ladder" in d
                   for d in _job_for("draft_story")["doctrine"])


def test_gates_are_doctrine_free():
    # a reviewer must never be nudged to be lazy.
    assert _job_for("red_gate")["doctrine"] == []
    assert _job_for("green_gate")["doctrine"] == []


def test_ui_first_carve_out_present():
    # the LOC-cut rung must not be allowed to kill a demonstrable UI surface.
    txt = next(a.content for a in role_rule_assets("dev")
               if a.id == "rule:minimum-change")
    assert "UI-FIRST" in txt and "regression" in txt
