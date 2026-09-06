"""The publish path redraws the Understand maps when work lands.

The owner's ask: completing work should rebuild the code map automatically,
as a real step in the publish workflow — not a thing someone remembers to
click. These tests pin the three things that make it a STEP rather than a
side effect: a drawn node, one implementation behind both entry points, and
an honest report of what the redraw is actually worth.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from prism_service.services import map_refresh

# tests/unit/<file> -> tests -> prism-service -> services -> repo root
_BEHAVIORS = (Path(__file__).resolve().parents[4] / ".prism" / "behaviors"
              / "conductor")


# --------------------------------------------------------------- the node

def test_the_step_is_a_drawn_node():
    """A node id that matches no drawn node can never paint the canvas."""
    node = json.loads((_BEHAVIORS / "refresh-maps.json").read_text())
    assert node["id"] == "refresh-maps"
    assert node["fsmId"] == "pipeline"
    assert node["botId"] == "conductor"
    assert node["steps"][0]["url"].endswith(
        "/api/workflows/steps/refresh-maps?project=${project}")


def test_the_node_is_registered_in_the_pipeline():
    bot = json.loads((_BEHAVIORS / "bot.json").read_text())
    ids = bot["fsms"][0]["behaviorIds"]
    assert "refresh-maps" in ids
    # After the play's knowledge is indexed, and before the worktree the map
    # may need is reaped.
    assert ids.index("brain-health") < ids.index("refresh-maps")
    assert ids.index("refresh-maps") < ids.index("reap")


# ------------------------------------------------------------- the redraw

class _Svc:
    """Stands in for ArchifyService; records which maps were rebuilt."""

    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.built: list[str] = []

    def build(self, kind, task_id=None):
        self.built.append(kind)
        out = self.outcomes[kind]
        if isinstance(out, Exception):
            raise out
        return out


def _patch(monkeypatch, svc, stale=False):
    import prism_service.services.archify_service as arch
    monkeypatch.setattr(arch, "ArchifyService", lambda project: svc)
    monkeypatch.setattr(map_refresh, "graph_is_stale", lambda project: stale)


def _ok(n=3, e=2):
    return {"ok": True, "components": n, "connections": e}


def test_every_project_map_is_redrawn(monkeypatch):
    svc = _Svc({k: _ok() for k in map_refresh.PROJECT_MAP_KINDS})
    _patch(monkeypatch, svc)
    out = map_refresh.refresh_maps("prism", task_id="t1")
    assert out["ok"] is True
    assert svc.built == list(map_refresh.PROJECT_MAP_KINDS)
    assert "code" in [r["kind"] for r in out["refreshed"]]


def test_the_task_map_is_not_redrawn_on_every_land():
    """A task map belongs to one task, not to the project."""
    assert "task" not in map_refresh.PROJECT_MAP_KINDS


def test_one_bad_map_never_stops_the_others(monkeypatch):
    svc = _Svc({"code": RuntimeError("graph.db is locked"),
                "concepts": _ok(), "language": _ok()})
    _patch(monkeypatch, svc)
    out = map_refresh.refresh_maps("prism")
    assert svc.built == ["code", "concepts", "language"]
    assert [r["kind"] for r in out["refreshed"]] == ["concepts", "language"]
    assert out["failed"][0]["kind"] == "code"
    assert "locked" in out["failed"][0]["reason"]
    assert out["ok"] is False


def test_a_map_that_did_not_validate_is_a_failure_not_a_refresh(monkeypatch):
    svc = _Svc({"code": {"ok": False, "error": "layout refused"},
                "concepts": _ok(), "language": _ok()})
    _patch(monkeypatch, svc)
    out = map_refresh.refresh_maps("prism")
    assert [r["kind"] for r in out["refreshed"]] == ["concepts", "language"]
    assert "layout refused" in out["failed"][0]["reason"]


def test_an_unavailable_renderer_is_reported_never_raised(monkeypatch):
    import prism_service.services.archify_service as arch

    def _boom(project):
        raise RuntimeError("node is missing")

    monkeypatch.setattr(arch, "ArchifyService", _boom)
    out = map_refresh.refresh_maps("prism")
    assert out["ok"] is False
    assert len(out["failed"]) == len(map_refresh.PROJECT_MAP_KINDS)


# ------------------------------------------------------------- the report

def test_a_stale_graph_is_said_out_loud(monkeypatch):
    """The code map is drawn from graph.db, and no step on the land path
    rebuilds that graph. Reporting the redraw as a refresh would overclaim."""
    svc = _Svc({k: _ok() for k in map_refresh.PROJECT_MAP_KINDS})
    _patch(monkeypatch, svc, stale=True)
    line = map_refresh.summarise(map_refresh.refresh_maps("prism"))
    assert "graph.db is behind the source" in line


def test_unknown_graph_freshness_never_reads_as_fresh(monkeypatch):
    svc = _Svc({k: _ok() for k in map_refresh.PROJECT_MAP_KINDS})
    _patch(monkeypatch, svc)
    monkeypatch.setattr(map_refresh, "graph_is_stale", lambda project: None)
    assert "freshness unknown" in map_refresh.summarise(
        map_refresh.refresh_maps("prism"))


def test_the_summary_names_the_maps_and_the_failures(monkeypatch):
    svc = _Svc({"code": _ok(12, 13), "concepts": _ok(40, 3),
                "language": RuntimeError("ontology locked")})
    _patch(monkeypatch, svc)
    line = map_refresh.summarise(map_refresh.refresh_maps("prism"))
    assert "code(12c/13e)" in line
    assert "concepts(40c/3e)" in line
    assert "language failed: ontology locked" in line


# ------------------------------------------- one implementation, two seats

def test_the_route_and_the_seat_call_the_same_function():
    """If the node and the ship seat drift, the drawn step stops describing
    what actually happens on a land."""
    api = (Path(__file__).resolve().parents[2] / "prism_service" / "api"
           / "workflows.py").read_text()
    ship = (Path(__file__).resolve().parents[2] / "prism_service" / "services"
            / "ship_worker.py").read_text()
    assert "map_refresh.refresh_maps(" in api
    assert "map_refresh.refresh_maps(" in ship
    assert '@router.post("/steps/refresh-maps")' in api


def test_the_ship_path_runs_the_step_after_a_land():
    ship = (Path(__file__).resolve().parents[2] / "prism_service" / "services"
            / "ship_worker.py").read_text()
    assert "_refresh_maps_after_land(task_svc, task_id, project)" in ship
    # It records on the task, so the outcome survives past the log buffer.
    assert 'action="refresh_maps"' in ship


def test_a_working_tree_project_reports_unknown_not_fresh(monkeypatch, tmp_path):
    """The only staleness signal PRISM has returns False for a project with
    no tracked remote — meaning "the proxy does not apply", not "the graph
    includes your landed commit". Reporting that as fresh is the defect."""
    (tmp_path / "graph.db").write_text("")
    monkeypatch.setattr("prism_service.config.project_data_dir",
                        lambda p: tmp_path)
    from prism_service.engines import understand_engine as ue
    monkeypatch.setattr(ue, "_read_state", lambda p: {"remote_url": ""})
    assert map_refresh.graph_is_stale("prism") is None


def test_a_missing_graph_is_stale_not_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr("prism_service.config.project_data_dir",
                        lambda p: tmp_path)
    assert map_refresh.graph_is_stale("prism") is True


def test_a_tracked_project_still_uses_the_real_signal(monkeypatch, tmp_path):
    (tmp_path / "graph.db").write_text("")
    monkeypatch.setattr("prism_service.config.project_data_dir",
                        lambda p: tmp_path)
    from prism_service.engines import understand_engine as ue
    monkeypatch.setattr(ue, "_read_state",
                        lambda p: {"remote_url": "https://example.invalid/r"})
    import prism_service.api.staleness as st
    monkeypatch.setattr(st, "_derived_from_source_stale", lambda p: True)
    assert map_refresh.graph_is_stale("prism") is True


def test_a_redraw_failure_never_fails_a_ship(monkeypatch):
    """A ship that already succeeded is never undone by a reading aid."""
    from prism_service.services import ship_worker

    class _Boom:
        def record_history(self, *a, **k):
            raise RuntimeError("history is down")

    monkeypatch.setattr(map_refresh, "refresh_maps",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    ship_worker._refresh_maps_after_land(_Boom(), "t1", "prism")  # must not raise
