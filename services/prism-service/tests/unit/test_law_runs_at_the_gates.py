"""The law runs over a diff at the gates (task 2bfe49db, epic 61821448).

services/law_promotion.py (task c5650403) writes a promoted architecture
principle into a project's own ``ontology/promoted-shapes.ttl`` as a SHACL
SPARQLConstraint over ``o:imports`` between ``o:Module`` nodes. Before this
task that rule ran only on a full ontology rebuild. This suite pins
services/law_check.py: it runs the SAME promoted rule over a task's git
diff at green_gate, cheaply (a throwaway in-process rdflib graph, never
the worker subprocess, never the package's own built-in shapes.ttl).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=str(cwd),
                          capture_output=True, text=True)


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# The exact promoted-shapes.ttl body law_promotion.py's _draft_principle_rule
# would write for arc_governance.PRISM_PRINCIPLES's real
# "models must not depend on services" principle -- copied verbatim (name,
# select body, o:derivedFrom) rather than re-derived, so this suite pins
# the CONSUMER (law_check.py) against a REAL promoted-rule shape, not an
# invented one.
_FAKE_MEMORY_ID = "mx-fake01"
_PROMOTED_SHAPES_TTL = f"""\
# promoted-shapes.ttl -- rules promoted from Understand memory.

@prefix o: <urn:prism:onto:> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

o:models-must-not-depend-on-services.target a sh:NodeShape ;
    sh:targetClass o:Module ;
    sh:sparql o:models-must-not-depend-on-services .

o:models-must-not-depend-on-services a sh:SPARQLConstraint ;
    rdfs:comment "A module under models must not import a module under services." ;
    sh:name "models must not depend on services" ;
    sh:description "A module under models must not import a module under services." ;
    sh:message "a module under models imports a module under services" ;
    o:derivedFrom <urn:prism:onto:instance/memory/{_FAKE_MEMORY_ID}> ;
    sh:select \"\"\"
        PREFIX o: <urn:prism:onto:>
        SELECT $this WHERE {{
            $this a o:Module ; o:imports ?m .
            $this rdfs:label ?fromPath .
            ?m rdfs:label ?toPath .
            FILTER(STRSTARTS(?fromPath, "models") && STRSTARTS(?toPath, "services"))
        }}
    \"\"\" .
