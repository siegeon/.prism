"""Tests for the spec gap detector (services/magic_spec_gaps) — the
brain-side completeness/confidence checker that drives follow-up questions."""

from __future__ import annotations

from prism_service.services import magic_spec_gaps as g


def test_no_entities_is_blocking():
    r = g.check_spec({"db": "x", "module": "x", "entities": []})
    assert r["complete"] is False
    assert r["blocking"][0]["kind"] == "no_entities"


def test_orphan_fk_blocks_and_asks():
    spec = {"db": "x", "module": "x", "entities": [
        {"name": "orders", "fields": [{"name": "customer_id", "type": "INTEGER"}],
         "rules": [{"type": "fk", "field": "customer_id", "ref": "customers"}]}]}
    r = g.check_spec(spec)
    assert r["complete"] is False
    kinds = {b["kind"] for b in r["blocking"]}
    assert "orphan_fk" in kinds
    assert any("customers" in q for q in r["questions"])


def test_unlinked_id_field_is_clarifying_question():
    spec = {"db": "x", "module": "x", "entities": [
        {"name": "appointments",
         "fields": [{"name": "patient_id", "type": "INTEGER"}]}]}
    r = g.check_spec(spec)
    # not blocking (it's a suggestion), but a question is raised
    assert r["complete"] is True
    assert any(c["kind"] == "unlinked_fk" for c in r["clarifying"])
    assert any("patient" in q for q in r["questions"])


def test_status_field_without_values_asks_enum():
    spec = {"db": "x", "module": "x", "entities": [
        {"name": "tickets", "fields": [{"name": "status", "type": "TEXT"}]}]}
    r = g.check_spec(spec)
    assert any(c["kind"] == "missing_enum" for c in r["clarifying"])


def test_status_with_enum_rule_no_gap():
    spec = {"db": "x", "module": "x", "entities": [
        {"name": "tickets", "fields": [{"name": "status", "type": "TEXT"}],
         "rules": [{"type": "enum", "field": "status",
                    "values": ["open", "closed"]}]}]}
    kinds = {gp["kind"] for gp in g.find_gaps(spec)}
    assert "missing_enum" not in kinds


def test_numeric_field_asks_bound():
    spec = {"db": "x", "module": "x", "entities": [
        {"name": "products", "fields": [{"name": "price", "type": "REAL"}]}]}
    assert any(c["kind"] == "maybe_min" for c in g.check_spec(spec)["clarifying"])


def test_complete_spec_has_no_blocking_gaps():
    spec = {"db": "x", "module": "x", "entities": [
        {"name": "customers", "fields": [{"name": "name", "type": "TEXT"}]},
        {"name": "orders", "fields": [
            {"name": "customer_id", "type": "INTEGER"},
            {"name": "total", "type": "REAL"},
            {"name": "status", "type": "TEXT"}],
         "rules": [{"type": "fk", "field": "customer_id", "ref": "customers"},
                   {"type": "min", "field": "total", "value": 0},
                   {"type": "enum", "field": "status", "values": ["new", "paid"]}]}]}
    assert g.is_complete(spec) is True
    assert g.check_spec(spec)["complete"] is True
