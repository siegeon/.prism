"""RED scaffold — violations.json per-SHA artifact (task 8579d49e, d1).

Pins: compute_violations(principles, layers) is a pure function diffing
INTENDED principles against the architecture_analyzer's OBSERVED
layers.json edges, and the result is stored beside the other understand
artifacts via understand_artifact_store under the analyzer key
'violations_analyzer' -> violations.json (per-SHA dir, cache semantics).

FAILS today: arc_governance does not exist and understand_artifact_store
raises ValueError('unknown analyzer') for 'violations_analyzer'
(_FILENAMES at understand_artifact_store.py:46-51 has no entry).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

PRINCIPLES = [{
    "id": "ARC-1", "kind": "layer_rule",
    "from": "domain", "must_not_depend_on": "infrastructure",
}]


def test_compute_violations_flags_forbidden_edge():
    from prism_service.services.arc_governance import compute_violations
    layers = {"edges": [
        {"from": "domain", "to": "infrastructure", "weight": 3},
        {"from": "api", "to": "domain", "weight": 1},
    ]}
    out = compute_violations(PRINCIPLES, layers)
    assert out["count"] == 1
    v = out["violations"][0]
    assert v["from"] == "domain" and v["to"] == "infrastructure"
    assert v["principle"] == "ARC-1"


def test_compute_violations_clean_edges_zero():
    from prism_service.services.arc_governance import compute_violations
    layers = {"edges": [{"from": "api", "to": "domain", "weight": 1}]}
    out = compute_violations(PRINCIPLES, layers)
    assert out["count"] == 0 and out["violations"] == []


@pytest.fixture
def isolated_projects_root(tmp_path, monkeypatch):
    from prism_service import config
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects")
    return tmp_path / "projects"


def test_violations_json_stored_per_sha_beside_layers(isolated_projects_root):
    from prism_service.services import understand_artifact_store as store
    sha = "deadbeefcafe"
    store.put("alpha", sha, "architecture_analyzer", {"edges": []})
    payload = {"count": 0, "violations": []}
    store.put("alpha", sha, "violations_analyzer", payload)

    vpath = store.sha_dir("alpha", sha) / "violations.json"
    assert vpath.exists(), (
        "violations.json must land beside layers.json in the per-SHA dir")
    assert (store.sha_dir("alpha", sha) / "layers.json").exists()

    got = store.get("alpha", sha, "violations_analyzer")
    assert got == payload, "round-trip through the artifact cache must hold"
