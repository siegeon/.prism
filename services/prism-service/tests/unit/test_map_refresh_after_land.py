"""The publish path redraws the Understand maps when work lands.

The owner's ask: completing work should rebuild the code map automatically,
as a real step in the publish workflow — not a thing someone remembers to
click. These tests pin the three things that make it a STEP rather than a
side effect: a drawn node, one implementation behind both entry points, and
an honest report of what the redraw is actually worth.
"""

from __future__ import annotations

import json
import re
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


def test_the_node_is_the_last_real_step_of_the_workflow():
    """Owner: 'its all a workflow, so just trigger it as the last step'.

    It is the last step that DOES anything: `reap` follows it and must stay
    terminal, because reap deletes the worktree and nothing may run after
    that (pinned in test_api_workflows). Redrawing needs the project's own
    stores, never the task worktree, so it sits immediately before reap.
    """
    bot = json.loads((_BEHAVIORS / "bot.json").read_text())
    ids = bot["fsms"][0]["behaviorIds"]
    assert ids[-1] == "reap", "reap deletes the worktree and stays last"
    assert ids[-2] == "refresh-maps", ids[-4:]
    assert ids.index("brain-health") < ids.index("refresh-maps")


# ------------------------------------------------------------- the redraw

class _Svc:
    """Stands in for ArchifyService; records what was rebuilt and diffed."""

    def __init__(self, outcomes, previous=None, diff=None):
        self.outcomes = outcomes
        self.previous = previous or {}
        self.diff = diff or {"ok": True, "changed": 0, "summary": {},
                             "error": ""}
        self.built: list[str] = []
        self.compared: list[str] = []
        self.order: list[str] = []

    def ir(self, kind, task_id=None):
        self.order.append(f"read:{kind}")
        return self.previous.get(kind)

    def build(self, kind, task_id=None):
        self.built.append(kind)
        self.order.append(f"build:{kind}")
        out = self.outcomes[kind]
        if isinstance(out, Exception):
            raise out
        return out

    def compare(self, kind, base_ir, task_id=None):
        self.compared.append(kind)
        self.order.append(f"diff:{kind}")
        return self.diff


def _patch(monkeypatch, svc, stale=False, graph=None):
    import prism_service.services.archify_service as arch
    monkeypatch.setattr(arch, "ArchifyService", lambda project: svc)
    monkeypatch.setattr(map_refresh, "graph_is_stale", lambda project: stale)
    monkeypatch.setattr(
        map_refresh, "rebuild_graph",
        lambda project: graph or {"ok": True, "nodes": 11007, "edges": 29197,
                                  "communities": 131, "error": ""})


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

def test_a_stale_graph_is_said_out_loud_when_it_was_not_recomputed(monkeypatch):
    """Staleness only means something when this pass did NOT rebuild."""
    svc = _Svc({k: _ok() for k in map_refresh.PROJECT_MAP_KINDS})
    _patch(monkeypatch, svc, stale=True)
    line = map_refresh.summarise(
        map_refresh.refresh_maps("prism", rebuild_the_graph=False))
    assert "graph.db is behind the source" in line


def test_unknown_graph_freshness_never_reads_as_fresh(monkeypatch):
    svc = _Svc({k: _ok() for k in map_refresh.PROJECT_MAP_KINDS})
    _patch(monkeypatch, svc)
    monkeypatch.setattr(map_refresh, "graph_is_stale", lambda project: None)
    assert "freshness unknown" in map_refresh.summarise(
        map_refresh.refresh_maps("prism", rebuild_the_graph=False))


def test_the_summary_names_the_maps_and_the_failures(monkeypatch):
    svc = _Svc({"code": _ok(12, 13), "concepts": _ok(40, 3),
                "language": RuntimeError("ontology locked")})
    _patch(monkeypatch, svc)
    line = map_refresh.summarise(map_refresh.refresh_maps("prism"))
    assert "code(12c/13e" in line
    assert "concepts(40c/3e" in line
    assert "language failed: ontology locked" in line


# ------------------------------------------- recompute, redraw, then diff

