"""Tests for the deterministic spec -> Puck-JSON UI renderer.

Guarantees: one page per entity; every node type maps to a registered
component (EntityForm/EntityTable); rules become form affordances; the
render is pure (no network) and stable."""

from __future__ import annotations

from prism_service.services import magic_ui as ui

SPEC = {"db": "clinic", "module": "clinic", "entities": [
    {"name": "appointments", "fields": [
        {"name": "patient_id", "type": "INTEGER"},
        {"name": "status", "type": "TEXT"}],
     "rules": [
        {"type": "fk", "field": "patient_id", "ref": "patients"},
        {"type": "enum", "field": "status",
         "values": ["scheduled", "completed", "cancelled"]}]},
    {"name": "patients", "fields": [{"name": "name", "type": "TEXT"}]}]}

_ALLOWED = {"EntityForm", "EntityTable"}


def test_page_per_entity():
    out = ui.render_ui(SPEC)
    assert set(out["pages"]) == {"appointments", "patients"}
    assert out["module"] == "clinic"
    assert out["entities"] == ["appointments", "patients"]


def test_nodes_only_registered_components():
    out = ui.render_ui(SPEC)
    for page in out["pages"].values():
        for node in page["content"]:
            assert node["type"] in _ALLOWED       # Puck can't freestyle off-brand
            assert node["props"].get("id")         # every node addressable


def test_rules_become_form_affordances():
    page = ui.render_ui(SPEC)["pages"]["appointments"]
    form = next(n for n in page["content"] if n["type"] == "EntityForm")
    kinds = {r["type"] for r in form["props"]["rules"]}
    assert {"fk", "enum"} <= kinds
    enum = next(r for r in form["props"]["rules"] if r["type"] == "enum")
    assert enum["values"] == ["scheduled", "completed", "cancelled"]


def test_form_and_table_bind_live_endpoints():
    page = ui.render_ui(SPEC)["pages"]["appointments"]
    for n in page["content"]:
        assert n["props"]["module"] == "clinic"     # -> magic/modules/clinic/appointments
        assert n["props"]["entity"] == "appointments"


def test_tokens_present_and_overridable():
    out = ui.render_ui(SPEC)
    assert out["tokens"]["--app-brand"].startswith("#")
    custom = ui.render_ui(SPEC, tokens={"--app-brand": "#ff0000"})
    assert custom["tokens"]["--app-brand"] == "#ff0000"


def test_pure_deterministic():
    assert ui.render_ui(SPEC) == ui.render_ui(SPEC)   # no network, no randomness
