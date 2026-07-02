"""Server-side plan scaffolder — rubric-passing stories and diagrams
(task ec6932b7, C3 of the PI-orchestration build, parent 81b23574 FR-3).

The model never authors a whole plan_doc or raw mermaid. This module
owns the SHAPE in code:

  * ``scaffold_plan_doc`` emits exactly the headers + markers
    ``arc_governance.score_story_complete`` requires (## Summary /
    ## Requirements FR-n / ## Acceptance Criteria with '- AC-n: ...
    - oracle: <check>' bullets);
  * ``scaffold_plan_diagram`` emits a TEMPLATED ``flowchart TD`` whose
    node ids are LAYER-NEUTRAL (``flow_*``) with bare edge lines so
    ``arc_governance.mermaid_edges`` can actually extract the topology
    for conformance scoring;
  * the model fills only small slot text through the C2 interface
    (``inference/pi_slots.fill_slot`` — bounded retry, deterministic
    fallback, stub-injectable);
  * ``build_plan`` SELF-CHECKS with the REAL governance scorers before
    returning (risk R2: a future principle set must never silently
    redden plan_gate): score_story_complete must pass, mermaid_parses
    must be true, score_plan_coverage must report zero violations. A
    violation that names a templated node id triggers ONE deterministic
    ``n_``-prefix rename + re-check; anything still failing is returned
    honestly (ok=False with the scorer reason), never swallowed.
"""

from __future__ import annotations

import re
from typing import Callable, Optional, Sequence

from prism_service.inference import pi_slots
from prism_service.services import arc_governance as gov


# ----------------------------------------------------------------------
# Deterministic assembly — the rubric shape lives HERE, not in a prompt
# ----------------------------------------------------------------------

def scaffold_plan_doc(
    summary: str,
    frs: Sequence[str],
    acs: Sequence[tuple[str, str]],
) -> str:
    """Assemble a rubric-exact plan_doc. ``acs`` is [(text, oracle)];
    FR-n / AC-n ids are auto-numbered from 1."""
    lines: list[str] = ["## Summary", "", (summary or "").strip(), ""]
    lines += ["## Requirements", ""]
    for i, fr in enumerate(frs, start=1):
        lines.append(f"FR-{i}: {str(fr).strip()}")
    lines += ["", "## Acceptance Criteria", ""]
    for i, (text, oracle) in enumerate(acs, start=1):
        lines.append(f"- AC-{i}: {str(text).strip()} "
                     f"- oracle: {str(oracle).strip()}")
    return "\n".join(lines) + "\n"


_NODE_PREFIX = "flow_"


def _slug(text: str, i: int) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")
    return f"{_NODE_PREFIX}{s[:24] or f'step{i}'}"


def scaffold_plan_diagram(
    feature: str,
    steps: Optional[Sequence[str]] = None,
) -> str:
    """Templated layer-neutral mermaid: bare 'flowchart TD' first line,
    labeled node declarations, then BARE edge lines (id --> id) so the
    governance edge extractor sees the real topology."""
    labels = [str(s).strip() for s in (steps or []) if str(s).strip()]
    if not labels:
        labels = ["gather context", "author slices", "verify outcome"]
    ids = [f"{_NODE_PREFIX}ask"]
    decls = [f'  {_NODE_PREFIX}ask["{_esc(feature) or "feature ask"}"]']
    for i, label in enumerate(labels, start=1):
        nid = _slug(label, i)
        if nid in ids:
            nid = f"{nid}_{i}"
        ids.append(nid)
        decls.append(f'  {nid}["{_esc(label)}"]')
    ids.append(f"{_NODE_PREFIX}done")
    decls.append(f'  {_NODE_PREFIX}done["verified outcome"]')
    edges = [f"  {a} --> {b}" for a, b in zip(ids, ids[1:])]
    return "flowchart TD\n" + "\n".join(decls + edges) + "\n"


def _esc(text: str) -> str:
    return str(text or "").replace('"', "'").replace("\n", " ").strip()


# ----------------------------------------------------------------------
# Rename recovery — deterministic, one pass (FR-5)
# ----------------------------------------------------------------------

def _rename_nodes(diagram: str, names: set[str]) -> str:
    """Prefix each offending node id with ``n_`` everywhere it appears
    as a whole identifier. Pure text transform, deterministic."""
    out = diagram
    for name in sorted(names):
        out = re.sub(rf"\b{re.escape(name)}\b", f"n_{name}", out)
    return out


# ----------------------------------------------------------------------
# build_plan — slots in, self-checked plan out
# ----------------------------------------------------------------------

def build_plan(
    context: dict,
    *,
    model: Optional[Callable[..., str]] = None,
    memory_svc=None,
    principles: Optional[list[dict]] = None,
    n_frs: int = 3,
    n_acs: int = 3,
) -> dict:
    """Author a plan_doc + plan_diagram with the model confined to the
    pi_slots slot-fill seam, then SELF-CHECK with the real governance
    scorers. Returns {ok, plan_doc, plan_diagram, checks:{story, plan,
    mermaid}, renamed, slot_calls}."""
    ctx = dict(context or {})

    def _fill(slot: str, extra: Optional[dict] = None) -> str:
        merged = dict(ctx, **(extra or {}))
        return pi_slots.fill_slot(slot, merged, model=model).value

    summary = _fill("summary")
    frs = [_fill("fr_line", {"index": i}) for i in range(1, max(1, n_frs) + 1)]
    acs = [(_fill("ac_line", {"index": i}), _fill("oracle", {"index": i}))
           for i in range(1, max(1, n_acs) + 1)]

    plan_doc = scaffold_plan_doc(summary, frs, acs)
    steps = [text for text, _ in acs]
    plan_diagram = scaffold_plan_diagram(
        str(ctx.get("feature_ask") or ctx.get("title") or ""), steps=steps)

    if principles is None:
        principles = (gov.load_principles(memory_svc)
                      if memory_svc is not None else [])

    rubrics = gov.load_rubrics()
    evidence = {"story_md": plan_doc, "plan_doc": plan_doc,
                "plan_diagram": plan_diagram}
    story = gov.score_story_complete(evidence, rubrics.get("story_complete") or {})
    plan = gov.score_plan_coverage(
        evidence, rubrics.get("plan_coverage") or {}, principles)

    renamed = False
    if not plan.get("ok") and plan.get("violations"):
        # A principle names a templated node: deterministic rename + ONE
        # re-check. Layer-neutral ids make this vanishingly rare; when a
        # future principle set collides anyway, the plan must not
        # silently redden the gate (risk R2).
        offenders = {str(v.get("from") or "") for v in plan["violations"]}
        offenders |= {str(v.get("to") or "") for v in plan["violations"]}
        offenders.discard("")
        plan_diagram = _rename_nodes(plan_diagram, offenders)
        evidence["plan_diagram"] = plan_diagram
        plan = gov.score_plan_coverage(
            evidence, rubrics.get("plan_coverage") or {}, principles)
        renamed = True

    mermaid_ok = gov.mermaid_parses(plan_diagram)
    ok = bool(story.get("ok")) and bool(plan.get("ok")) and mermaid_ok
    return {
        "ok": ok,
        "plan_doc": plan_doc,
        "plan_diagram": plan_diagram,
        "checks": {"story": story, "plan": plan, "mermaid": mermaid_ok},
        "renamed": renamed,
    }
