"""A memory in Understand becomes a rule or a term in the ontology (task
c5650403, epic 61821448: "Understand writes the law, the ontology holds
it, the code obeys it").

Pins:

  1. models.workflow.steps_for("promote_to_law") / known_workflows()
     register the workflow (draft -> review -> install -> done, ONE
     gate -- the same gate type the triage workflow's decide step uses).
  2. The Workflows page catalog (api/workflows.get_workflows) lists it
     with real step content.
  3. Its behaviour is a versioned per-project file
     (.prism/behaviors/promote_to_law.json), same mechanism
     align_language's behaviour uses.
  4. services.law_promotion.draft(): the three drafters --
       (a) a principle memory -> a SPARQLConstraint rule over o:imports
           between o:Module nodes;
       (b) a convention memory -> a rule SKELETON with a TODO sh:select
           body;
       (c) a memory naming one term -> an o:Term with altLabels.
  5. services.law_promotion.start_promotion creates ONE visible run task
     and drives it, through the same server-side conductor report path
     services/language_alignment_worker.py drives its own run task
     through, to the review gate -- which PARKS (gate_state=pending) for
     a distinct actor.
  6. After a distinct-actor approve, install_pending() writes the
     approved rule into the project's own promoted-shapes.ttl, proves the
     violating fixture fires (and the compliant one stays quiet), and the
     task reaches status=done. The installed rule appears in
     ontology_rules.rule_catalog(project) with derived_from set.
  7. install() on a term draft writes it into promoted-model.ttl; the
     term appears in ontology_terms.terms(project)'s lexicon vocabulary
     with derived_from set.
  8. OntologyPage.tsx source carries the "from mx-..." link on a
     promoted rule/term (no JS test runner in this repo -- UI ACs are
     pinned by asserting the real TSX source).
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest
import rdflib

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_ONTOLOGY_PAGE = _SRC / "pages" / "OntologyPage.tsx"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """Isolated ProjectContext (mirrors test_align_language_workflow.py's
    own fixture) so TaskService, MemoryService, the ontology stores, and
    the REST routers all resolve the SAME tmp-backed data dir."""
    from prism_service import config as cfg
    from prism_service import project_context as pc

    monkeypatch.setattr(cfg, "PROJECTS_DIR", tmp_path / "projects")
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    pc._contexts.clear()
    yield f"promote-to-law-{uuid.uuid4().hex[:8]}"
    pc._contexts.clear()


@pytest.fixture
def real_project(monkeypatch):
    """A project under the REAL default PROJECTS_DIR (mirrors
    test_rules_are_shacl_shapes.py's own seeded_project fixture), never
    monkeypatched. install()'s violating-fixture check runs SHACL in an
    isolated CHILD PROCESS (services/ontology_rules.py's run_shapes) that
    re-imports prism_service.config fresh -- an in-process monkeypatch of
    cfg.PROJECTS_DIR (the plain `project` fixture above) is invisible to
    that child, so a promoted-shapes.ttl written under the parent's
    monkeypatched tmp dir is never found by the worker. Real data dir,
    random uuid name, cleaned up via pc._contexts.clear() only (same as
    every other project-context test)."""
    from prism_service import project_context as pc

    pc._contexts.clear()
    yield f"promote-to-law-real-{uuid.uuid4().hex[:8]}"
    pc._contexts.clear()


@pytest.fixture
def no_real_worktree(tmp_path, monkeypatch):
    """flow_start creates a REAL git worktree by default (task_workspace.
    ensure_workspace). Stub it to a fake record (mirrors
    test_align_language_workflow.py's own fixture of the same name), and
    stub the behaviour file's location the same way."""
    from prism_service.api import conductor_flow as flow
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(
        flow.task_workspace, "ensure_workspace",
        lambda task_id, **kw: {"path": "/fake/ws", "branch": "x",
                               "baseline": "0" * 40, "repo_root": "/fake"},
    )
    monkeypatch.setattr(
        workflows_api, "_behavior_file",
        lambda project, workflow: tmp_path / ".prism" / "behaviors" / f"{workflow}.json",
    )


def _principle_memory(project: str, **overrides):
    from prism_service.project_context import get_project

    memory_svc = get_project(project).memory_svc
    kwargs = dict(
        domain="architecture-principles",
        name="ARC-PROMOTE-1",
        description="The API layer must not import the engines layer directly.",
        type="decision",
        classification="foundational",
        evidence={"principle": {
            "from": "prism_service/api",
            "must_not_depend_on": ["prism_service/engines"],
            "why": "the API layer must not import the engines layer directly",
        }},
        importance=8,
    )
    kwargs.update(overrides)
    return memory_svc.store(**kwargs)


# ── (1) registration ────────────────────────────────────────────────────

def test_promote_to_law_steps_and_known_workflows():
    from prism_service.models.task import known_workflows
    from prism_service.models.workflow import steps_for

    ids = [s["id"] for s in steps_for("promote_to_law")]
    assert ids == ["draft", "review", "install", "done"]
    assert "promote_to_law" in known_workflows()

    types = {s["id"]: s["type"] for s in steps_for("promote_to_law")}
    assert types == {"draft": "agent", "review": "gate",
                     "install": "agent", "done": "done"}
    gates = [s["id"] for s in steps_for("promote_to_law") if s["type"] == "gate"]
    assert gates == ["review"], (
        f"review must be the ONLY gate in promote_to_law, got {gates}")


# ── (2) catalog ──────────────────────────────────────────────────────────

def test_workflows_catalog_lists_promote_to_law_with_step_content(project, monkeypatch):
    from prism_service.api import workflows as workflows_api

    monkeypatch.setattr(
        workflows_api, "_project_validation_workflow",
        lambda proj: {"id": "validation", "name": "Build and test",
                      "description": "x", "project_type": "python",
                      "steps": [], "bots": [], "occupancy": {}},
    )

    result = workflows_api.get_workflows(project=project)
    entries = {w["id"]: w for w in result["workflows"]}
    assert "promote_to_law" in entries, [w["id"] for w in result["workflows"]]
    entry = entries["promote_to_law"]
    assert entry["name"] == "Promote to law"
    step_ids = [s["id"] for s in entry["steps"]]
    assert step_ids == ["draft", "review", "install", "done"]
    for step in entry["steps"]:
        assert step["input"] and step["action"] and step["output"]
    review = next(s for s in entry["steps"] if s["id"] == "review")
    assert review["authority"], "the review gate must name who decides it"
    assert "task_count" in entry


# ── (3) behaviour ────────────────────────────────────────────────────────

def test_promote_to_law_behavior_defaults_and_provide_bumps_version(tmp_path, monkeypatch, project):
    from prism_service.api import workflows as workflows_api

    target = tmp_path / ".prism" / "behaviors" / "promote_to_law.json"
    monkeypatch.setattr(workflows_api, "_behavior_file",
                        lambda proj, wf: target)

    got = workflows_api.get_workflow_behavior(project, "promote_to_law")
    assert got["path"] == "promote_to_law"
    behavior = got["behavior"]
    assert behavior["enabled"] is True
    assert behavior["require_fixture"] is True
    assert behavior["target"] == "project"
    assert behavior["behaviorVersion"] == 1

    result = workflows_api.provide_workflow_behavior(
        project, "promote_to_law", 1, {"enabled": False})
    assert result == {"ok": True, "path": "promote_to_law", "version": 2}
    assert target.exists()
    stored = json.loads(target.read_text())
    assert stored["enabled"] is False

    with pytest.raises(Exception) as stale:
        workflows_api.provide_workflow_behavior(
            project, "promote_to_law", 1, {"enabled": True})
    assert getattr(stale.value, "status_code", None) == 409


# ── (4) the three drafters ────────────────────────────────────────────────

def test_draft_principle_memory_produces_a_sparql_rule(project):
    from prism_service.services import law_promotion

    memory = _principle_memory(project)
    drafted = law_promotion.draft(memory, project)

    assert drafted["kind"] == "rule"
    assert drafted["derived_from"] == memory.id
    ttl = drafted["ttl"]
    assert "sh:SPARQLConstraint" in ttl
    assert "sh:targetClass o:Module" in ttl
    assert f"<{law_promotion._memory_iri(memory.id)}>" in ttl
    assert "o:imports" in ttl
    assert "STRSTARTS" in ttl
    assert "prism_service/api" in ttl
    assert "prism_service/engines" in ttl
    assert drafted["fixtures"]["compliant"]
    assert drafted["fixtures"]["violating"]
    # rdflib must actually parse the drafted TTL on its own.
    rdflib.Graph().parse(data=ttl, format="turtle", publicID=
                         "urn:prism:onto:")


def test_draft_convention_memory_produces_a_rule_skeleton(project):
    from prism_service.project_context import get_project
    from prism_service.services import law_promotion

    memory_svc = get_project(project).memory_svc
    memory = memory_svc.store(
        domain="conventions", name="never-commit-secrets",
        description="A commit must never add a .env file to the repo.",
        type="convention", classification="tactical",
        evidence={}, importance=6,
    )
    drafted = law_promotion.draft(memory, project)

    assert drafted["kind"] == "rule"
    assert drafted.get("needs_completion") is True
    assert f"TODO from memory {memory.id}" in drafted["ttl"]
    assert "sh:name" in drafted["ttl"]
    assert "sh:message" in drafted["ttl"]
    rdflib.Graph().parse(data=drafted["ttl"], format="turtle", publicID=
                         "urn:prism:onto:")


def test_draft_term_memory_produces_an_o_term_with_alt_labels(project):
    from prism_service.project_context import get_project
    from prism_service.services import law_promotion

    memory_svc = get_project(project).memory_svc
    memory = memory_svc.store(
        domain="conventions", name="retrieval-term",
        description="term: Retrieval is a search over the brain graph.",
        type="convention", classification="tactical",
        evidence={}, importance=5,
    )
    drafted = law_promotion.draft(memory, project)

    assert drafted["kind"] == "term"
    ttl = drafted["ttl"]
    assert "a o:Term" in ttl
    assert 'rdfs:label "Retrieval"' in ttl
    assert "o:altLabel" in ttl
    assert f"<{law_promotion._memory_iri(memory.id)}>" in ttl
    rdflib.Graph().parse(data=ttl, format="turtle", publicID="urn:prism:onto:")


# ── (5)+(6) the run: draft -> review (parked) -> approve -> install -> done ──

def test_principle_promotion_walks_to_review_then_installs_after_approval(
    real_project, no_real_worktree,
):
    project = real_project
    from prism_service.api import conductor_flow as flow
    from prism_service.project_context import get_project
    from prism_service.services import law_promotion, ontology_rules

    memory = _principle_memory(project)
    task_svc = get_project(project).task_svc

    started = law_promotion.start_promotion(project, memory.id)
    assert started["ok"], started
    run_task_id = started["run_task_id"]
    drafted = started["draft"]

    all_promote_tasks = [t for t in task_svc.list() if t.workflow == "promote_to_law"]
    assert len(all_promote_tasks) == 1, \
        f"exactly one run task must exist, got {[t.id for t in all_promote_tasks]}"

    task = task_svc.get(run_task_id)
    assert task.workflow == "promote_to_law"
    assert task.workflow_step == "review"
    assert task.gate_state == "pending", \
        "the review gate must PARK for a distinct actor, never auto-clear"
    assert drafted["name"] in (task.plan_doc or "")
    # An unset proof_type falls through conductor_service.py's demo-shaped
    # evidence path, which wants a trusted-runner oracle receipt a
    # promote_to_law task can never produce -- every run parked at
    # "review" showed BLOCKED / "evidence not on file" in the live UI no
    # matter how good the draft was (owner, 2026-08-27, live repro on
    # task 44c7e2d0). review is a pure human sign-off; it must declare
    # proof_type="review" so the readiness/approve path treats it as
    # human-judgment, never as a missing machine receipt.
    assert task.proof_type == "review", \
        "a promote_to_law run task must declare proof_type='review' so its " \
        "one owner gate is never blocked on a machine oracle receipt"

    # A distinct actor approves -- never the drafting seat's own session.
    # override=True: "review" carries no rubric validation kind (by
    # design -- a plain owner gate, the same shape triage's decide step
    # uses), so the wired VerifierService has nothing to check on its own
    # and the manual-review path requires the caller's explicit override,
    # same as any other non-rubric gate (green_gate's demo/review proof).
    approved = flow.flow_report(flow.Ident(
        task_id=run_task_id, session_id="owner-review",
        outcome="approved: matches ARC-PROMOTE-1",
        expected_step="review", override=True), project=project)
    assert approved["ok"], approved

    task = task_svc.get(run_task_id)
    # gate_decide's approve auto-advances PAST the gate to the next
    # non-gate step (install, an agent step) -- gate_state reflects the
    # NEW current step, not the just-decided one.
    assert task.workflow_step == "install"
    assert task.gate_state == "none"

    install_result = law_promotion.install_pending(project, task_id=run_task_id)
    assert install_result["ok"], install_result
    assert install_result["install"]["ok"] is True
    assert install_result["install"]["installed"] is True

    task = task_svc.get(run_task_id)
    assert task.status == "done"
    assert task.workflow_step == "done"

    # The installed rule loads for the PROJECT, with derived_from set.
    catalog = {r["name"]: r for r in ontology_rules.rule_catalog(project)}
    assert drafted["name"] in catalog, list(catalog)
    assert catalog[drafted["name"]]["derived_from"] == memory.id

    # A second install is idempotent -- no duplicate write, no error.
    again = law_promotion.install_pending(project, task_id=run_task_id)
    assert again["ok"] is False, \
        "a done task is no longer awaiting install -- must not re-run it"


def test_plain_approve_with_no_override_passes_the_review_gate(
    real_project, no_real_worktree,
):
    """Live repro (owner, 2026-08-27, task 44c7e2d0): a plain Approve click
    (no override) on the review gate failed every time with "gate has no
    validation kind" -- because review is a plain owner gate BY DESIGN
    (models/workflow.py's PROMOTE_TO_LAW_STEPS, the same shape triage's
    decide step uses), so it never had a rubric for the verifier to
    consult. gate_decide treated that "nothing to check" None the same as
    an explicit verifier REJECTION, and the live UI's Evidence tab has no
    override control at all -- the error text said "recover manually with
    override=True" while giving no way to do that, so the gate was
    permanently stuck for a human clicking the only Approve button the
    page has. Fixed in conductor_service.py's gate_decide: verified=None
    is now distinguished from an explicit False specifically when
    validation is also None (a step that never declared a rubric), and
    that case passes on a plain approve, same as the human's review
    already being the sign-off everywhere else this shape is used.
    """
    from prism_service.api import conductor_flow as flow
    from prism_service.services import law_promotion

    from prism_service.project_context import get_project as _get_project

    memory = _principle_memory(real_project, name="ARC-PROMOTE-2")
    task_svc = _get_project(real_project).task_svc

    started = law_promotion.start_promotion(real_project, memory.id)
    assert started["ok"], started
    run_task_id = started["run_task_id"]

    task = task_svc.get(run_task_id)
    assert task.workflow_step == "review"
    assert task.gate_state == "pending"

    # THE ACTUAL FIX UNDER TEST: override is NOT passed (defaults False) --
    # exactly what a plain Approve click on the live Evidence tab sends.
    approved = flow.flow_report(flow.Ident(
        task_id=run_task_id, session_id="owner-review-plain",
        outcome="approved: matches ARC-PROMOTE-2",
        expected_step="review"), project=real_project)
    assert approved["ok"], \
        f"a plain Approve on a by-design no-rubric gate must pass, not fail: {approved}"

    task = task_svc.get(run_task_id)
    assert task.workflow_step == "install"
    assert task.gate_state == "none"


def test_a_review_gate_stuck_failed_from_the_old_bug_recovers_on_a_plain_approve(
    real_project, no_real_worktree,
):
    """A task that hit the ORIGINAL bug before this fix existed (like the
    owner's own live task 44c7e2d0) is left with gate_state="failed" on
    disk. The first-approve fix above only helps a FRESH pending gate --
    a task already stuck in "failed" goes through gate_decide's SEPARATE
    recovery branch, which needed the identical plain-owner-gate carve-out
    or it would refuse forever, exactly like the live task did on this
    session's own recovery attempts ("Approve (recover)" resubmitting the
    same plain approve and hitting the same refusal).
    """
    from prism_service.api import conductor_flow as flow
    from prism_service.services import law_promotion
    from prism_service.project_context import get_project as _get_project

    memory = _principle_memory(real_project, name="ARC-PROMOTE-3")
    task_svc = _get_project(real_project).task_svc

    started = law_promotion.start_promotion(real_project, memory.id)
    assert started["ok"], started
    run_task_id = started["run_task_id"]

    # Simulate the pre-fix stuck state directly (the real task 44c7e2d0's
    # own on-disk shape after this session's first Approve attempts,
    # before either fix existed).
    task_svc.update(run_task_id, gate_state="failed",
                     gate_reason="gate has no validation kind")
    task = task_svc.get(run_task_id)
    assert task.gate_state == "failed"

    recovered = flow.flow_report(flow.Ident(
        task_id=run_task_id, session_id="owner-review-recover",
        outcome="approved: matches ARC-PROMOTE-3",
        expected_step="review"), project=real_project)
    assert recovered["ok"], \
        f"a stuck plain-owner gate must recover on a plain approve, not refuse forever: {recovered}"

    task = task_svc.get(run_task_id)
    assert task.workflow_step == "install"
    assert task.gate_state == "none"


def test_review_gate_readiness_is_the_human_judgment_path_not_a_missing_receipt(
    real_project, no_real_worktree,
):
    """The exact live-UI bug (owner, 2026-08-27, task 44c7e2d0): the
    Evidence tab's Approve control read "BLOCKED - evidence not on file"
    for a review-step task with a genuinely good draft, because an unset
    proof_type falls into api/conductor.py:gate_readiness's generic
    trusted-runner EvidenceReceipt branch, which a promote_to_law task can
    never satisfy (it has no pinned pytest oracle). Calls the SAME
    gate_readiness function the live Approve button's card reads, not
    just the stored task field, so a regression here reproduces the
    actual UI symptom, not just a missing attribute.
    """
    from prism_service.api.conductor import gate_readiness
    from prism_service.services import law_promotion

    memory = _principle_memory(real_project)
    started = law_promotion.start_promotion(real_project, memory.id)
    assert started["ok"], started
    run_task_id = started["run_task_id"]

    readiness = gate_readiness(run_task_id, project=real_project)

    assert readiness["receipt_ok"] is True, \
        f"review must be READY to approve, not blocked on a missing machine receipt: {readiness}"
    assert readiness.get("manual_review") is True
    assert readiness["receipt"]["adapter"] == "human", \
        f"review's evidence tooth must be the human-judgment path, not a trusted-runner receipt: {readiness}"
    reason = str(readiness["receipt"].get("reason") or "")
    assert "evidence not on file" not in reason.lower()
    assert "missing" not in reason.lower()


def test_installed_rule_fires_on_its_violating_fixture_and_stays_quiet_on_compliant(
    real_project, no_real_worktree,
):
    project = real_project
    from prism_service.api import conductor_flow as flow
    from prism_service.project_context import get_project
    from prism_service.services import law_promotion, ontology_rules
    from prism_service.services.ontology_graph import OntologyGraph

    memory = _principle_memory(project)
    task_svc = get_project(project).task_svc

    started = law_promotion.start_promotion(project, memory.id)
    drafted = started["draft"]
    run_task_id = started["run_task_id"]

    flow.flow_report(flow.Ident(
        task_id=run_task_id, session_id="owner-review",
        outcome="approved", expected_step="review", override=True),
        project=project)
    result = law_promotion.install_pending(project, task_id=run_task_id)
    assert result["ok"], result

    tbox = OntologyGraph(project).to_rdflib()

    violating = rdflib.Graph()
    violating += tbox
    violating.parse(data=drafted["fixtures"]["violating"], format="turtle",
                    publicID="urn:prism:onto:")
    _inferred, violations = ontology_rules.run_shapes(violating, project)
    assert drafted["name"] in violations, (
        drafted["name"], list(violations),
        "the installed rule did not fire on its own violating fixture "
        "-- a rule that cannot fail is decoration")

    compliant = rdflib.Graph()
    compliant += tbox
    compliant.parse(data=drafted["fixtures"]["compliant"], format="turtle",
                    publicID="urn:prism:onto:")
    _inferred2, quiet = ontology_rules.run_shapes(compliant, project)
    assert drafted["name"] not in quiet, (
        drafted["name"], list(quiet),
        "the installed rule fired on its own compliant fixture")

    # o:verifiedBy: the fixture proof above becomes a durable, committed
    # regression test the moment it passes, linked from the rule's own
    # URI (owner 2026-08-27: "the ontology should work with the code and
    # the rules to ensure that rules are covered by unit/int tests...
    # tied back to the code").
    from prism_service.config import project_data_dir
    from prism_service.services import task_workspace

    slug = drafted["name"].replace("-", "_")
    test_rel = f"services/prism-service/tests/unit/law/test_promoted_{slug}.py"
    fn_name = f"test_{slug}_fires_on_violating_and_stays_quiet_on_compliant"
    test_ref = f"{test_rel}::{fn_name}"

    repo_root = task_workspace._prism_repo_root()
    test_path = repo_root / test_rel
    assert test_path.exists(), test_path
    content = test_path.read_text(encoding="utf-8")
    assert "RULE_TTL" in content, content
    assert "VIOLATING_FIXTURE" in content, content
    assert "COMPLIANT_FIXTURE" in content, content
    assert f"def {fn_name}" in content, content

    shapes_path = project_data_dir(project) / "ontology" / "promoted-shapes.ttl"
    shapes_text = shapes_path.read_text(encoding="utf-8")
    assert f'o:{drafted["name"]} o:verifiedBy "{test_ref}" .' in shapes_text, shapes_text

    catalog = ontology_rules.rule_catalog(project)
    row = next(r for r in catalog if r["name"] == drafted["name"])
    assert row.get("verified_by") == test_ref, row


def test_generated_verification_test_for_the_promoted_rule_actually_runs_green(
    real_project, no_real_worktree,
):
    """The fixture proof _install_rule() turns into a test file is not
    just well-formed-looking text -- run IT, standalone, via a fresh
    pytest invocation, and require it to pass for real."""
    import subprocess
    import sys

    project = real_project
    from prism_service.api import conductor_flow as flow
    from prism_service.project_context import get_project
    from prism_service.services import law_promotion, task_workspace

    memory = _principle_memory(project)
    task_svc = get_project(project).task_svc
    assert task_svc is not None  # keeps the import honest / used

    started = law_promotion.start_promotion(project, memory.id)
    drafted = started["draft"]
    run_task_id = started["run_task_id"]

    flow.flow_report(flow.Ident(
        task_id=run_task_id, session_id="owner-review",
        outcome="approved", expected_step="review", override=True),
        project=project)
    result = law_promotion.install_pending(project, task_id=run_task_id)
    assert result["ok"], result

    slug = drafted["name"].replace("-", "_")
    repo_root = task_workspace._prism_repo_root()
    test_path = (repo_root / "services" / "prism-service" / "tests" /
                 "unit" / "law" / f"test_promoted_{slug}.py")
    assert test_path.exists(), test_path

    service_root = repo_root / "services" / "prism-service"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_path), "-q",
         "-o", "faulthandler_timeout=120"],
        cwd=str(service_root), capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 passed" in proc.stdout, proc.stdout


def test_install_refuses_a_rule_skeleton_with_no_demonstrable_fixture(project):
    from prism_service.project_context import get_project
    from prism_service.services import law_promotion

    memory_svc = get_project(project).memory_svc
    memory = memory_svc.store(
        domain="conventions", name="a-skeleton-rule",
        description="A rule with no real check yet.",
        type="convention", classification="tactical",
        evidence={}, importance=5,
    )
    drafted = law_promotion.draft(memory, project)
    assert drafted.get("needs_completion") is True

    result = law_promotion.install(drafted, project)
    assert result["ok"] is False
    assert "refused" in result["reason"].lower()

    path = None
    from prism_service.config import project_data_dir
    path = project_data_dir(project) / "ontology" / "promoted-shapes.ttl"
    if path.exists():
        assert drafted["name"] not in path.read_text(encoding="utf-8")


# ── (7) a promoted term surfaces in terms(project) ───────────────────────

def test_installed_term_appears_in_terms_with_derived_from(project):
    from prism_service.project_context import get_project
    from prism_service.services import law_promotion, ontology_terms

    memory_svc = get_project(project).memory_svc
    memory = memory_svc.store(
        domain="conventions", name="convergence-term",
        description="term: Convergence is when a drive reaches a stable state.",
        type="convention", classification="tactical",
        evidence={}, importance=5,
    )
    drafted = law_promotion.draft(memory, project)
    assert drafted["kind"] == "term"

    result = law_promotion.install(drafted, project)
    assert result["ok"], result
    assert result["installed"] is True

    payload = ontology_terms.terms(project)
    lexicon = next(v for v in payload["vocabularies"] if v["name"] == "lexicon")
    row = next((t for t in lexicon["terms"] if t["value"] == "Convergence"), None)
    assert row is not None, [t["value"] for t in lexicon["terms"]]
    assert row["derived_from"] == memory.id

    # Idempotent: installing the same draft twice does not duplicate it.
    again = law_promotion.install(drafted, project)
    assert again["installed"] is False
    payload2 = ontology_terms.terms(project)
    lexicon2 = next(v for v in payload2["vocabularies"] if v["name"] == "lexicon")
    matches = [t for t in lexicon2["terms"] if t["value"] == "Convergence"]
    assert len(matches) == 1, matches


# ── (8) UI source assertion (no JS test runner in this repo) ─────────────

def test_ontology_page_renders_derived_from_link():
    src = _ONTOLOGY_PAGE.read_text(encoding="utf-8")
    assert "derived_from" in src
    assert "/understand?concept=" in src
    assert "DerivedFromLink" in src
    # Rendered on both the Rules tab and the Terms tab.
    assert src.count("DerivedFromLink") >= 3  # definition + 2 call sites
