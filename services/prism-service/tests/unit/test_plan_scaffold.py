"""RED scaffold — server-side plan scaffolder self-checked by the real
governance scorers (task ec6932b7, C3 of the PI-orchestration build,
parent 81b23574 FR-3).

Pins: rubric-exact plan_doc assembly (## Summary / ## Requirements FR-n
/ ## Acceptance Criteria AC-n with '- oracle:' markers), TEMPLATED
layer-neutral mermaid, model confined to the C2 pi_slots interface, and
a MANDATORY self-check via arc_governance.score_story_complete /
mermaid_parses / score_plan_coverage (compute_violations against seeded
principles) before the plan is handed to a gate (risk R2).

No inference: all model calls ride an injected stub.

FAILS today: prism_service.services.plan_scaffold does not exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _mod():
    from prism_service.services import plan_scaffold
    return plan_scaffold


def _gov():
    from prism_service.services import arc_governance
    return arc_governance


class StubModel:
    """Always-valid slot fill; counts calls."""

    def __init__(self):
        self.calls = 0

    def __call__(self, prompt: str, system: str = "") -> str:
        self.calls += 1
        return '{"value": "Stubbed slot text for the scaffolder test run"}'


CONTEXT = {"feature_ask": "plan scaffolder authors rubric passing stories"}

PRINCIPLES = [{"id": "ARC-T-1", "kind": "layer_rule",
               "from": "domain", "must_not_depend_on": "infrastructure"}]


# ── AC-1: scaffolded plan_doc passes the story rubric ──────────────────

def test_scaffold_doc_passes_story_rubric():
    m, g = _mod(), _gov()
    doc = m.scaffold_plan_doc(
        summary="A short summary paragraph for the scaffolded story.",
        frs=["first requirement text", "second requirement text"],
        acs=[("first criterion", "pytest -q tests/unit/test_x.py"),
             ("second criterion", "curl :8888/api/x returns 200")],
    )
    res = g.score_story_complete(
        {"story_md": doc}, g.load_rubrics()["story_complete"])
    assert res["ok"] is True, res
    assert "FR-1" in doc and "FR-2" in doc
    assert "- AC-1:" in doc and "- AC-2:" in doc
    assert doc.count("oracle:") >= 2


# ── AC-2: templated diagram parses, bare keyword first line ────────────

def test_templated_diagram_parses():
    m, g = _mod(), _gov()
    dia = m.scaffold_plan_diagram(
        "plan scaffolder", steps=["fill slots", "assemble", "self check"])
    assert g.mermaid_parses(dia) is True, dia
    first = dia.strip().splitlines()[0].strip()
    assert first == "flowchart TD", first
    assert g.mermaid_edges(dia), "template must carry extractable edges"


# ── AC-3: build_plan self-checks against a seeded principle store ──────

def test_build_plan_zero_violations_against_seeded_store(tmp_path):
    from prism_service.services.memory_service import MemoryService
    m, g = _mod(), _gov()
    mem = MemoryService(str(tmp_path / "mulch"))
    g.seed_default_principles(mem)
    stub = StubModel()
    out = m.build_plan(CONTEXT, model=stub, memory_svc=mem)
    assert out["ok"] is True, out
    assert out["checks"]["story"]["ok"] is True, out["checks"]
    assert out["checks"]["plan"]["ok"] is True, out["checks"]
    assert out["checks"]["plan"].get("violations") == [], out["checks"]
    assert g.mermaid_parses(out["plan_diagram"]) is True
    assert stub.calls > 0, "slot text must come from the injected model"


# ── AC-4: hostile principles → deterministic rename or honest fail ─────

def test_hostile_principles_rename_or_surface():
    m, g = _mod(), _gov()
    # a principle that names a TEMPLATED node id — must trigger rename.
    # Probe build_plan's own deterministic diagram (stub model) so the
    # hostile rule targets an edge that really exists in the output.
    probe = m.build_plan(CONTEXT, model=StubModel(), principles=PRINCIPLES)
    edge = g.mermaid_edges(probe["plan_diagram"])[0]
    hostile = [{"id": "ARC-H-1", "kind": "layer_rule",
                "from": edge["from"], "must_not_depend_on": edge["to"]}]
    out = m.build_plan(CONTEXT, model=StubModel(), principles=hostile)
    assert out["ok"] is True, out
    assert out.get("renamed") is True, "rename pass must be recorded"
    assert out["checks"]["plan"].get("violations") == [], out["checks"]
    # unfixable: EMPTY principles can never pass — surfaced, not swallowed
    bad = m.build_plan(CONTEXT, model=StubModel(), principles=[])
    assert bad["ok"] is False
    assert "principle" in str(bad["checks"]["plan"].get("reason", "")).lower()


# ── AC-5: model confined to the pi_slots interface ─────────────────────

def test_model_confined_to_slot_interface(monkeypatch):
    m = _mod()
    from prism_service.inference import local_llm

    def _boom(*a, **k):
        raise AssertionError("build_plan must not reach local_llm directly")

    monkeypatch.setattr(local_llm, "complete", _boom)
    stub = StubModel()
    out = m.build_plan(CONTEXT, model=stub, principles=PRINCIPLES)
    assert out["ok"] is True, out
    assert stub.calls > 0
    assert "Stubbed slot text" in out["plan_doc"]
