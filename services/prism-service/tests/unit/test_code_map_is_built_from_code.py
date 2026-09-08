"""The code architecture map is built from the CODE, not from clusters.

The previous builder made each community from `graph_svc.communities()` a
"component": the label was the text after the last separator, the type was a
keyword match against that label, and the drawn lines were whichever pairs
landed next to each other on the grid. So the boxes read `brain`, `index`,
`resolve`, `ident` -- clipped cluster-label tails -- none of them carried a
code location, and the subtitle counted grid adjacencies as if they were the
codebase's dependencies.

Owner: "thats not even code archeture", "you are not looking at the code
correctly, and you aer munging the data", "when i click on one of the nodes
it does not take me to the skills at ale".

These pin the replacement: real directories, real edges, honest counts, and a
click target that xref can actually resolve.
"""

from __future__ import annotations

import pytest

from prism_service.services.archify_maps import code as code_map


class _FakeGraph:
    """Two runtime modules plus a test dir, with real-looking paths."""

    def file_graph(self) -> dict:
        return {
            "files": [
                {"file": "src/app/services/conductor.py", "entities": 40},
                {"file": "src/app/services/tasks.py", "entities": 10},
                {"file": "src/app/api/routes.py", "entities": 30},
                {"file": "src/app/web/pages/Home.tsx", "entities": 5},
                # Must not become a component, however big it is.
                {"file": "src/app/tests/unit/test_conductor.py", "entities": 900},
            ],
            "edges": [
                {"from": "src/app/services/conductor.py",
                 "to": "src/app/api/routes.py", "weight": 12},
                {"from": "src/app/api/routes.py",
                 "to": "src/app/services/tasks.py", "weight": 3},
                {"from": "src/app/web/pages/Home.tsx",
                 "to": "src/app/api/routes.py", "weight": 2},
                # Test edges are not architecture either.
                {"from": "src/app/tests/unit/test_conductor.py",
                 "to": "src/app/services/conductor.py", "weight": 500},
            ],
        }


@pytest.fixture
def ir(monkeypatch):
    monkeypatch.setattr(code_map, "get_project",
                        lambda project: type("C", (), {"graph_svc": _FakeGraph()})())
    return code_map.build("proj")


def _card(ir: dict, title: str) -> dict:
    return next(c for c in ir["cards"] if c["title"] == title)


def _dir_for(ir: dict, comp: dict) -> str:
    """The real directory a box stands for, read back off its own target."""
    return ir["x_targets"][comp["id"]].rsplit("/", 1)[0]


def test_components_are_real_directories(ir):
    """Every box stands for a directory that actually exists in the file list,
    and its label is a real path tail -- never a clipped cluster label."""
    dirs = {_dir_for(ir, c) for c in ir["components"]}
    assert dirs == {"src/app/services", "src/app/api", "src/app/web/pages"}
    labels = {c["label"] for c in ir["components"]}
    assert labels == {"services", "api", "pages"}


def test_tags_stay_inside_the_box(ir):
    """archify validates that a tag fits its component at the 6px legible
    minimum and FAILS THE BUILD otherwise -- a full path needed ~123px in a
    112px box. The tag says which tree the module sits in; the full location
    lives in x_targets."""
    assert {c["tag"] for c in ir["components"]} == {"app", "web"}
    assert all(len(c["tag"]) <= 18 for c in ir["components"])


def test_tests_are_excluded_and_said_so(ir):
    """Test code is real but is not runtime architecture; by entity count it
    would otherwise be the biggest box on the map."""
    assert all("tests" not in _dir_for(ir, c) for c in ir["components"])
    assert any("900" in item for item in _card(ir, "Source")["items"])


