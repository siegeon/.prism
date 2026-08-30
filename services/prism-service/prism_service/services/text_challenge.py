"""Challenge, correct and report the text a conductor node writes into a
task artifact (task d7947eb6).

Owner: "we must make sure all of the text generation nodes that hit
artifacts use the ontology rules, they should have separate nodes to
challenge, correct or provide access to prevent this from happening
again."

WHAT WAS OPEN. Four nodes generate free text --
``review_previous_notes`` -> ``premise_notes``, ``draft_story`` and
``verify_plan`` -> ``plan_doc``, ``implement_tasks`` and
``verify_green_state`` -> ``completion_proof``. ``services/ste.py``
normalises on write and returns a style block, but the block is
advisory and the write proceeds either way, and
``TaskService._align_plan_doc`` holds every heading, table row and
BULLET byte-identical because those line shapes are rubric-critical.
A story is almost entirely bullets, so a generated acceptance criterion
kept its semicolon and its synonym, and the two live ontology rule
counts climbed with every drive.

THE SHAPE, copied from ``review_previous_notes`` (task cd33263f):
premise-gather (codified) -> premise-judge (agentic) ->
premise-citation-check (codified). This module is the codified half for
every text node: a CHALLENGE that judges, a CORRECT that repairs only
what a machine can repair safely, and a report that NAMES what neither
can fix so a person or an agent can act on it.

CODIFIED BY CONSTRUCTION. Never a model call. The judgement is
``ontology_rules.run_shapes`` over a one-node probe graph, so the verdict
is the SAME SPARQL the Rules tab runs -- there is no second checker here
to drift from ``ontology/shapes.ttl``, and which rules count as TEXT
rules is read off the shapes file itself (a rule whose own select reads
``rdfs:label`` or ``rdfs:comment``), never a hand-kept list. The repair
is ``ste.normalize`` and nothing else.

NO BACKGROUND SWEEPER (owner, 2026-08-30): "if its working right we
don't need a background constant here". The check runs IN THE PIPELINE,
at the moment an artifact is built -- as a behaviour step after each
text node, and inside ``conductor_flow.flow_report`` before the advance.
This module starts no thread, registers no worker, and does not depend
on ``services/language_alignment_worker.py`` running.

WHAT IT REFUSES TO TOUCH:

* A lexicon synonym. ``services/lexicon.py``'s ``align`` substitutes
  ontology CLASS NAMES into sentences (PR becomes PullRequest, ticket
  becomes Task, doc becomes Document), so "the PR is merged" becomes
  "the PullRequest is merged" -- the same corruption that once produced
  "gate card" -> "gate Task". This node REPORTS the canonical-term
  violation and never substitutes. Note that
  ``TaskService._align_plan_doc`` and ``_apply_ste`` still call
  ``lexicon.align`` on a prose run at write time. That is existing
  behaviour outside this module and it is where the substitution really
  happens today.

* ``oracle``, ``stop_if`` and ``likely_misfire`` are challenged and never
  rewritten. They are claims a machine or a person must follow exactly.
* Anything from an ``oracle:`` or ``citation:`` marker rightwards on a
  line is copied through byte-identical, for the same reason -- an
  acceptance criterion carries its oracle inline.
* A heading (``arc_governance._sections`` keys a rubric section by its
  exact heading text) and a table row.
* Every hedge word. A rewrite that turns "may have failed" into "failed"
  is a different claim, so a repair that changes any hedge count is
  REFUSED and the original text stands.
* Every span ``ste._protected_spans`` protects (fenced and inline code,
  quoted text, URLs, ids, file paths). Note that shapes.ttl strips a
  NARROWER set before it looks, so a semicolon inside a single-quoted
  span is flagged and correctly not repaired -- that case is reported,
  not hidden.
"""

from __future__ import annotations

import functools
import re
from pathlib import Path

import rdflib

from prism_service.services import ontology_rules, ste

_ONTOLOGY_DIR = Path(__file__).resolve().parent.parent / "ontology"
_SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
_PROBE = rdflib.URIRef("urn:prism:onto:instance/text-challenge/probe")

