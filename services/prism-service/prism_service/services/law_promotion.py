"""law_promotion — a memory in Understand becomes a rule or a term in the
ontology (task c5650403, epic 61821448: "Understand writes the law, the
ontology holds it, the code obeys it").

``draft(memory, project)`` reads one memory and writes a candidate SHACL
rule or lexicon term, with its own compliant and violating fixture, never
touching disk. ``start_promotion(project, memory_id)`` creates ONE visible
run task (workflow=``promote_to_law``) and drives it, through the SAME
server-side conductor report path ``services/language_alignment_worker.py``
drives its own run task through (``api/conductor_flow.flow_start`` /
``flow_report``), to the ``review`` gate -- the ONE owner stop this
workflow has. The gate parks; a distinct actor decides it (never this
module's own seat). ``install_pending`` runs after approval: it writes the
approved draft into the project's own law and proves the violating
fixture fires before it commits -- a draft that cannot demonstrate its
own violation is refused, never installed quiet.

Two files hold a project's own promoted law, both under
``<project data dir>/ontology/``:

  promoted-shapes.ttl   rules -- read by services/ontology_rules.py's
                         rule_catalog(project) / validate(project)
  promoted-model.ttl     terms, plus the o:derivedFrom property every
                         promoted rule and term carries -- read by
                         services/ontology_graph.py's load_model() and
                         merged into services/ontology_terms.py's lexicon
                         vocabulary
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import rdflib

from prism_service.config import project_data_dir
from prism_service.project_context import get_project
from prism_service.services import ontology_rules
from prism_service.services.ontology_graph import NS, OntologyGraph

SEAT_ID = "prism-law-promotion-worker"


def _slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return value or "rule"


def _first_sentence(text: str) -> str:
    sentence = (text or "").strip().split(". ")[0].strip()
    if sentence and not sentence.endswith("."):
        sentence += "."
    return sentence


def _memory_iri(memory_id: str) -> str:
    return f"{NS}instance/memory/{memory_id}"


# ---------------------------------------------------------------------------
# TTL templates -- one place each drafter fills in, so the shape of a
# promoted rule or term never drifts between drafters.
# ---------------------------------------------------------------------------

_RULE_TTL_TEMPLATE = """@prefix o: <urn:prism:onto:> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

o:{name}.target a sh:NodeShape ;
    sh:targetClass {target_class} ;
    sh:sparql o:{name} .

o:{name} a sh:SPARQLConstraint ;
    rdfs:comment "{description}" ;
    sh:name "{title}" ;
    sh:description "{description}" ;
    sh:message "{message}" ;
    o:derivedFrom <{derived_from}> ;
    sh:select \"\"\"
{select}
    \"\"\" .
"""

_TERM_TTL_TEMPLATE = """@prefix o: <urn:prism:onto:> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

