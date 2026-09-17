"""Architect intake: bounded source retrieval, proposal, controls, then task write.

Magic owns execution order. These typed steps never advance an SDLC gate.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from urllib.parse import quote
from uuid import UUID, uuid4
from prism_service.models.task_intake import IntakeProposal, TaskContract

FIELDS = ("title", "description", "oracle", "proof_type", "allowed_files", "verify", "stop_if", "likely_misfire", "plan_doc")

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()

def facts(task):
    return {key: getattr(task, key, None) for key in FIELDS}

def location(ctx, task_id):
    return Path(ctx._data_dir) / "task-intake" / (str(UUID(task_id)) + ".json")

def save(ctx, task_id, packet):
    path = location(ctx, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)

def load(ctx, task_id):
    return json.loads(location(ctx, task_id).read_text(encoding="utf-8"))

def task_for_intake(ctx, task_id):
    task = ctx.task_svc.get(task_id)
    if task is None:
        raise ValueError("Task context is unavailable")
    if task.status != "pending" or task.workflow_step:
        raise ValueError("Intake can change only a pending task before its first SDLC step")
    return task

def gather(ctx, project, task_id, engine):
    from prism_service.services.claude_transcripts import _project_source_path
    from prism_service.services.ontology_rules import rule_catalog
    task = task_for_intake(ctx, task_id)
    request = facts(task)
    root = Path(_project_source_path(project)).resolve()
    if not root.is_dir():
        raise ValueError("Project source is unavailable")
    query = (task.title + " " + task.description)[:1800]
    sources = [{"id": "request", "kind": "request", "content": request}]
    source_hashes = {}
    # Indexed hits are pointers. Read current source before citing code.
    for hit in ctx.brain_svc.search(query, limit=5, task_id=task_id):
        path = Path(hit.get("source_file") or "")
        if not path.is_absolute():
            path = root / path
        path = path.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        relative = str(path.relative_to(root))
        source_hashes[relative] = digest(content)
        start = max(0, int(hit.get("line_start") or 1) - 1)
        sources.append({"id": "code:" + relative, "kind": "brain", "path": relative,
                        "content": "\n".join(content.splitlines()[start:start + 45])[:2500]})
    for entry in ctx.memory_svc.recall(query, limit=5):
        sources.append({"id": entry.id, "kind": "concept_memory", "name": entry.name,
                        "content": entry.description[:2000]})
    rules = rule_catalog(project)
    sources.append({"id": "ontology:rules", "kind": "ontology",
                    "content": [{k: r.get(k) for k in ("name", "title", "description", "message")} for r in rules]})
    model = Path(__file__).parents[1] / "ontology" / "model.ttl"
    model_text = model.read_text(encoding="utf-8")
    sources.append({"id": "ontology:model", "kind": "ontology", "hash": digest(model_text),
                    "content": [b for b in model_text.split("\n\n") if any("o:" + name + " " in b for name in ("Task", "Bot", "FSM", "Behavior", "Step", "StepResult", "AcceptanceCriterion"))]})
    backend = engine("/workflows/bots/conductor?project=" + quote(project, safe=""))
    sources.append({"id": "magic:conductor", "kind": "magic", "content": backend})
    packet = {"task_id": task_id, "input_hash": digest(request), "request": request,
              "root": str(root), "sources": sources, "source_hashes": source_hashes,
              "state": "gathered", "unresolved_questions": [], "inference_used": False}
    try:
        previous = load(ctx, task_id)
        if previous.get("input_hash") == packet["input_hash"] and previous.get("state") == "validated":
            packet["contract"] = previous["contract"]
    except FileNotFoundError:
        pass
    save(ctx, task_id, packet)
    ctx.task_svc.record_history(task_id, action="intake_gather", actor="architect",
        details=json.dumps({"sources": [s["id"] for s in sources], "input_hash": packet["input_hash"]}))
    return packet

def assert_fresh(ctx, task_id, packet):
    task = task_for_intake(ctx, task_id)
    if digest(facts(task)) != packet["input_hash"]:
        raise ValueError("Task inputs changed. Run intake again")
    for relative, expected in packet["source_hashes"].items():
        path = Path(packet["root"]) / relative
        if not path.is_file() or digest(path.read_text(encoding="utf-8", errors="replace")) != expected:
            raise ValueError("A cited source changed. Run intake again")
    return task

def validate(ctx, task_id, packet, contract):
    from prism_service.services.oracle_authoring import validate_for_authoring
    task = assert_fresh(ctx, task_id, packet)
    known = {s["id"] for s in packet["sources"]}
    for ac in contract.acceptance_criteria:
        if not set(ac.evidence_refs) <= known:
            raise ValueError("Acceptance criterion cites an unknown source: " + ac.id)
        if not (set(ac.evidence_refs) - {"request"}):
            raise ValueError("Acceptance criterion needs project evidence: " + ac.id)
    for relative in contract.allowed_files:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or any(c in relative for c in "*?["):
            raise ValueError("File scope must name exact project-relative files")
        if not (Path(packet["root"]) / path).resolve().is_relative_to(Path(packet["root"])):
            raise ValueError("File scope leaves the project")
    # Existing owner constraints are inputs, never overwritten by inference.
    for key in ("oracle", "proof_type", "allowed_files", "verify", "stop_if", "likely_misfire"):
        current = getattr(task, key, None)
        if current and current != getattr(contract, key):
            raise ValueError("The proposal changes an existing task constraint: " + key)
    _, errors = validate_for_authoring(oracle=contract.oracle, proof_type=contract.proof_type,
        verify=contract.verify, likely_misfire=contract.likely_misfire, allowed_files=contract.allowed_files)
    if errors:
        raise ValueError("; ".join(errors))
    return contract

def propose(ctx, project, task_id, infer):
    packet = load(ctx, task_id)
    task = assert_fresh(ctx, task_id, packet)
    # A previously validated typed contract is a reusable template binding.
    existing = packet.get("contract")
    if existing:
        contract = validate(ctx, task_id, packet, TaskContract.model_validate(existing))
        proposal = IntakeProposal(contract=contract)
    else:
        prompt = ("Act as the architect for task intake. Propose a task contract, not implementation or completion proof. "
            "Use only the request and cited project sources below. Each criterion must state given, when, then, a verification target, "
            "and evidence_refs from source ids. Name a user-reachable result. Include the production wiring in file scope. "
            "Preserve all existing task constraints. Do not claim a test ran or a feature exists. "
            "If intent or evidence is insufficient, return contract=null and precise unresolved_questions. "
            "Do not invent owner intent to fill a form.\n" + json.dumps({"request": packet["request"], "sources": packet["sources"]}))
        fields = infer(prompt, IntakeProposal.model_json_schema())
        proposal = IntakeProposal.model_validate(fields)
        packet["inference_used"] = True
    packet["unresolved_questions"] = proposal.unresolved_questions
    packet["contract"] = proposal.contract.model_dump() if proposal.contract else None
    packet["state"] = "unresolved" if proposal.unresolved_questions or not proposal.contract else "proposed"
    save(ctx, task_id, packet)
    return packet

def check(ctx, task_id):
    packet = load(ctx, task_id)
    if packet.get("unresolved_questions") or not packet.get("contract"):
        raise ValueError("Unresolved intake: " + "; ".join(packet.get("unresolved_questions") or ["No task contract"]))
    validate(ctx, task_id, packet, TaskContract.model_validate(packet["contract"]))
    packet["state"] = "validated"
    save(ctx, task_id, packet)
    return packet

def apply(ctx, task_id):
    packet = load(ctx, task_id)
    if packet["state"] == "applied":
        return packet
    if packet["state"] != "validated":
        raise ValueError("The task contract has not passed intake controls")
    contract = validate(ctx, task_id, packet, TaskContract.model_validate(packet["contract"]))
    task = ctx.task_svc.get(task_id)
    if task.plan_doc:
        raise ValueError("Intake must not overwrite an existing plan")
    lines = ["## Intake acceptance criteria", "These are proposed requirements, not completion evidence."]
    for ac in contract.acceptance_criteria:
        lines.extend([f"### {ac.id}", f"Given: {ac.given}", f"When: {ac.when}", f"Then: {ac.then}",
                      f"Verification: {ac.verification}", "Sources: " + ", ".join(ac.evidence_refs)])
    fields = contract.model_dump(exclude={"schema_version", "acceptance_criteria"})
    criteria = "\n\n".join(lines)
    ctx.task_svc.update(task_id, **fields, plan_doc=criteria,
        description=task.description + "\n\n" + criteria)
    packet["state"] = "applied"
    save(ctx, task_id, packet)
    ctx.task_svc.record_history(task_id, action="intake_apply", actor="architect",
        details=json.dumps({"input_hash": packet["input_hash"], "contract": contract.model_dump(), "inference_used": packet["inference_used"]}))
    return packet

def start(project, task_id, engine, *, retry=False):
    from prism_service.project_context import get_project
    task = task_for_intake(get_project(project), task_id)
    return engine(f"/workflows/bots/{quote(project, safe='')}/architect/pipeline/create-task",
        method="POST", body={"vars": {"taskId": task_id},
            "requestKey": "intake:" + task_id + ":" + digest(facts(task)) + (":" + str(uuid4()) if retry else "")})

def after_create(project, task):
    # Complete authored tasks retain the established creation contract.
    if task.workflow != "implement" or all(getattr(task, k, None) for k in ("oracle", "proof_type", "verify", "allowed_files", "stop_if")):
        return None
    from prism_service.api.workflows import _workflow_engine_json
    try:
        return {"status": "queued", **start(project, task.id, _workflow_engine_json)}
    except Exception as exc:
        return {"status": "unavailable", "reason": str(exc), "task_id": task.id}
