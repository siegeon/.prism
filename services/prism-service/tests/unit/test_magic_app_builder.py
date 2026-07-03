"""Tests for the deterministic Magic app renderer (services/magic_app_builder).

The load-bearing guarantee: rendered Hyperlambda indentation is ALWAYS a
multiple of 3 spaces — this is the whole reason PRISM renders instead of
letting a small model emit raw Hyperlambda (which trips Magic's
whitespace-strict parser). Rule idioms (fk/min/enum) are the ones verified
live against magic-backend v22.10.10.
"""

from __future__ import annotations

import pytest

from prism_service.services import magic_app_builder as ab

SPEC = {
    "db": "shop", "module": "shop",
    "entities": [
        {"name": "customers", "fields": [{"name": "name", "type": "TEXT"}]},
        {"name": "orders", "fields": [
            {"name": "customer_id", "type": "INTEGER"},
            {"name": "total", "type": "REAL"},
            {"name": "status", "type": "TEXT"}],
         "rules": [
            {"type": "fk", "field": "customer_id", "ref": "customers"},
            {"type": "min", "field": "total", "value": 0},
            {"type": "enum", "field": "status", "values": ["new", "paid"]}]},
    ],
}


def _all_indents_multiple_of_3(hl: str) -> bool:
    for line in hl.splitlines():
        if not line.strip():
            continue
        lead = len(line) - len(line.lstrip(" "))
        if lead % 3 != 0:
            return False
    return True


def test_indentation_always_multiple_of_3():
    # The guarantee the 7B could not meet.
    app = ab.render_app(SPEC)
    assert _all_indents_multiple_of_3(app["schema"])
    for content in app["files"].values():
        assert _all_indents_multiple_of_3(content), content


def test_schema_has_pk_and_tables():
    ddl = ab.render_schema(SPEC)
    assert "sqlite.connect:shop" in ddl
    assert "CREATE TABLE IF NOT EXISTS customers (id INTEGER PRIMARY KEY" in ddl
    assert "CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY" in ddl


def test_fk_rule_emits_not_exists_guard():
    hl = ab.render_add_endpoint(SPEC["entities"][1], "shop")
    assert "SELECT id FROM customers WHERE id = @fk" in hl
    assert "not" in hl and "exists:x:@sqlite.select/*" in hl
    assert "customer_id must reference an existing customers" in hl


def test_min_rule_emits_lt_guard():
    hl = ab.render_add_endpoint(SPEC["entities"][1], "shop")
    assert "lt:x:@.arguments/*/total" in hl
    assert ".:decimal:0" in hl


def test_enum_rule_emits_flag_and_checks():
    hl = ab.render_add_endpoint(SPEC["entities"][1], "shop")
    assert ".valid_status:bool:false" in hl
    assert "eq:x:@.arguments/*/status" in hl
    assert "status must be one of new, paid" in hl


def test_list_endpoint_returns_nodes_inside_connect():
    hl = ab.render_list_endpoint(SPEC["entities"][0], "shop")
    lines = hl.splitlines()
    # return-nodes must be indented (inside the connect block) — gotcha #2
    rn = [l for l in lines if "return-nodes" in l][0]
    assert rn.startswith("   ")


def test_normalize_strips_id_field_and_fk_suffix():
    spec = {"db": "x", "module": "x", "entities": [
        {"name": "a", "fields": [{"name": "id", "type": "INTEGER"},
                                 {"name": "n", "type": "TEXT"}],
         "rules": [{"type": "fk", "field": "n", "ref": "b.id"}]}]}
    ab.normalize_spec(spec)
    names = [f["name"] for f in spec["entities"][0]["fields"]]
    assert "id" not in names and "n" in names
    assert spec["entities"][0]["rules"][0]["ref"] == "b"


def test_deploy_calls_execute_for_schema_and_each_file():
    calls = []
    ab.deploy_app(dict(SPEC), execute=lambda hl: calls.append(hl))
    # 1 schema + 2 files/entity * 2 entities = 5
    assert len(calls) == 5
    assert any("CREATE TABLE" in c for c in calls)
    assert sum("io.file.save" in c for c in calls) == 4