# The class the probe node carries. o:Task is the real projection class
# for task free text (services/ontology_graph.py _emit_tasks sets
# rdfs:label from the title and rdfs:comment from the description), so
# every rule that judges a task's text applies to the probe unchanged.
_PROBE_CLASS = "Task"

# Which artifact fields each text-generating step writes. Mirrors the
# routing in mcp/tools.py's conductor_work and
# services/task_runner.py's _route_proof -- the challenge reads exactly
# what the node just produced.
ARTIFACT_FIELDS: dict[str, tuple[str, ...]] = {
    "review_previous_notes": ("premise_notes", "completion_proof"),
    "draft_story": ("plan_doc", "completion_proof"),
    "verify_plan": ("plan_doc", "completion_proof"),
    "implement_tasks": ("completion_proof",),
    "verify_green_state": ("completion_proof",),
}

# Instructions, never prose. Challenged, never repaired.
NEVER_REPAIR: tuple[str, ...] = ("oracle", "stop_if", "likely_misfire")

# The same per-field mode services/language_alignment.py uses.
_MODE_FOR_FIELD = {
    "title": "flavored",
    "description": "flavored",
    "completion_proof": "flavored",
    "premise_notes": "flavored",
    "plan_doc": "flavored",
}

_HEADING_RE = re.compile(r"^\s*#")
_TABLE_RE = re.compile(r"^\s*\|")
# A line's claim marker. Everything from here rightwards is held
# byte-identical: an acceptance criterion carries its oracle inline, and
# a premise bullet carries its citation inline.
_CLAIM_MARKER_RE = re.compile(r"(?i)\b(oracle|citation|stop_if|verify)\s*:")


# ----------------------------------------------------------------------
# The rules -- read off shapes.ttl, never re-declared here
# ----------------------------------------------------------------------


@functools.lru_cache(maxsize=8)
def text_rule_names(project: str | None = None) -> tuple[str, ...]:
    """The ontology rules that judge free TEXT, derived from the shapes
    file itself: a SPARQL constraint whose own select reads rdfs:label or
    rdfs:comment. A structural rule that walks a property (a task's
    channel, a blocked task's children) is not a text rule and is left
    to the Rules tab, where it belongs.

    Narrowed a second time to the rules that TARGET the probe's own
    class, so a text rule about some other class (a skill description, a
    dated folder) is not reported as a rule this node checked. Both
    filters read the shapes file; neither is a list kept in Python.

    Derived, never hand-kept: a new text rule added to shapes.ttl is
    enforced by this node for free, and a renamed one cannot leave a
    stale copy behind in Python."""
    graph = ontology_rules._shapes_graph(project)
    label_iri = str(rdflib.RDFS.label)
    comment_iri = str(rdflib.RDFS.comment)
    names: set[str] = set()
    for rule, select in graph.subject_objects(_SH.select):
        body = str(select)
        if ("rdfs:label" in body or "rdfs:comment" in body
                or label_iri in body or comment_iri in body):
            name = ontology_rules._local_name(str(rule))
            if name.endswith(".target"):
                name = name[: -len(".target")]
            names.add(name)
    probe_class = ontology_rules.NS + _PROBE_CLASS
    targeting = {r["name"] for r in _rule_meta(project).values()
                 if probe_class in (r.get("target_classes")
                                    or [r.get("target_class")])}
    # An empty catalog means the catalog query failed, not that no rule
    # targets the probe class -- report the wider set rather than none.
    return tuple(sorted(names & targeting if targeting else names))


@functools.lru_cache(maxsize=8)
def _rule_meta(project: str | None = None) -> dict:
    """{rule name: catalog row} -- the rule's own title and message, so a
    report quotes what the Rules tab shows rather than inventing wording."""
    try:
        return {r["name"]: r for r in ontology_rules.rule_catalog(project)}
    except Exception:
        return {}