def test_the_graph_is_recomputed_before_the_maps_are_drawn(monkeypatch):
    """Redrawing without recomputing would never contain the landed code."""
    calls: list[str] = []
    svc = _Svc({k: _ok() for k in map_refresh.PROJECT_MAP_KINDS})
    import prism_service.services.archify_service as arch
    monkeypatch.setattr(arch, "ArchifyService", lambda project: svc)
    monkeypatch.setattr(map_refresh, "graph_is_stale", lambda p: False)

    def _graph(project):
        calls.append("graph")
        return {"ok": True, "nodes": 11007, "edges": 29197,
                "communities": 131, "error": ""}

    monkeypatch.setattr(map_refresh, "rebuild_graph", _graph)
    out = map_refresh.refresh_maps("prism")
    assert calls == ["graph"]
    assert svc.order[0].startswith("read:"), svc.order
    assert out["graph"]["nodes"] == 11007
    assert "graph rebuilt (11007 nodes/29197 edges)" in map_refresh.summarise(out)


def test_each_map_is_diffed_against_the_one_it_replaced(monkeypatch):
    svc = _Svc({k: _ok() for k in map_refresh.PROJECT_MAP_KINDS},
               previous={k: {"components": []} for k in map_refresh.PROJECT_MAP_KINDS},
               diff={"ok": True, "changed": 4, "summary": {}, "error": ""})
    _patch(monkeypatch, svc)
    out = map_refresh.refresh_maps("prism")
    assert svc.compared == list(map_refresh.PROJECT_MAP_KINDS)
    assert all(r["changed"] == 4 for r in out["refreshed"])
    assert "4 changed" in map_refresh.summarise(out)


def test_the_base_is_read_before_the_rebuild_overwrites_it(monkeypatch):
    """ir.json IS the diff's base, and build() overwrites it."""
    svc = _Svc({"code": _ok()}, previous={"code": {"components": []}})
    _patch(monkeypatch, svc)
    map_refresh.refresh_maps("prism", kinds=["code"])
    assert svc.order == ["read:code", "build:code", "diff:code"]


def test_a_first_draw_has_nothing_to_diff(monkeypatch):
    svc = _Svc({"code": _ok()}, previous={})
    _patch(monkeypatch, svc)
    out = map_refresh.refresh_maps("prism", kinds=["code"])
    assert svc.compared == []
    assert out["refreshed"][0]["changed"] is None
    assert "first draw" in map_refresh.summarise(out)


def test_a_publish_that_moved_nothing_says_unchanged(monkeypatch):
    """Zero change is a measurement, not a missing one."""
    svc = _Svc({"code": _ok()}, previous={"code": {"components": []}},
               diff={"ok": True, "changed": 0, "summary": {}, "error": ""})
    _patch(monkeypatch, svc)
    out = map_refresh.refresh_maps("prism", kinds=["code"])
    assert "unchanged" in map_refresh.summarise(out)


def test_a_failed_graph_rebuild_is_reported_not_hidden(monkeypatch):
    svc = _Svc({k: _ok() for k in map_refresh.PROJECT_MAP_KINDS})
    _patch(monkeypatch, svc,
           graph={"ok": False, "nodes": 0, "edges": 0, "communities": 0,
                  "error": "graphify produced no graph.json"})
    out = map_refresh.refresh_maps("prism")
    assert out["ok"] is False
    assert "graph rebuild failed: graphify produced no graph.json" in \
        map_refresh.summarise(out)


def test_a_broken_diff_never_loses_the_redraw(monkeypatch):
    svc = _Svc({"code": _ok()}, previous={"code": {"components": []}},
               diff={"ok": False, "changed": 0, "summary": {},
                     "error": "compare refused"})
    _patch(monkeypatch, svc)
    out = map_refresh.refresh_maps("prism", kinds=["code"])
    assert out["refreshed"][0]["components"] == 3  # the redraw still counts
    assert out["refreshed"][0]["diff_error"] == "compare refused"


def test_no_map_puts_a_clock_in_a_field_the_diff_reads(monkeypatch):
    """A timestamp in the drawing makes EVERY publish report a change.

    The concepts map carried "read <date> <hh:mm> UTC" in its subtitle, so
    each publish diffed as one changed fact even when nothing in the
    architecture had moved — noise that would teach a reader to ignore the
    number. Build time reaches the reader through meta.json instead.
    """
    from prism_service.services.archify_maps import BUILDERS

    for kind, module in BUILDERS.items():
        if kind == "task":
            continue  # needs a real task id
        try:
            meta = module.build("default").get("meta", {})
        except Exception:  # noqa: BLE001 - a store this test cannot reach
            continue
        text = f"{meta.get('title', '')} {meta.get('subtitle', '')}"
        assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{2}:\d{2}", text), (
            f"{kind} map writes a clock into a field the diff compares: {text}")


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