"""


def _init_repo_with_law(tmp_path):
    """A throwaway git repo carrying an existing prism_service/services/y.py
    (so an import of it resolves to a real in-repo file) plus this
    project's own promoted-shapes.ttl (task c5650403's install path:
    <project data dir>/ontology/promoted-shapes.ttl), all committed as the
    baseline so the diff-based check has a `baseline` rev to compare
    against."""
    repo = tmp_path / "repo"
    _git(["init", "-q", str(repo)], tmp_path)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "t"], repo)
    _write(repo / "prism_service" / "services" / "y.py",
          "def helper():\n    return 1\n")
    _write(repo / "prism_service" / "models" / "sibling.py",
          "def ok():\n    return 1\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    baseline = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    data_dir = tmp_path / "data"
    ontology_dir = data_dir / "lawproj" / "ontology"
    ontology_dir.mkdir(parents=True, exist_ok=True)
    (ontology_dir / "promoted-shapes.ttl").write_text(
        _PROMOTED_SHAPES_TTL, encoding="utf-8")
    return repo, baseline, data_dir


def _patch_project_data_dir(monkeypatch, data_dir: Path):
    from prism_service import config as config_mod
    monkeypatch.setattr(config_mod, "project_data_dir",
                        lambda project: data_dir / project)
    from prism_service.services import law_check
    monkeypatch.setattr(law_check, "project_data_dir",
                        lambda project: data_dir / project, raising=False)


# ---------------------------------------------------------------------------
# Core: law_violation_reason_for_diff(workspace, baseline, project)
# ---------------------------------------------------------------------------

def test_violating_import_fires_the_promoted_rule(tmp_path, monkeypatch):
    from prism_service.services import law_check

    repo, baseline, data_dir = _init_repo_with_law(tmp_path)
    monkeypatch.setattr(
        law_check, "_project_promoted_shapes_path",
        lambda project: data_dir / project / "ontology" / "promoted-shapes.ttl")

    _write(repo / "prism_service" / "models" / "x.py",
          "import prism_service.services.y\n")

    reason = law_check.law_violation_reason_for_diff(
        repo, baseline, "lawproj")

    assert reason, "a models->services import must fire the promoted rule"
    assert "models-must-not-depend-on-services" in reason
    assert "models/x.py" in reason
    assert "services/y.py" in reason
    assert _FAKE_MEMORY_ID in reason


def test_clean_import_within_the_same_area_is_not_refused(tmp_path, monkeypatch):
    from prism_service.services import law_check

    repo, baseline, data_dir = _init_repo_with_law(tmp_path)
    monkeypatch.setattr(
        law_check, "_project_promoted_shapes_path",
        lambda project: data_dir / project / "ontology" / "promoted-shapes.ttl")

    _write(repo / "prism_service" / "models" / "x.py",
          "import prism_service.models.sibling\n")

    reason = law_check.law_violation_reason_for_diff(
        repo, baseline, "lawproj")

    assert reason == "", (
        f"a models->models import must not fire the models/services "
        f"principle: {reason!r}")


def test_no_promoted_shapes_file_abstains(tmp_path, monkeypatch):
    from prism_service.services import law_check

    repo = tmp_path / "repo"
    _git(["init", "-q", str(repo)], tmp_path)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "t"], repo)
    _write(repo / "README.md", "baseline\n")
    _git(["add", "."], repo)
    _git(["commit", "-q", "-m", "baseline"], repo)
    baseline = _git(["rev-parse", "HEAD"], repo).stdout.strip()

    monkeypatch.setattr(
        law_check, "_project_promoted_shapes_path", lambda project: None)

    _write(repo / "prism_service" / "models" / "x.py",
          "import prism_service.services.y\n")

    reason = law_check.law_violation_reason_for_diff(
        repo, baseline, "noproj")
    assert reason == "", (
        f"no promoted-shapes.ttl on file must abstain, never refuse: "
        f"{reason!r}")


def test_syntax_error_file_does_not_raise(tmp_path, monkeypatch):
    from prism_service.services import law_check

    repo, baseline, data_dir = _init_repo_with_law(tmp_path)
    monkeypatch.setattr(
        law_check, "_project_promoted_shapes_path",
        lambda project: data_dir / project / "ontology" / "promoted-shapes.ttl")

    _write(repo / "prism_service" / "models" / "broken.py",
          "def broken(:\n    pass\n")

    reason = law_check.law_violation_reason_for_diff(
        repo, baseline, "lawproj")
    assert reason == "", (
        f"a file that fails to parse must never raise or be treated as a "
        f"violation: {reason!r}")


# ---------------------------------------------------------------------------
# Wired into the machine seat -- ConductorService.adjudicate_green_gate.
# Mirrors test_green_gate_requires_reachability.py's own doctrine: a
# truthy refusal PARKS pending with the reason (never failed).
# ---------------------------------------------------------------------------

_HTTP_ORACLE = "GET http://127.0.0.1:9/health returns 200 with the current build info"


def _gated_task(tmp_path, oracle=_HTTP_ORACLE, proof_type=""):
    from prism_service.services.task_service import TaskService
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    t = task_svc.create(title="adjudicate me", oracle=oracle,
                        proof_type=proof_type)
    task_svc.update(t.id, workflow_step="green_gate", gate_state="pending",
                    completion_proof="pytest run: 3 passed, 0 failed")
    return task_svc, task_svc.get(t.id)


def _conductor(tmp_path, task_svc):
    from prism_service.services.conductor_service import ConductorService
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    cond._project_name = "lawproj"
    return cond


def test_law_violation_parks_green_gate_naming_rule_module_and_memory(
        tmp_path, monkeypatch):
    from prism_service.services import law_check
    task_svc, task = _gated_task(tmp_path)
    cond = _conductor(tmp_path, task_svc)

    monkeypatch.setattr(
        law_check, "law_violation_reason",
        lambda t, project="default": (
            "Rule models-must-not-depend-on-services fires on this diff. "
            "Module models/x.py imports services/y.py. From memory "
            f"{_FAKE_MEMORY_ID} (Understand). Fix the import or ask for "
            "an exemption on the Queue."))

    res = cond.adjudicate_green_gate(task.id)
    assert res is None, (
        "a refused promoted-law check must never auto-approve the gate")
    after = task_svc.get(task.id)
    assert after.gate_state == "pending", (
        f"must park pending, never failed: got {after.gate_state!r}")
    assert "models-must-not-depend-on-services" in (after.gate_reason or "")
    assert "models/x.py" in (after.gate_reason or "")
    assert "services/y.py" in (after.gate_reason or "")
    assert _FAKE_MEMORY_ID in (after.gate_reason or "")


def test_quiet_law_check_does_not_block_an_otherwise_passing_gate(
        tmp_path, monkeypatch):
    from prism_service.services import oracle_spec as osp
    from prism_service.services import law_check
    from prism_service.services import reachability_check
    task_svc, task = _gated_task(tmp_path)
    cond = _conductor(tmp_path, task_svc)

    monkeypatch.setattr(law_check, "law_violation_reason",
                        lambda t, project="default": "")
    monkeypatch.setattr(reachability_check, "unreachable_entry_point_reason",
                        lambda t: "")

    class _R:
        adapter = osp.ADAPTER_HTTP
        job_id = "fresh-pass"
        tree_sha = "deadbeef"
        spec_hash = "sha256:fake"
        passed = True
        status = "passed"
        policy_hash = ""
        reason = "pytest run: 3 passed, 0 failed (test double)"
    monkeypatch.setattr(cond, "_oracle_receipt_refusal",
                        lambda *a, **k: ("", _R()))

    res = cond.adjudicate_green_gate(task.id)
    assert res is not None and res.get("ok") is True, (
        f"an empty law-check reason must not block an otherwise-passing "
        f"gate: {res}")
    assert task_svc.get(task.id).gate_state == "passed"


# ---------------------------------------------------------------------------
# UI: LinkedText.tsx (the shared renderer TaskDetailPage.tsx uses for both
# task.gate_reason and task.blocked_reason) source-reads a "Rule <name>"
# span to /ontology?tab=rules, an "mx-<id>" span to the Understand memory
# route, and a bare module path as plain code text. Source-read assertions
# only -- the SPA has no JS test runner (see CLAUDE.md's own convention).
# ---------------------------------------------------------------------------

def _linked_text_source() -> str:
    path = (_SERVICE_ROOT / "prism_service" / "web" / "src" / "components"
           / "LinkedText.tsx")
    return path.read_text(encoding="utf-8")


def test_linked_text_recognises_a_promoted_rule_name():
    src = _linked_text_source()
    assert "tab=rules" in src, (
        "LinkedText.tsx must link a 'Rule <name>' mention to the Ontology "
        "page's Rules tab")
    assert "Rule " in src


def test_linked_text_recognises_a_memory_id():
    src = _linked_text_source()
    assert "mx-" in src
    assert "understand?concept=" in src, (
        "LinkedText.tsx must link an 'mx-<id>' mention to the Understand "
        "memory route the app already uses")


def test_linked_text_renders_a_module_path_as_code():
    src = _linked_text_source()
    assert "<code" in src, (
        "LinkedText.tsx must render a bare module path as plain code "
        "text, not a link")