@functools.lru_cache(maxsize=1)
def _tbox_nt() -> str:
    """model.ttl and every ontology/model-*.ttl extension, serialised
    once per process. model-lexicon.ttl is what carries the o:Term /
    o:altLabel rows the canonical-term rule reads, so the probe graph is
    useless without it."""
    graph = rdflib.Graph()
    for path in sorted(_ONTOLOGY_DIR.glob("model*.ttl")):
        graph.parse(str(path), format="turtle")
    return graph.serialize(format="nt")


def _probe_graph(text: str) -> rdflib.Graph:
    """The TBox plus ONE node carrying `text` on both rdfs:label and
    rdfs:comment. Both, because a rule may read either one and this node
    must never miss a rule for the sake of which property it picked."""
    graph = rdflib.Graph()
    graph.parse(data=_tbox_nt(), format="nt")
    graph.add((_PROBE, rdflib.RDF.type,
               rdflib.URIRef(ontology_rules.NS + _PROBE_CLASS)))
    graph.add((_PROBE, rdflib.RDFS.label, rdflib.Literal(text)))
    graph.add((_PROBE, rdflib.RDFS.comment, rdflib.Literal(text)))
    return graph


def challenge(text: str, project: str | None = None) -> dict:
    """Run the live ontology text rules over `text` and report which
    fired. No model call, no store I/O, no git.

    Returns ``{"ok", "checked", "violations": [{"name", "title",
    "message"}], "rules", "reason"}``. ``checked`` is False when the
    rules could not run at all -- an honest "I did not look", never a
    silent pass dressed as a clean verdict."""
    body = str(text or "")
    rules = list(text_rule_names(project))
    if not body.strip():
        return {"ok": True, "checked": True, "violations": [],
                "rules": rules, "reason": "no text to check"}
    try:
        _inferred, fired = ontology_rules.run_shapes(_probe_graph(body), project)
    except Exception as exc:  # pragma: no cover - environment failure
        return {"ok": True, "checked": False, "violations": [],
                "rules": rules,
                "reason": f"the ontology rules did not run: {exc}"}
    meta = _rule_meta(project)
    violations = []
    for name in sorted(set(fired) & set(rules)):
        row = meta.get(name, {})
        violations.append({"name": name,
                           "title": row.get("title", ""),
                           "message": row.get("message", "")})
    reason = ("the text satisfies every ontology text rule" if not violations
              else "; ".join(v["message"] or v["name"] for v in violations))
    return {"ok": not violations, "checked": True, "violations": violations,
            "rules": rules, "reason": reason}


# ----------------------------------------------------------------------
# The repair -- only what a machine can repair safely
# ----------------------------------------------------------------------


def _hedge_counts(text: str) -> dict:
    """Every hedge word occurrence in `text`, counted over the WHOLE
    string. Deliberately not span-aware: a repair must not move a hedge
    into or out of a protected span either."""
    counts: dict[str, int] = {}
    for word in ste._HEDGE_WORDS:
        counts[word] = len(re.findall(rf"\b{re.escape(word)}\b", text,
                                       re.IGNORECASE))
    return counts


def _repair_line(line: str, mode: str, rules: list) -> str:
    """One line of markdown. A heading, a table row and a blank line are
    copied through. Everything from a claim marker rightwards is copied
    through. The rest runs through ``ste.normalize`` and NOTHING ELSE.

    NORMALIZE ONLY, never the lexicon (owner, 2026-08-30): "lexicon.align
    is something that happens as we build and merge artifacts it should
    be language nodes that make sure we are not dealing with machine
    generated slop", and the substitution itself is dangerous in prose --
    ``services/lexicon.py`` puts ontology CLASS NAMES into sentences (PR
    becomes PullRequest, ticket becomes Task, doc becomes Document), so
    "the PR is merged" becomes "the PullRequest is merged". That is the
    same corruption that once produced "gate card" -> "gate Task". A
    synonym is therefore REPORTED by this node, never rewritten by it."""
    if not line.strip():
        return line
    if _HEADING_RE.match(line) or _TABLE_RE.match(line):
        return line
    marker = _CLAIM_MARKER_RE.search(line)
    head, tail = (line[:marker.start()], line[marker.start():]) if marker \
        else (line, "")
    if not head.strip():
        return line

    fixed, line_rules = ste.normalize(head, mode=mode)
    for rule in line_rules:
        if rule not in rules:
            rules.append(rule)
    return fixed + tail


