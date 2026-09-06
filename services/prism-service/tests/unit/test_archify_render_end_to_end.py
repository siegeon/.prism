"""A map is only real when node renders it.

test_archify_service.py covers the not-found paths, which never start the
renderer. These tests run the ACTUAL vendored archify binary and assert the
delivered artifact, because a service that returns tidy metadata while
producing no SVG passes every other test in this repo.
"""

from __future__ import annotations

import json
import shutil

import pytest

# The tests below reload prism_service.config to repoint DATA_DIR at a tmp
# dir. A reload mutates the module for the WHOLE session, and monkeypatch
# cannot undo it — that leaked the tmp dir into every later test and turned
# test_data_dir_isolation and the packaging/promotion suites red. This runs
# first, so its teardown runs LAST: after monkeypatch restores the ambient
# PRISM_DATA_DIR the conftest pinned, reload config once more so the module
# agrees with the environment again.
@pytest.fixture(autouse=True)
def _restore_config_module_after_reload():
    yield
    import importlib
    from prism_service import config as _cfg
    importlib.reload(_cfg)


pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)

_ARCH_IR = {
    "schema_version": 1,
    "diagram_type": "architecture",
    "meta": {"title": "Render probe", "visual_preset": "blueprint"},
    "layout": {"mode": "grid", "cols": 2, "cellW": 170, "cellH": 76},
    "components": [
        {"id": "api", "type": "backend", "label": "API", "row": 0, "col": 0},
        {"id": "db", "type": "database", "label": "Store", "row": 0, "col": 1},
    ],
    "connections": [{"from": "api", "to": "db", "label": "reads"}],
}


def _service(tmp_path, monkeypatch):
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    import importlib
    from prism_service import config as cfg
    importlib.reload(cfg)
    from prism_service.services import archify_service as mod
    importlib.reload(mod)
    return mod.ArchifyService("default")


def test_a_valid_ir_validates(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    assert svc.validate("architecture", _ARCH_IR).get("ok") is True


def test_an_invalid_ir_is_refused_with_a_reason(tmp_path, monkeypatch):
    """A refusal must carry why, or no driver can repair the map."""
    svc = _service(tmp_path, monkeypatch)
    broken = json.loads(json.dumps(_ARCH_IR))
    broken["connections"] = [{"from": "api", "to": "nope"}]
    out = svc.validate("architecture", broken)
    assert out.get("ok") is not True
    assert out.get("diagnostics") or out.get("error")


def test_validate_does_not_clobber_a_stored_map(tmp_path, monkeypatch):
    """Validating any kind used to write through the code map's own ir.json."""
    svc = _service(tmp_path, monkeypatch)
    svc.render("code", "architecture", _ARCH_IR)
    svc.validate("architecture", {"schema_version": 1, "diagram_type": "architecture"})
    assert svc.ir("code") == _ARCH_IR


def test_render_delivers_an_svg_and_honest_counts(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    meta = svc.render("code", "architecture", _ARCH_IR)

    assert meta["ok"] is True
    assert meta["components"] == 2, "counts come from the IR, not the receipt"
    assert meta["connections"] == 1
    assert meta["title"] == "Render probe"

    html = svc.html("code")
    assert html and "<svg" in html
    assert "API" in html and "Store" in html
    assert svc.receipt("code") is not None
    assert svc.meta("code")["html_url"].startswith("/api/archify/maps/code/html")


def test_the_rendered_page_is_safe_to_embed(tmp_path, monkeypatch):
    """The map is shown in an iframe on Understand, so it must not need a top
    window and must not pull a script from another origin."""
    svc = _service(tmp_path, monkeypatch)
    svc.render("code", "architecture", _ARCH_IR)
    html = svc.html("code")
    assert "window.top" not in html
    assert "<script src=" not in html


def test_a_built_map_is_listed(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch)
    svc.render("code", "architecture", _ARCH_IR)
    kinds = [m.get("kind") for m in svc.list_maps()]
    assert "code" in kinds


def test_every_registered_builder_produces_a_valid_diagram(tmp_path, monkeypatch):
    """The four builders must emit IR the renderer accepts, on THIS machine's
    real project data. A builder that only validates on a synthetic fixture
    has not been shown to work."""
    svc = _service(tmp_path, monkeypatch)
    from prism_service.services.archify_maps import BUILDERS

    assert set(BUILDERS) == {"code", "concepts", "language", "task"}
    for kind, module in BUILDERS.items():
        if kind == "task":
            continue  # needs a real task id; covered by the task map's own test
        diagram_type, ir = module.DIAGRAM_TYPE, module.build("default")
        out = svc.validate(diagram_type, ir)
        assert out.get("ok") is True, f"{kind} builder emitted invalid IR: {out}"


# ------------------------------------------------- the publish-time diff

def test_every_relationship_gets_a_stable_id(tmp_path, monkeypatch):
    """Archify's compare refuses a base whose connections carry no authored
    ids, so a map without them can be drawn but never diffed."""
    from prism_service.services import archify_service as mod
    ir = {"connections": [{"from": "a", "to": "b"}, {"from": "a", "to": "b"},
                          {"from": "9x", "to": "c"}]}
    mod.stamp_edge_ids(ir)
    ids = [c["id"] for c in ir["connections"]]
    assert ids[0] == "a-to-b"
    assert ids[1] == "a-to-b-2", "a repeated pair still needs a unique id"
    assert ids[2][0].isalpha(), "an id may not start with a digit"
    # Stable: stamping the same shape again yields the same ids.
    again = {"connections": [{"from": "a", "to": "b"}, {"from": "a", "to": "b"},
                             {"from": "9x", "to": "c"}]}
    assert [c["id"] for c in mod.stamp_edge_ids(again)["connections"]] == ids


def test_an_authored_id_is_never_overwritten():
    from prism_service.services import archify_service as mod
    ir = {"connections": [{"id": "mine", "from": "a", "to": "b"}]}
    assert mod.stamp_edge_ids(ir)["connections"][0]["id"] == "mine"


def test_a_workflow_maps_edges_are_stamped_too():
    from prism_service.services import archify_service as mod
    ir = {"edges": [{"from": "s1", "to": "s2"}]}
    assert mod.stamp_edge_ids(ir)["edges"][0]["id"] == "s1-to-s2"


def test_the_diff_reports_what_actually_moved(tmp_path, monkeypatch):
    """A publish must be able to say WHAT changed, not only that a new
    picture exists."""
    import copy
    svc = _service(tmp_path, monkeypatch)
    svc.render("code", "architecture", _ARCH_IR)
    base = copy.deepcopy(svc.ir("code"))
    base["components"][0]["label"] = "OLD NAME"
    out = svc.compare("code", base)
    assert out["ok"] is True, out["error"]
    assert out["changed"] == 1
    assert out["summary"]["components"]["changed"] == 1
    assert "<svg" in (svc.delta_html("code") or "")
    assert svc.delta_receipt("code") is not None


def test_an_identical_publish_reports_zero_not_a_failure(tmp_path, monkeypatch):
    import copy
    svc = _service(tmp_path, monkeypatch)
    svc.render("code", "architecture", _ARCH_IR)
    out = svc.compare("code", copy.deepcopy(svc.ir("code")))
    assert out["ok"] is True and out["changed"] == 0