<urn:prism:onto:term/{name}> a o:Term ;
    rdfs:label "{label}" ;
    rdfs:comment "{definition}" ;
{alt_labels}    o:derivedFrom <{derived_from}> .
"""


def _module_snippet(from_label: str, to_label: str) -> str:
    """A tiny ABox fixture: two o:Module instances, one importing the
    other, each carrying only rdfs:label -- the one property
    ontology_graph._emit_code_graph actually puts on a real Module
    instance today. o:imports itself is not yet emitted by real code
    (no production Module carries it), so this fixture is the honest way
    to demonstrate the drafted rule without waiting on that gap to close."""
    from_id = _slug(from_label)
    to_id = _slug(to_label)
    return (
        "@prefix o: <urn:prism:onto:> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        f'o:mod-{from_id} a o:Module ; rdfs:label "{from_label}" ; '
        f"o:imports o:mod-{to_id} .\n"
        f'o:mod-{to_id} a o:Module ; rdfs:label "{to_label}" .\n'
    )


# ---------------------------------------------------------------------------
# (a) A principle memory -> a rule over o:imports between o:Module nodes.
# ---------------------------------------------------------------------------

def _draft_principle_rule(memory, principle: dict) -> dict:
    src = str(principle.get("from") or "").strip()
    forbidden_raw = principle.get("must_not_depend_on")
    if isinstance(forbidden_raw, str):
        forbidden_raw = [forbidden_raw]
    forbidden = str((forbidden_raw or [""])[0]).strip()
    name = f"{_slug(src)}-must-not-depend-on-{_slug(forbidden)}"
    why = str(principle.get("why") or "").strip()
    title = f"{src} must not depend on {forbidden}"
    description = why or f"A module under {src} must not import a module under {forbidden}."
    message = f"a module under {src} imports a module under {forbidden}"
    select = (
        "        PREFIX o: <urn:prism:onto:>\n"
        "        SELECT $this WHERE {\n"
        "            $this a o:Module ; o:imports ?m .\n"
        "            $this rdfs:label ?fromPath .\n"
        "            ?m rdfs:label ?toPath .\n"
        f'            FILTER(STRSTARTS(?fromPath, "{src}") '
        f'&& STRSTARTS(?toPath, "{forbidden}"))\n'
        "        }"
    )
    ttl = _RULE_TTL_TEMPLATE.format(
        name=name, target_class="o:Module", title=title,
        description=description, message=message, select=select,
        derived_from=_memory_iri(memory.id),
    )
    fixtures = {
        "compliant": _module_snippet(f"{src}/ok.py", f"{src}/sibling.py"),
        "violating": _module_snippet(f"{src}/bad.py", f"{forbidden}/thing.py"),
    }
    return {"kind": "rule", "name": name, "ttl": ttl, "fixtures": fixtures,
            "derived_from": memory.id}


# ---------------------------------------------------------------------------
# (b) A convention/pattern memory -> a rule SKELETON, for a person or a
# later drafting agent to finish. Its sh:select body is a deliberate
# placeholder -- it never fires, so install() refuses to install it until
# the TODO is replaced with a real check.
# ---------------------------------------------------------------------------

def _draft_convention_skeleton(memory) -> dict:
    name = _slug(memory.name) or f"memory-{memory.id}"
    title = (memory.name or memory.id).replace("-", " ").replace("_", " ").strip().title()
    description = _first_sentence(memory.description) or (
        "A rule drafted from a memory. It has no real check yet.")
    message = f"check the rule from memory {memory.id}: {description}"
    select = (
        f"        # TODO from memory {memory.id}\n"
        "        PREFIX o: <urn:prism:onto:>\n"
        "        SELECT $this WHERE {\n"
        "            FILTER(false)\n"
        "        }"
    )
    ttl = _RULE_TTL_TEMPLATE.format(
        name=name, target_class="o:Task", title=title,
        description=description, message=message, select=select,
        derived_from=_memory_iri(memory.id),
    )
    return {"kind": "rule", "name": name, "ttl": ttl,
            "fixtures": {"compliant": "", "violating": ""},
            "derived_from": memory.id, "needs_completion": True}


# ---------------------------------------------------------------------------
# (c) A memory naming one term -> an o:Term with altLabels.
# ---------------------------------------------------------------------------

_TERM_PREFIX_RE = re.compile(r"\bterm:\s*([A-Za-z][A-Za-z0-9_-]*)", re.IGNORECASE)
_BACKTICK_WORD_RE = re.compile(r"`([A-Za-z][A-Za-z0-9_-]*)`")


def _extract_term_name(memory) -> str:
    text = f"{memory.name}\n{memory.description}"
    m = _TERM_PREFIX_RE.search(text)
    if m:
        return m.group(1)
    words = _BACKTICK_WORD_RE.findall(text)
    if len(words) == 1:
        return words[0]
    return ""


def _draft_term(memory, term_name: str) -> dict:
    label = term_name[:1].upper() + term_name[1:]
    name = _slug(term_name)
    definition = _first_sentence(memory.description) or _first_sentence(memory.name) or (
        f"A term drafted from memory {memory.id}.")
    alt = sorted({term_name.lower()} - {label})
    alt_lines = "".join(f'    o:altLabel "{a}" ;\n' for a in alt)
    ttl = _TERM_TTL_TEMPLATE.format(
        name=name, label=label, definition=definition, alt_labels=alt_lines,
        derived_from=_memory_iri(memory.id),
    )
    return {"kind": "term", "name": name, "ttl": ttl,
            "fixtures": {"compliant": "", "violating": ""},
            "derived_from": memory.id}


def draft(memory, project: str) -> dict:
    """Draft a rule or a term from one memory. Never writes to disk."""
    principle = None
    evidence = getattr(memory, "evidence", None) or {}
    if isinstance(evidence, dict):
        principle = evidence.get("principle")
    if isinstance(principle, dict) and principle.get("from") and principle.get("must_not_depend_on"):
        return _draft_principle_rule(memory, principle)
    term_name = _extract_term_name(memory)
    if term_name:
        return _draft_term(memory, term_name)
    return _draft_convention_skeleton(memory)


# ---------------------------------------------------------------------------
# The run: create ONE visible task, drive it to the review gate.
# ---------------------------------------------------------------------------

def _plan_doc(drafted: dict, memory) -> str:
    lines = [
        f"# Promote {memory.id} to law",
        "",
        "## Memory",
        "",
        (memory.description or memory.name or "").strip(),
        "",
        f"## Draft ({drafted['kind']}: {drafted['name']})",
        "",
        "```turtle",
        drafted["ttl"].strip(),
        "```",
        "",
    ]
    fixtures = drafted.get("fixtures") or {}
    if fixtures.get("compliant"):
        lines += ["## Compliant fixture", "", "```turtle",
                  fixtures["compliant"].strip(), "```", ""]
    if fixtures.get("violating"):
        lines += ["## Violating fixture", "", "```turtle",
                  fixtures["violating"].strip(), "```", ""]
    return "\n".join(lines)


def start_promotion(project: str, memory_id: str) -> dict:
    """Draft `memory_id` and drive a new run task to the review gate.
    Returns {"ok": True, "run_task_id", "draft"} once the gate is parked,
    or {"ok": False, "reason"} if the memory is unknown or the flow could
    not start. Never raises."""
    ctx = get_project(project)
    memory = ctx.memory_svc.get_entry(memory_id)
    if memory is None:
        return {"ok": False, "reason": f"unknown memory: {memory_id}"}
    drafted = draft(memory, project)

    task_svc = ctx.task_svc
    # The memory id rides a TAG, not completion_proof: every free-text
    # field (title/description/completion_proof/plan_doc/...) runs
    # through TaskService._apply_ste on every write, which rewrites style
    # AND aligns lexicon synonyms in plain prose -- exactly what a
    # drafted SPARQL/TTL body must never have happen to it. install()
    # re-derives the draft fresh from the memory (a pure function of
    # memory + project) instead of round-tripping it through a
    # STE-normalised field.
    run_task = task_svc.create(
        title=f"Promote {memory.name or memory.id} to law",
        description=(
            f"Turn memory {memory.id} into a {drafted['kind']}. The "
            "ontology will hold it once you approve the draft."
        ),
        channel="daemon", tags=["promote-to-law", "daemon", f"memory:{memory.id}"],
        parent_id="", workflow="promote_to_law",
        # review is this workflow's ONE owner stop -- a pure human
        # sign-off on the drafted rule, never a machine-runnable check.
        # An unset proof_type falls through conductor_service.py's
        # demo-shaped evidence path (it wants a trusted-runner oracle
        # receipt), which a promote_to_law task can never produce, so
        # every run parked at "review" showed BLOCKED - evidence not on
        # file no matter how good the draft was. proof_type="review"
        # routes it to the human-judgment path this gate actually is.
        proof_type="review",
    )
    task_svc.update(run_task.id, plan_doc=_plan_doc(drafted, memory))

    from prism_service.api import conductor_flow as flow

    started = flow.flow_start(
        flow.Ident(task_id=run_task.id, session_id=SEAT_ID), project=project)
    if not started.get("ok"):
        return {"ok": False, "run_task_id": run_task.id,
                "reason": started.get("error") or "flow_start refused"}

    res = flow.flow_report(flow.Ident(
        task_id=run_task.id, session_id=SEAT_ID, outcome="pass",
        expected_step="draft"), project=project)
    if not res.get("ok"):
        return {"ok": False, "run_task_id": run_task.id,
                "reason": res.get("reason") or "could not reach the review gate"}
    return {"ok": True, "run_task_id": run_task.id, "draft": drafted}


# ---------------------------------------------------------------------------
# Install: write the approved draft into the project's own law, and prove
# the violating fixture fires before committing to it.
# ---------------------------------------------------------------------------

_PROMOTED_MODEL_HEADER = (
    "# promoted-model.ttl -- terms promoted from Understand memory, plus\n"
    "# the o:derivedFrom property every promoted rule and term carries.\n"
    "# services/law_promotion.py writes to this file, once an owner\n"
    "# approves the draft at the promote_to_law workflow's review gate.\n"
    "\n"
    "@prefix o: <urn:prism:onto:> .\n"
    "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
    "\n"
    "o:derivedFrom a rdf:Property ;\n"
    '    rdfs:label "derivedFrom" ;\n'
    '    rdfs:comment "The memory entry that produced this rule or term." .\n'
)

_PROMOTED_SHAPES_HEADER = (
    "# promoted-shapes.ttl -- rules promoted from Understand memory.\n"
    "# services/law_promotion.py writes each rule here, once an owner\n"
    "# approves the draft at the promote_to_law workflow's review gate.\n"
    "\n"
    "@prefix o: <urn:prism:onto:> .\n"
    "@prefix sh: <http://www.w3.org/ns/shacl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
)


def _ensure_promoted_model_header(project: str) -> Path:
    path = project_data_dir(project) / "ontology" / "promoted-model.ttl"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(_PROMOTED_MODEL_HEADER, encoding="utf-8")
    return path


def _install_rule(drafted: dict, project: str) -> dict:
    name = drafted["name"]
    ttl = drafted["ttl"]
    _ensure_promoted_model_header(project)  # o:derivedFrom's own declaration
    path = project_data_dir(project) / "ontology" / "promoted-shapes.ttl"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else _PROMOTED_SHAPES_HEADER
    marker = f"# name: {name}\n"
    if marker in existing:
        return {"ok": True, "installed": False, "kind": "rule", "name": name,
                "reason": "already installed"}

    fixtures = drafted.get("fixtures") or {}
    violating_snippet = str(fixtures.get("violating") or "")
    if not violating_snippet.strip() or drafted.get("needs_completion"):
        return {"ok": False, "reason": (
            f"refused install: {name!r} has no demonstrable violating "
            "fixture yet -- finish its sh:select body before it can "
            "become law")}

    candidate = existing.rstrip() + "\n\n" + marker + ttl.strip() + "\n"
    path.write_text(candidate, encoding="utf-8")

    try:
        tbox = OntologyGraph(project).to_rdflib()
    except Exception as exc:
        path.write_text(existing, encoding="utf-8")
        return {"ok": False, "reason": f"could not build the project graph: {exc}"}

    violating_graph = rdflib.Graph()
    violating_graph += tbox
    try:
        violating_graph.parse(data=violating_snippet, format="turtle", publicID=NS)
    except Exception as exc:
        path.write_text(existing, encoding="utf-8")
        return {"ok": False, "reason": f"violating fixture did not parse: {exc}"}
    _inferred, violations = ontology_rules.run_shapes(violating_graph, project)
    if name not in violations:
        path.write_text(existing, encoding="utf-8")
        return {"ok": False, "reason": (
            f"refused install: the rule {name!r} did not fire on its own "
            "violating fixture -- a rule that cannot fail is decoration")}

    compliant_snippet = str(fixtures.get("compliant") or "")
    if compliant_snippet.strip():
        compliant_graph = rdflib.Graph()
        compliant_graph += tbox
        compliant_graph.parse(data=compliant_snippet, format="turtle", publicID=NS)
        _inferred2, quiet = ontology_rules.run_shapes(compliant_graph, project)
        if name in quiet:
            path.write_text(existing, encoding="utf-8")
            return {"ok": False, "reason": (
                f"refused install: the rule {name!r} fired on its own "
                "compliant fixture")}

    ontology_rules.validate(project)
    return {"ok": True, "installed": True, "kind": "rule", "name": name}


def _install_term(drafted: dict, project: str) -> dict:
    name = drafted["name"]
    ttl = drafted["ttl"]
    path = _ensure_promoted_model_header(project)
    existing = path.read_text(encoding="utf-8")
    marker = f"# name: {name}\n"
    if marker in existing:
        return {"ok": True, "installed": False, "kind": "term", "name": name,
                "reason": "already installed"}

    candidate = existing.rstrip() + "\n\n" + marker + ttl.strip() + "\n"
    path.write_text(candidate, encoding="utf-8")
    try:
        rdflib.Graph().parse(data=candidate, format="turtle", publicID=NS)
    except Exception as exc:
        path.write_text(existing, encoding="utf-8")
        return {"ok": False, "reason": f"draft did not parse as turtle: {exc}"}
    try:
        OntologyGraph(project).load_model()
    except Exception as exc:
        path.write_text(existing, encoding="utf-8")
        return {"ok": False, "reason": f"could not load the project model: {exc}"}
    return {"ok": True, "installed": True, "kind": "term", "name": name}


def install(drafted: dict, project: str) -> dict:
    """Write `drafted` into the project's own law. Refuses (never
    partially writes) when a rule's violating fixture does not fire, or
    when a file will not parse. Idempotent by name."""
    kind = drafted.get("kind")
    if kind == "rule":
        return _install_rule(drafted, project)
    if kind == "term":
        return _install_term(drafted, project)
    return {"ok": False, "reason": f"unknown draft kind: {kind!r}"}


def _memory_id_from_task(task) -> str:
    for tag in getattr(task, "tags", None) or []:
        if str(tag).startswith("memory:"):
            return str(tag)[len("memory:"):]
    return ""


def _install_one(project: str, task) -> dict:
    if task.workflow != "promote_to_law" or task.workflow_step != "install":
        return {"ok": False, "task_id": task.id,
                "reason": "task is not awaiting install"}
    memory_id = _memory_id_from_task(task)
    if not memory_id:
        return {"ok": False, "task_id": task.id,
                "reason": "task carries no memory: tag -- cannot re-derive its draft"}
    ctx = get_project(project)
    memory = ctx.memory_svc.get_entry(memory_id)
    if memory is None:
        return {"ok": False, "task_id": task.id,
                "reason": f"unknown memory: {memory_id}"}
    # Re-derived fresh from the memory (a pure function of memory +
    # project) -- never round-tripped through a STE-normalised task
    # field. See start_promotion's own comment on this.
    drafted = draft(memory, project)

    task_svc = ctx.task_svc
    result = install(drafted, project)
    if not result.get("ok"):
        reason = result.get("reason", "install failed")
        task_svc.update(task.id, gate_reason=reason)
        task_svc.record_history(
            task.id, action="law_promotion_install_failed",
            details=reason, actor=SEAT_ID)
        return {"ok": False, "task_id": task.id, "reason": reason}

    task_svc.update(task.id, completion_proof=json.dumps(result, indent=2))
    from prism_service.api import conductor_flow as flow

    res = flow.flow_report(flow.Ident(
        task_id=task.id, session_id=SEAT_ID, outcome="pass",
        expected_step="install"), project=project)
    if res.get("ok"):
        # "done" (models/workflow.py PROMOTE_TO_LAW_STEPS) is not a gate --
        # nothing else closes the task once the flow reaches it, the same
        # rule language_alignment_worker.run_once_for documents for its own
        # terminal step.
        task_svc.update(task.id, status="done", full_outcome_complete=True)
        task_svc.record_history(
            task.id, action="law_promotion_worker_done",
            details=f"kind={result.get('kind')}; name={result.get('name')}",
            actor=SEAT_ID)
    return {"ok": bool(res.get("ok")), "task_id": task.id, "install": result}


def install_pending(project: str, task_id: str = "") -> dict:
    """The small worker tick that runs after a review gate approves: finds
    every promote_to_law task parked at the install step (or just
    `task_id`, when given) and installs it. Callable directly by a test or
    an API trigger right after the owner's approve -- the same shape
    services/ship_worker.py reacts to a green_gate approval with, kept
    here (rather than a new daemon module) since this task's own
    allowed_files carry no separate worker file."""
    task_svc = get_project(project).task_svc
    if task_id:
        task = task_svc.get(task_id)
        if task is None:
            return {"ok": False, "reason": f"unknown task: {task_id}"}
        return _install_one(project, task)

    candidates = [
        t for t in task_svc.list()
        if t.workflow == "promote_to_law" and t.workflow_step == "install"
        and t.status not in ("done", "cancelled", "deleted")
    ]
    return {"installed": [_install_one(project, t) for t in candidates]}