def _repair_markdown(text: str, mode: str) -> tuple[str, list]:
    rules: list = []
    lines = [_repair_line(line, mode, rules) for line in text.split("\n")]
    return "\n".join(lines), rules


def correct(text: str, field: str = "completion_proof",
            project: str | None = None) -> dict:
    """Challenge `text`, repair what is safe to repair, challenge again.

    Returns ``{"text", "changed", "repairable", "rules_applied",
    "hedges_kept", "refused", "before", "after"}``. ``text`` is what
    should be stored: the repaired text when the repair held every
    hedge, the ORIGINAL otherwise. ``after`` names every violation the
    normaliser did not clear, including every synonym -- this node
    reports a synonym and never substitutes one."""
    body = str(text or "")
    before = challenge(body, project)
    repairable = field not in NEVER_REPAIR
    base = {"text": body, "changed": False, "repairable": repairable,
            "rules_applied": [], "hedges_kept": True,
            "refused": "", "before": before, "after": before}

    if not repairable:
        base["refused"] = (
            f"{field} is an instruction. A rewrite would change what it "
            "claims, so this node reports it and never repairs it.")
        return base
    if not body.strip():
        return base

    mode = _MODE_FOR_FIELD.get(field, "flavored")
    fixed, rules = _repair_markdown(body, mode)
    if _hedge_counts(body) != _hedge_counts(fixed):
        base["hedges_kept"] = False
        base["refused"] = (
            "the repair would drop a hedge word. A hedge carries the "
            "claim, so the original text stands.")
        return base
    if fixed == body:
        return base

    after = challenge(fixed, project)
    return {"text": fixed, "changed": True, "repairable": True,
            "rules_applied": rules, "hedges_kept": True,
            "refused": "", "before": before, "after": after}


# ----------------------------------------------------------------------
# The node itself
# ----------------------------------------------------------------------


def challenge_step_artifacts(task_svc, task_id: str, step_id: str,
                             project: str | None = None) -> dict:
    """CODIFIED CHALLENGE NODE. Read the artifact fields `step_id` just
    wrote, judge each with the live ontology rules, repair what is safe,
    and write the repair back THROUGH ``TaskService.update`` so the
    normal STE pipeline and the normal history row still happen -- this
    module never writes a column itself.

    Returns a report naming every field it looked at, what it repaired,
    and every violation it could not repair."""
    fields = ARTIFACT_FIELDS.get(step_id, ())
    report: dict = {"step": step_id, "task_id": task_id, "fields": {},
                    "repaired": [], "unrepaired": [], "reason": ""}
    if not fields:
        report["reason"] = f"{step_id} writes no text artifact"
        return report
    task = task_svc.get(task_id)
    if task is None:
        report["reason"] = f"no such task: {task_id}"
        return report

    for field in fields:
        value = str(getattr(task, field, "") or "")
        if not value.strip():
            continue
        result = correct(value, field=field, project=project)
        report["fields"][field] = {
            "changed": result["changed"],
            "rules_applied": result["rules_applied"],
            "hedges_kept": result["hedges_kept"],
            "refused": result["refused"],
            "before": result["before"],
            "after": result["after"],
        }
        if result["changed"]:
            task_svc.update(task_id, **{field: result["text"]})
            report["repaired"].append(field)
        for violation in result["after"]["violations"]:
            report["unrepaired"].append(
                {"field": field, "name": violation["name"],
                 "message": violation["message"]})

    if report["unrepaired"]:
        report["reason"] = (
            f"{len(report['unrepaired'])} violation(s) a machine must not "
            "repair. Read them and fix the text by hand.")
    elif report["repaired"]:
        report["reason"] = "repaired: " + ", ".join(report["repaired"])
    else:
        report["reason"] = "every artifact already satisfies the text rules"
    return report
