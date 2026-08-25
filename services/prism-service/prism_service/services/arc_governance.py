"""Architecture governance — rubrics, principles, and conformance (task 8579d49e).

Ports arc-kit's (MIT, https://github.com/arc-kit) governance core into
PRISM's conductor seams:

  * Rubrics are YAML DATA (governance_rubrics.yaml beside this module),
    not code. ``score_story_complete`` / ``score_plan_coverage`` are PURE
    functions of (evidence, rubric[, principles]) so the conductor's
    story/plan gates verify mechanically — the forced-override path for
    these validation kinds is retired (conductor_service._VERIFIER_RULES).
  * Architecture principles (machine-checkable layer rules, e.g. domain
    must-not-depend-on infrastructure) are stored as MEMORY DATA via
    ``seed_prism_principles`` / ``load_principles`` — never static docs.
  * ``compute_violations`` diffs INTENDED principles against the
    architecture_analyzer's OBSERVED layers.json edges (pure function);
    the result lands beside the other understand artifacts as
    violations.json (understand_artifact_store, 'violations_analyzer').

Misfire guard: an EMPTY principles set never scores green — the plan
gate says principles are missing instead of silently passing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

# ----------------------------------------------------------------------
# Rubrics — YAML data, loaded once
# ----------------------------------------------------------------------

RUBRICS_PATH = Path(__file__).parent / "governance_rubrics.yaml"

_rubrics_cache: Optional[dict] = None


def load_rubrics(path: Optional[Path] = None) -> dict:
    """Load the gate rubrics from YAML data. Cached after first read
    (the file ships with the package and does not change at runtime)."""
    global _rubrics_cache
    if path is None and _rubrics_cache is not None:
        return _rubrics_cache
    import yaml

    p = path or RUBRICS_PATH
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if path is None:
        _rubrics_cache = data
    return data


# ----------------------------------------------------------------------
# Pure scoring helpers
# ----------------------------------------------------------------------

def _sections(markdown: str) -> dict[str, str]:
    """Split a markdown doc into {heading-text-lower: body} chunks."""
    out: dict[str, str] = {}
    current = ""
    for line in (markdown or "").splitlines():
        m = re.match(r"^#{1,6}\s+(.*)$", line.strip())
        if m:
            current = m.group(1).strip().lower()
            out.setdefault(current, "")
            continue
        if current:
            out[current] += line + "\n"
    return out


def _find_section(sections: dict[str, str], wanted: str) -> Optional[str]:
    wanted = wanted.strip().lower()
    for name, body in sections.items():
        if wanted in name:
            return body
    return None


def _ac_lines(section_body: str) -> list[tuple[str, str]]:
    """Return [(ac_id, full_line)] for every entry carrying an AC id.

    An entry starts on a markdown bullet ('- AC-1: ...' / '* AC-1: ...') OR on a
    PLAIN line that begins with the AC id ('AC-1: ...') — owner 2026-07-19: accept
    either so a naive author is not silently rejected for omitting the bullet
    (the job instructions never said the AC must be a bullet). Continuation
    (wrapped/indented) lines fold into the current entry, so an 'Oracle:' line
    under its AC counts toward that AC."""
    entries: list[str] = []
    for raw in (section_body or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        is_bullet = re.match(r"^\s*[-*]\s+", line) is not None
        starts_ac = re.match(r"^AC-\d+\b", stripped) is not None
        if is_bullet or starts_ac:
            entries.append(stripped)
        elif entries:
            entries[-1] += " " + stripped
    out: list[tuple[str, str]] = []
    for e in entries:
        m = re.search(r"\b(AC-\d+)\b", e)
        if m:
            out.append((m.group(1), e))
    return out


def score_story_complete(evidence: dict, rubric: dict) -> dict:
    """PURE rubric verdict for the story gate.

    evidence: {"story_md": <markdown>}; rubric: the story_complete entry
    from governance_rubrics.yaml. Returns {"ok": bool, "reason": str}.
    A compliant story has every rubric-required section AND every AC
    carries an id + an oracle marker (the observable check for that AC).
    """
    story = str(evidence.get("story_md") or "")
    if not story.strip():
        return {"ok": False, "reason": "story_complete: story_md is empty"}
    sections = _sections(story)
    missing = [s for s in rubric.get("required_sections", [])
               if _find_section(sections, s) is None]
    if missing:
        return {"ok": False,
                "reason": ("story_complete: missing required section(s): "
                           + ", ".join(missing))}
    ac_section = _find_section(sections, rubric.get(
        "ac_section", "acceptance criteria")) or ""
    acs = _ac_lines(ac_section)
    if not acs:
        return {"ok": False,
                "reason": ("story_complete: no acceptance criteria with "
                           "AC-<n> ids found")}
    marker = rubric.get("oracle_marker", "oracle:")
    if rubric.get("require_oracle", True):
        missing_oracle = [ac_id for ac_id, line in acs
                          if marker not in line.lower()]
        if missing_oracle:
            return {"ok": False,
                    "reason": ("story_complete: AC(s) without an oracle "
                               f"({marker} <observable check>): "
                               + ", ".join(missing_oracle))}
    return {"ok": True,
            "reason": (f"story_complete: {len(acs)} AC(s) with ids + "
                       "oracles; all required sections present")}


# Mermaid diagram-type keywords accepted as a parsing plan_diagram.
_MERMAID_KEYWORDS = (
    "flowchart", "graph", "sequencediagram", "classdiagram",
    "statediagram", "erdiagram", "journey", "gantt", "c4context",
    "c4container", "c4component", "mindmap", "timeline",
)

_EDGE_RE = re.compile(
    r"([A-Za-z_][\w./-]*)\s*(?:-{1,3}>|-\.+->|={2,}>)\s*([A-Za-z_][\w./-]*)")


def mermaid_parses(source: str) -> bool:
    """Cheap structural check: the first non-empty line must open a known
    mermaid diagram type. (We do not embed a full mermaid parser.)"""
    for line in (source or "").splitlines():
        token = line.strip().split()[0].lower() if line.strip() else ""
        if not token:
            continue
        return any(token.startswith(k) for k in _MERMAID_KEYWORDS)
    return False


def mermaid_edges(source: str) -> list[dict]:
    """Extract directed edges (A --> B) from mermaid source."""
    edges: list[dict] = []
    for m in _EDGE_RE.finditer(source or ""):
        edges.append({"from": m.group(1), "to": m.group(2)})
    return edges


def score_plan_coverage(evidence: dict, rubric: dict,
                        principles: list[dict]) -> dict:
    """PURE rubric verdict for the plan gate.

    Three teeth, all data-driven:
      (i)  AC-id coverage diff plan-vs-story (every story AC id must be
           referenced by the plan_doc);
      (ii) plan_diagram present + parsing as mermaid (e2);
      (iii) plan-vs-principles conformance: the plan_diagram's layer
           edges must not violate a Brain-stored principle (c1 — flag
           BEFORE code). Empty principles NEVER score green (misfire
           guard: unseeded governance is a failure, not a pass).
    """
    problems: list[str] = []
    out: dict[str, Any] = {"ok": False, "missing_ac_ids": [], "violations": []}

    diagram = str(evidence.get("plan_diagram") or "")
    if rubric.get("require_plan_diagram", True):
        if not diagram.strip():
            problems.append("plan_diagram is missing")
        elif not mermaid_parses(diagram):
            problems.append("plan_diagram does not parse as mermaid "
                            "(unknown diagram type)")

    story = str(evidence.get("story_md") or "")
    plan = str(evidence.get("plan_doc") or "")
    story_acs = sorted({m.group(1) for m in
                        re.finditer(r"\b(AC-\d+)\b", story)})
    missing = [ac for ac in story_acs if ac not in plan]
    if missing:
        out["missing_ac_ids"] = missing
        problems.append("plan does not cover AC id(s): " + ", ".join(missing))
    elif not story_acs:
        problems.append("story carries no AC-<n> ids to diff coverage against")

    if not principles:
        problems.append("no architecture principles seeded — conformance "
                        "cannot be scored (empty principles never pass)")
    elif diagram.strip():
        verdict = compute_violations(
            principles, {"edges": mermaid_edges(diagram)})
        if verdict["count"]:
            out["violations"] = verdict["violations"]
            cited = "; ".join(
                f"{v['from']} -> {v['to']} violates {v['principle']}"
                for v in verdict["violations"])
            problems.append("plan_diagram violates principle(s): " + cited)

    if problems:
        out["reason"] = "plan_coverage: " + "; ".join(problems)
        return out
    out["ok"] = True
    out["reason"] = (f"plan_coverage: {len(story_acs)} AC id(s) covered; "
                     "plan_diagram parses; no principle violations against "
                     f"{len(principles)} seeded principle(s)")
    return out


# ----------------------------------------------------------------------
# premise_grounded (task 3a63190b / github issue #222) — pure scorer
# ----------------------------------------------------------------------
# The conductor gates whether a story is WELL-FORMED but nothing gated
# whether it is TRUE. review_previous_notes was the only WORKFLOW_STEPS
# entry with validation=None. This rubric requires every load-bearing claim
# recorded there to carry EITHER a citation OR an explicit REFUTED/
# UNVERIFIED marker — never neither.

def _claim_lines(section_body: str) -> list[str]:
    """Return each load-bearing claim bullet from a markdown section body,
    folding wrapped/indented continuation lines into their claim (mirrors
    _ac_lines' bullet-folding, generalized to any bulleted claim line — a
    premise claim carries no required id, unlike an AC). Recognises '-'/'*'
    bullets AND numbered lists ('1.' / '1)') identically (task 43cefc52) —
    grounding (_claim_is_grounded) is untouched by bullet form."""
    entries: list[str] = []
    for raw in (section_body or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if re.match(r"^\s*(?:[-*]|\d+[.)])\s+", line):
            entries.append(stripped)
        elif entries:
            entries[-1] += " " + stripped
    return entries


# A file:line citation REQUIRES the line number — a bare path with no digit
# after the colon ('src/foo.py') is citation theatre, not evidence (the
# task's recorded likely_misfire).
_CLAIM_FILELINE_RE = re.compile(r'[\w./\\-]+\.[A-Za-z0-9]+:\d+')
_CLAIM_RUNID_RE = re.compile(
    r'\b(?:PR|issue|run|commit|sha)\s*#?\s*[0-9a-fA-F]+\b', re.IGNORECASE)
# A bare single token in backticks ('`unit-tests.yml`', '`x`') is inline
# CODE FORMATTING, not command output — accepting it would be the same
# citation-theatre trap as a bare path, generalized. Require whitespace
# inside the backticks (a real output/command snippet has multiple
# tokens: 'pytest -q -> 2 passed').
_CLAIM_OUTPUT_RE = re.compile(r'`[^`\n]*\s[^`\n]*`')
_CLAIM_MARKER_RE = re.compile(r'\b(REFUTED|UNVERIFIED)\b', re.IGNORECASE)


def _claim_is_grounded(line: str) -> bool:
    return bool(_CLAIM_FILELINE_RE.search(line)
                or _CLAIM_RUNID_RE.search(line)
                or _CLAIM_OUTPUT_RE.search(line)
                or _CLAIM_MARKER_RE.search(line))


def score_premise_grounded(evidence: dict, rubric: dict) -> dict:
    """PURE rubric verdict for review_previous_notes.

    evidence: {"notes_md": <review_previous_notes report text>}; rubric:
    the premise_grounded entry from governance_rubrics.yaml. Returns
    {"ok": bool, "reason": str}. Every claim bullet under the rubric's
    claims_section heading must carry a citation (file:line with a real
    line number, a run/PR/commit/issue id, or backtick command output) or
    an explicit REFUTED/UNVERIFIED marker; a refusal names the offending
    claim so the driver can self-diagnose.

    UNCONDITIONAL (task 3928b7ac, issue #222 continued): task 3a63190b
    originally scoped this rubric to engage ONLY once the report opened a
    '## <claims_section>' heading — a report with none "had nothing to
    ground" and passed. That opt-in path is RETIRED here: notes_md is now
    read from task.premise_notes, a field DEDICATED to this step (never the
    shared task.completion_proof several fixtures stage with unrelated
    green-proof content for a later gate), so the original collision this
    opt-in guarded against cannot recur. A missing or empty section is a
    real gap now (the exact population issue #222 named — a driver who
    never considers citing evidence) and is REFUSED, not passed.
    """
    notes = str(evidence.get("notes_md") or "")
    section_name = rubric.get("claims_section", "premises")
    sections = _sections(notes) if notes.strip() else {}
    section_body = _find_section(sections, section_name)
    if section_body is None:
        return {"ok": False,
                "reason": (f"premise_grounded: no '{section_name}' section "
                           "present - record at least one load-bearing "
                           "claim from the ticket (with a file:line "
                           "citation, a run/PR/commit/issue id, backtick "
                           "command output, or an explicit REFUTED/"
                           "UNVERIFIED marker) before reporting "
                           "review_previous_notes")}
    claims = _claim_lines(section_body)
    if not claims:
        if not section_body.strip():
            return {"ok": False,
                     "reason": (f"premise_grounded: '{section_name}' "
                                "section is present but empty - record "
                                "each load-bearing claim from the ticket, "
                                "or drop the empty section")}
        return {"ok": False,
                "reason": (f"premise_grounded: '{section_name}' section "
                           "has content but no recognised claim bullet "
                           "('-', '*', '1.', '1)') - record each "
                           "load-bearing claim as its own bullet or "
                           "numbered list item")}
    ungrounded = [c for c in claims if not _claim_is_grounded(c)]
    if ungrounded:
        shown = "; ".join(c[:100] for c in ungrounded)
        return {"ok": False,
                "reason": ("premise_grounded: claim(s) without a citation "
                           "or REFUTED/UNVERIFIED marker: " + shown)}
    return {"ok": True,
            "reason": (f"premise_grounded: {len(claims)} claim(s) all "
                       "carry a citation or an explicit REFUTED/UNVERIFIED "
                       "marker")}


# ----------------------------------------------------------------------
# Green-gate oracle tooth (task 3eb67fb3) — pure scorer
# ----------------------------------------------------------------------

# Observable-token extractors. An oracle names something OBSERVABLE the
# worker must have exercised: a URL, a dev-surface :8888/:7778 citation, a
# screenshot/artifact path, or a named (snake_case) metric. The green_gate
# tooth requires the completion_proof to cite at least one such token that
# ALSO appears in the task's oracle.
_ORACLE_URL_RE = re.compile(r"https?://[^\s)\"'<>]+")
_ORACLE_PORT_RE = re.compile(r":(?:8888|7778)\b")
_ORACLE_ARTIFACT_RE = re.compile(
    r"[\w./\\-]+\.(?:png|jpe?g|webp|gif|svg)\b", re.IGNORECASE)
_ORACLE_METRIC_RE = re.compile(r"\b\w+_\w+\b")


def _weak_proof_value(value: object) -> bool:
    """A completion proof is weak when absent or a placeholder — it does not
    actually evidence the outcome. Mirrors conductor_service.is_weak_proof so
    this scorer stays a self-contained pure function of its evidence."""
    if value is None:
        return True
    s = str(value).strip().lower()
    if s in ("", "unknown", "tbd", "todo", "none"):
        return True
    return s.startswith("<") and s.endswith(">")


def oracle_observables(oracle: object) -> set[str]:
    """Derive the observable token(s) named in a task's oracle (pure).

    Returns the set of citation tokens (URLs, :8888/:7778 ports,
    screenshot/artifact paths, snake_case metric names) that a compliant
    completion_proof must reference. Empty when the oracle names nothing
    machine-observable (the audited override handles that terminal case).
    """
    low = str(oracle or "").lower()
    toks: set[str] = set()
    for m in _ORACLE_URL_RE.finditer(low):
        toks.add(m.group(0).rstrip(".,;)!?\"'"))
    toks.update(m.group(0) for m in _ORACLE_PORT_RE.finditer(low))
    toks.update(m.group(0) for m in _ORACLE_ARTIFACT_RE.finditer(low))
    if "screenshot" in low:
        toks.add("screenshot")
    toks.update(m.group(0) for m in _ORACLE_METRIC_RE.finditer(low))
    return {t for t in toks if len(t) >= 4}


def score_green_outcome(evidence: dict, rubric: dict) -> dict:
    """PURE rubric verdict for the terminal green_gate oracle tooth.

    evidence: {"oracle", "completion_proof", "likely_misfire"}; rubric: the
    green_outcome entry from governance_rubrics.yaml. Returns
    {"ok": bool, "reason": str}. Promotes conductor_service's ADVISORY
    proof/misfire notes into a BLOCKING verdict: a compliant green close has a
    real completion_proof that (a) cites an observable token named in the
    oracle AND (b) addresses the pre-declared likely_misfire (shares a
    meaningful word). Judges the STORED proof, never a decision reason — so a
    green-looking approve cannot clear an empty/mismatched proof.
    """
    proof_raw = evidence.get("completion_proof")
    oracle = str(evidence.get("oracle") or "")
    misfire = str(evidence.get("likely_misfire") or "").strip()

    if _weak_proof_value(proof_raw):
        return {"ok": False,
                "reason": ("green_outcome: no completion_proof recorded — the "
                           "oracle was never evidenced (empty/weak proof)")}
    proof = str(proof_raw).lower()

    cited_token = ""
    if rubric.get("require_oracle_citation", True):
        tokens = oracle_observables(oracle)
        if tokens:
            cited = sorted(t for t in tokens if t in proof)
            if not cited:
                shown = ", ".join(sorted(tokens)[:4])
                return {"ok": False,
                        "reason": ("green_outcome: completion_proof does not "
                                   f"cite the oracle observable ({shown}) — "
                                   "tests-green is not oracle-met")}
            cited_token = cited[0]

    if rubric.get("require_misfire_addressed", True) and misfire:
        min_len = int(rubric.get("misfire_word_min_len", 5))
        misfire_words = set(re.findall(rf"[a-z]{{{min_len},}}", misfire.lower()))
        if misfire_words and not any(w in proof for w in misfire_words):
            return {"ok": False,
                    "reason": ("green_outcome: completion_proof does not "
                               "address the likely_misfire "
                               f"(\"{misfire[:80]}\")")}

    cite = f"oracle {cited_token}" if cited_token else "oracle"
    return {"ok": True,
            "reason": f"green_outcome: proof cites {cite} and addresses misfire"}


# ----------------------------------------------------------------------
# Intended-vs-observed conformance (d1) — pure function
# ----------------------------------------------------------------------

def compute_violations(principles: list[dict], layers: dict) -> dict:
    """Diff INTENDED principles against OBSERVED layer edges (pure).

    `layers` is the architecture_analyzer's layers.json payload (or any
    dict carrying an `edges` list of {from, to}). Returns
    {"count": N, "violations": [{"from","to","principle"}...]} suitable
    for storage as the per-SHA violations.json artifact.
    """
    edges = (layers or {}).get("edges") or []
    violations: list[dict] = []
    for p in principles or []:
        if p.get("kind") != "layer_rule":
            continue
        src = str(p.get("from") or "").strip().lower()
        forbidden = str(p.get("must_not_depend_on") or "").strip().lower()
        if not src or not forbidden:
            continue
        for e in edges:
            if (str(e.get("from") or "").strip().lower() == src
                    and str(e.get("to") or "").strip().lower() == forbidden):
                violations.append({
                    "from": e.get("from"), "to": e.get("to"),
                    "principle": p.get("id"),
                })
    return {"count": len(violations), "violations": violations}


# ----------------------------------------------------------------------
# Principles as memory data (a1) — never static docs
# ----------------------------------------------------------------------

PRINCIPLES_DOMAIN = "architecture-principles"

# PRISM's own principles — the first seeded data. Layer names mirror the
# architecture_analyzer's layer ids for this codebase: models (domain
# dataclasses) / services (application) / api+mcp (interface) / web (ui).
PRISM_PRINCIPLES: list[dict] = [
    {"id": "ARC-PRISM-1", "kind": "layer_rule",
     "from": "models", "must_not_depend_on": "services",
     "why": "domain dataclasses stay dependency-free (models/*.py import "
            "nothing from services/)"},
    {"id": "ARC-PRISM-2", "kind": "layer_rule",
     "from": "services", "must_not_depend_on": "api",
     "why": "application services never reach up into the FastAPI "
            "interface layer"},
    {"id": "ARC-PRISM-3", "kind": "layer_rule",
     "from": "domain", "must_not_depend_on": "infrastructure",
     "why": "the classic arc-kit rule: domain logic must not couple to "
            "infrastructure adapters"},
]


# Generic, repo-agnostic defaults — a sensible starting set for ANY project
# so a fresh customer repo's plan_gate is satisfiable without hand-authoring
# principles first (issue #171). These are the classic clean-architecture
# layer rules (NOT PRISM's own models/services names): seed them, then the
# project tailors via principles_seed(rules=...) or a memory_store edit.
DEFAULT_PRINCIPLES: list[dict] = [
    {"id": "ARC-DEFAULT-1", "kind": "layer_rule",
     "from": "domain", "must_not_depend_on": "infrastructure",
     "why": "the classic clean-architecture rule: domain logic must not "
            "couple to infrastructure adapters"},
    {"id": "ARC-DEFAULT-2", "kind": "layer_rule",
     "from": "interface", "must_not_depend_on": "domain",
     "why": "the interface/delivery layer (controllers, UI) talks to the "
            "application layer, never reaches straight into the domain"},
]


def seed_default_principles(memory_svc: Any,
                            rules: Optional[list[dict]] = None) -> list:
    """Seed a GENERIC default principle set (or `rules`) as MEMORY DATA so a
    fresh project's plan_gate is satisfiable (issue #171). Idempotent:
    MemoryService.store supersedes same-name entries. Mirrors the store
    pattern of seed_prism_principles; returns the stored entries."""
    stored = []
    for p in (rules or DEFAULT_PRINCIPLES):
        stored.append(memory_svc.store(
            domain=PRINCIPLES_DOMAIN,
            name=p["id"],
            description=(f"{p['kind']}: {p['from']} must not depend on "
                         f"{p['must_not_depend_on']} — {p.get('why', '')}"),
            type="decision",
            classification="foundational",
            evidence={"principle": p},
            importance=8,
        ))
    return stored


def seed_prism_principles(memory_svc: Any) -> list:
    """Seed PRISM's own architecture principles as MEMORY DATA (a1).
    Idempotent: MemoryService.store supersedes same-name entries."""
    stored = []
    for p in PRISM_PRINCIPLES:
        stored.append(memory_svc.store(
            domain=PRINCIPLES_DOMAIN,
            name=p["id"],
            description=(f"{p['kind']}: {p['from']} must not depend on "
                         f"{p['must_not_depend_on']} — {p.get('why', '')}"),
            type="decision",
            classification="foundational",
            evidence={"principle": p},
            importance=8,
        ))
    return stored



# ----------------------------------------------------------------------
# Prototype rules become axioms (task c1d0ee70) — "a rule that cannot
# fail is decoration". Four EVALUABLE axioms, each with a real violation
# case in PRISM data, as data + a pure evaluate_axioms(context) so this is
# unit-testable without a live daemon. ADDITIVE ONLY: no existing
# principle/rubric/gate behaviour above is touched by this section.
# ontology_prototype_projection.rebuild wires the real context (task rows,
# document paths, workflow/behavior catalog entries) and persists the
# evaluated state onto OntologyStore's ontology_axioms rows, which the
# Understand ontology view (OntologyPanel.tsx) already renders quietly in
# graphite and spends --alarm only when state == 'violated'.
# ----------------------------------------------------------------------

PROTOTYPE_AXIOMS: list[dict] = [
    {"name": "task-names-its-channel",
     "description": "Every task row names the channel it arrived through "
                     "— a blank channel is a rule with no way to fail."},
    {"name": "no-artifacts-in-the-root",
     "description": "Every document lives inside a folder — one sitting "
                     "loose in the project root breaks the file-tree "
                     "grammar (document_tree.classify's loose_in_root)."},
    {"name": "dated-folder-uses-one-format",
     "description": "Every dated folder uses YYYY-MM-DD — a folder dated "
                     "any other way (2026-Q1, 2026-08) breaks the one "
                     "format the tree commits to (date_format_breaks)."},
    {"name": "skill-description-says-when",
     "description": "Every workflow/behavior catalog entry's description "
                     "says WHEN to use it — one with no 'use when'/'when'/"
                     "'triggers' phrasing gives no trigger condition."},
]

_AXIOM_DETAIL_CAP = 5


def _cap_detail(label: str, rows: list[str]) -> str:
    shown = rows[:_AXIOM_DETAIL_CAP]
    more = len(rows) - len(shown)
    suffix = f" (+{more} more)" if more > 0 else ""
    return f"{label}: {', '.join(shown)}{suffix}"


def _row_get(row: Any, key: str, default: Any = "") -> Any:
    """Read `key` off a plain dict OR an attribute-bearing row object —
    evaluate_axioms stays usable both from hand-built test context dicts
    and from real PRISM rows (Task objects) without a conversion step."""
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _eval_task_names_its_channel(context: dict) -> tuple[str, str]:
    blank = []
    for t in context.get("tasks") or []:
        channel = str(_row_get(t, "channel") or "").strip()
        if channel:
            continue
        label = (str(_row_get(t, "title") or "").strip()
                 or str(_row_get(t, "id") or "").strip() or "?")
        blank.append(label)
    if blank:
        return "violated", _cap_detail("task(s) with a blank channel", blank)
    return "quiet", ""


def _eval_no_artifacts_in_root(context: dict) -> tuple[str, str]:
    from prism_service.services import document_tree

    paths = list(context.get("document_paths") or [])
    loose = document_tree.classify(paths)["loose_in_root"]
    if loose:
        return "violated", _cap_detail("document(s) loose in the root", loose)
    return "quiet", ""


def _eval_dated_folder_uses_one_format(context: dict) -> tuple[str, str]:
    from prism_service.services import document_tree

    paths = list(context.get("document_paths") or [])
    breaks = document_tree.classify(paths)["date_format_breaks"]
    if breaks:
        return "violated", _cap_detail(
            "folder(s) not dated YYYY-MM-DD", breaks)
    return "quiet", ""


_WHEN_RE = re.compile(r"\bwhen\b|\btrigger", re.IGNORECASE)


def _eval_skill_description_says_when(context: dict) -> tuple[str, str]:
    missing = []
    for e in context.get("catalog_entries") or []:
        desc = str(_row_get(e, "description") or "")
        if _WHEN_RE.search(desc):
            continue
        label = str(_row_get(e, "id") or desc[:30] or "?")
        missing.append(label)
    if missing:
        return "violated", _cap_detail(
            "catalog entr(y/ies) with no when/triggers phrasing", missing)
    return "quiet", ""


_AXIOM_EVALUATORS = {
    "task-names-its-channel": _eval_task_names_its_channel,
    "no-artifacts-in-the-root": _eval_no_artifacts_in_root,
    "dated-folder-uses-one-format": _eval_dated_folder_uses_one_format,
    "skill-description-says-when": _eval_skill_description_says_when,
}


def evaluate_axioms(context: dict) -> list[dict]:
    """Evaluate the four prototype axioms against `context` (pure).

    context carries the rows each axiom checks: {"tasks": [...],
    "document_paths": [...], "catalog_entries": [...]}. Each row may be a
    plain dict or an attribute-bearing object (Task rows work directly).
    Returns [{"name","description","state":'quiet'|'violated',"detail"}],
    one row per PROTOTYPE_AXIOMS entry in order. A missing context key
    reads as no rows to check, i.e. quiet — an axiom needs an ACTUAL
    violating row to fire, never the absence of data.
    """
    out = []
    for axiom in PROTOTYPE_AXIOMS:
        state, detail = _AXIOM_EVALUATORS[axiom["name"]](context)
        out.append({
            "name": axiom["name"], "description": axiom["description"],
            "state": state, "detail": detail,
        })
    return out


def load_principles(memory_svc: Any) -> list[dict]:
    """Read machine-checkable principles back from memory data.
    Empty store -> [] (the misfire guard upstream refuses to pass)."""
    rules: list[dict] = []
    entries = memory_svc.list_entries(PRINCIPLES_DOMAIN)
    for e in entries:
        if getattr(e, "status", "active") != "active":
            continue
        if getattr(e, "invalid_at", ""):
            continue
        principle = (getattr(e, "evidence", None) or {}).get("principle")
        if isinstance(principle, str):
            try:
                principle = json.loads(principle)
            except json.JSONDecodeError:
                principle = None
        if isinstance(principle, dict):
            rules.append(principle)
    return rules