def test_counts_describe_the_code_not_the_drawing(ir):
    """THE COUNT MUST NOT FLATTER THE PICTURE. Only grid-adjacent pairs can be
    routed, so the number of LINES is a property of the layout. The subtitle
    reports real module dependencies, and the card says how many of them the
    drawing managed to show."""
    # Three DIRECTED module pairs survive, and direction matters for a
    # dependency: services->api (conductor->routes), api->services
    # (routes->tasks), pages->api (Home->routes). The 500-weight test edge is
    # excluded with its file.
    assert ir["meta"]["subtitle"] == "3 modules, 3 dependencies"
    drawn = _card(ir, "What is drawn")["items"][0]
    assert drawn.startswith(f"{len(ir['connections'])} of 3 module dependencies")
    assert len(ir["connections"]) <= 3


def test_every_box_has_a_resolvable_code_target(ir):
    """A directory answers kind:"unresolved" from xref, so a box that pointed
    at one was a dead click. Targets are the module's largest FILE, which
    resolves to kind:"code"."""
    targets = ir["x_targets"]
    assert len(targets) == len(ir["components"])
    real_files = {f["file"] for f in _FakeGraph().file_graph()["files"]}
    for comp in ir["components"]:
        target = targets[comp["id"]]
        assert target in real_files, target
    # The biggest file in the module, not an arbitrary one.
    services = next(c for c in ir["components"]
                    if _dir_for(ir, c) == "src/app/services")
    assert targets[services["id"]] == "src/app/services/conductor.py"


def test_type_comes_from_the_real_path(ir):
    """Roles are read off real directory names. The old builder keyword-matched
    the cluster's LABEL, so a cluster called anything containing "service" was
    backend whatever its files were."""
    by_dir = {_dir_for(ir, c): c["type"] for c in ir["components"]}
    assert by_dir["src/app/web/pages"] == "frontend"
    assert by_dir["src/app/api"] == "backend"


def test_the_cluster_heuristics_are_gone():
    """The label-clipper and the keyword type-guesser must not come back."""
    assert not hasattr(code_map, "_distinctive")
    assert not hasattr(code_map, "_infer_type")


def test_empty_graph_says_so_rather_than_drawing_nothing(monkeypatch):
    class _Empty:
        def file_graph(self) -> dict:
            return {"files": [], "edges": []}

    monkeypatch.setattr(code_map, "get_project",
                        lambda project: type("C", (), {"graph_svc": _Empty()})())
    out = code_map.build("proj")
    assert "no files" in out["meta"]["subtitle"]


def test_x_targets_never_reaches_archify(monkeypatch, tmp_path):
    """REGRESSION: archify's schema check refuses unknown top-level keys, and
    it runs in build() BEFORE render(). Popping x_targets inside render() was
    too late, so the live build failed outright with
    `/ must NOT have additional properties {"additionalProperty":"x_targets"}`
    while a direct render() call looked fine. The lift must happen before
    validate, and the targets must still arrive on meta.
    """
    from prism_service.services import archify_service as svc_mod

    seen: dict = {}

    def _fake_validate(self, diagram_type, ir):
        seen["validated_keys"] = set(ir)
        return {"ok": True}

    def _fake_render(self, kind, diagram_type, ir, task_id=None, targets=None):
        seen["rendered_keys"] = set(ir)
        return {"ok": True, "targets": targets or {}}

    monkeypatch.setattr(svc_mod.ArchifyService, "validate", _fake_validate)
    monkeypatch.setattr(svc_mod.ArchifyService, "render", _fake_render)
    monkeypatch.setattr(
        svc_mod, "build_ir",
        lambda project, kind, task_id=None: (
            "architecture", {"components": [], "x_targets": {"a": "src/a.py"}}),
    )
    monkeypatch.setattr(svc_mod, "project_data_dir", lambda project: tmp_path)

    meta = svc_mod.ArchifyService("proj").build("code")

    assert "x_targets" not in seen["validated_keys"], (
        "our own key reached archify's validator and failed the build")
    assert "x_targets" not in seen["rendered_keys"]
    assert meta["targets"] == {"a": "src/a.py"}, (
        "the targets must survive the lift, or every box becomes a dead click")
