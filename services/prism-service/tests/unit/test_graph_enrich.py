"""Tests for v6.1.12 graph cluster enrichment — the deterministic parts
(scope hashing, prompt rendering, JSON parsing). The inference call itself
(claude -p) is not exercised here."""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def test_input_hash_stable_and_membership_sensitive():
    from prism_service.services.graph_enrich import _input_hash
    a = _input_hash(["b.py", "a.py"])
    b = _input_hash(["a.py", "b.py"])  # order-independent
    assert a == b
    # adding a file changes the hash -> triggers re-enrichment ("data changed")
    assert _input_hash(["a.py", "b.py"]) != _input_hash(["a.py", "b.py", "c.py"])


def test_parse_extracts_name_and_purpose_from_json():
    from prism_service.services.graph_enrich import _parse
    name, purpose = _parse('Sure! {"name": "Service Layer", "purpose": "Backend APIs."} ')
    assert name == "Service Layer"
    assert purpose == "Backend APIs."


def test_parse_handles_fenced_or_noisy_output():
    from prism_service.services.graph_enrich import _parse
    name, purpose = _parse('```json\n{"name":"Web SPA Frontend","purpose":"React app"}\n```')
    assert name == "Web SPA Frontend"
    assert purpose == "React app"


def test_parse_returns_empty_on_garbage():
    from prism_service.services.graph_enrich import _parse
    assert _parse("no json here") == ("", "")


def test_render_prompt_includes_level_and_files():
    from prism_service.services.graph_enrich import render_prompt
    scope = {"scope_id": "prism-service", "level": 0,
             "files": ["a/x.py", "a/y.py"], "symbols": ["Foo", "bar"]}
    p = render_prompt(scope)
    assert "domain" in p          # level 0 -> domain
    assert "prism-service" in p
    assert "x.py" in p and "y.py" in p   # basenames listed
    assert "Foo" in p
