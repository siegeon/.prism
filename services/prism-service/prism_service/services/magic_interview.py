"""Reverse-engineering interview — a deterministic state machine.

The small model is stochastic; the interview must not be. SpecInterview
drives ask -> answer -> refine -> re-check until the spec is STABLE and
COMPLETE, so we get consistent results regardless of SLM wobble and never
build on a half-baked spec. Its state is serializable, so a customer's
interview lives in PRISM's memory/context and can be paused and resumed.

States:
  clarifying — gaps remain (blocking first); the SLM voices the questions.
  ready      — no blocking gaps AND the spec has converged; safe to build.
  exhausted  — hit the round cap with a blocking gap still open; escalate.
"""

from __future__ import annotations

import json

from prism_service.services import magic_app_builder as ab
from prism_service.services import magic_spec_gaps as gaps

CLARIFYING, READY, EXHAUSTED = "clarifying", "ready", "exhausted"


def _fingerprint(spec: dict) -> str:
    """Stable signature of the spec's shape — entities, fields, rules —
    used to detect convergence (the spec stopped changing)."""
    ents = []
    for e in sorted(spec.get("entities", []) or [], key=lambda x: x.get("name", "")):
        fields = sorted((f.get("name", ""), (f.get("type") or "").upper())
                        for f in e.get("fields", []) or [])
        rules = sorted((r.get("type", ""), r.get("field", ""),
                        r.get("ref", ""), tuple(sorted(r.get("values", []))))
                       for r in e.get("rules", []) or [])
        ents.append((e.get("name", ""), tuple(fields), tuple(rules)))
    return json.dumps(ents, sort_keys=True)


class SpecInterview:
    """Deterministic controller for the reverse-engineering interview."""

    def __init__(self, spec: dict, max_rounds: int = 6):
        self.spec = ab.normalize_spec(spec)
        self.max_rounds = max_rounds
        self.round = 0
        self.answered: list[dict] = []
        self._prev_fp: str | None = None
        self.state, self.pending = self._evaluate()

    def _evaluate(self):
        chk = gaps.check_spec(self.spec)
        fp = _fingerprint(self.spec)
        converged = self._prev_fp is not None and self._prev_fp == fp
        if chk["blocking"]:
            state = EXHAUSTED if self.round >= self.max_rounds else CLARIFYING
            return state, chk["blocking"]
        if chk["clarifying"] and not converged and self.round < self.max_rounds:
            return CLARIFYING, chk["clarifying"]
        return READY, []

    # --- interview surface -------------------------------------------------
    def questions(self) -> list[str]:
        return [g["question"] for g in self.pending]

    def domain(self):
        return gaps.match_domain(self.spec)

    def answer(self, qa_pairs: list[dict], refine) -> str:
        """Fold the customer's answers in, refine the spec (SLM-backed
        `refine(spec, answered) -> new_spec`), re-check, advance. Returns the
        new state."""
        if self.state in (READY, EXHAUSTED):
            return self.state
        self.answered.extend(qa_pairs)
        self._prev_fp = _fingerprint(self.spec)
        self.spec = ab.normalize_spec(refine(self.spec, self.answered))
        self.round += 1
        self.state, self.pending = self._evaluate()
        return self.state

    # --- persistence (lives in PRISM memory/context) -----------------------
    def to_dict(self) -> dict:
        return {"spec": self.spec, "max_rounds": self.max_rounds,
                "round": self.round, "answered": self.answered,
                "prev_fp": self._prev_fp, "state": self.state,
                "pending": self.pending}

    @classmethod
    def from_dict(cls, d: dict) -> "SpecInterview":
        obj = cls.__new__(cls)
        obj.spec = d["spec"]; obj.max_rounds = d.get("max_rounds", 6)
        obj.round = d.get("round", 0); obj.answered = d.get("answered", [])
        obj._prev_fp = d.get("prev_fp")
        obj.state = d.get("state"); obj.pending = d.get("pending", [])
        return obj


def run(spec: dict, refine, answer_provider, max_rounds: int = 6) -> SpecInterview:
    """Auto-run the interview to a terminal state (ready/exhausted). Used for
    automation + tests. answer_provider(questions) -> list[{question,answer}]."""
    iv = SpecInterview(spec, max_rounds)
    while iv.state == CLARIFYING:
        qa = answer_provider(iv.questions())
        iv.answer(qa, refine)
    return iv


# --- persistence: the interview lives in PRISM's per-project memory ----------

def session_path(project: str, data_dir=None):
    """Where a customer project's interview session is stored."""
    from prism_service import config
    base = data_dir if data_dir is not None else config.DATA_DIR
    from pathlib import Path
    return Path(base) / "projects" / project / ".interview.json"


def save_session(project: str, session: dict, data_dir=None) -> None:
    """Persist a serialized interview so it survives restarts + visits."""
    p = session_path(project, data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(session), encoding="utf-8")
    import os
    os.replace(tmp, p)


def load_session(project: str, data_dir=None) -> dict | None:
    """Load a customer's saved interview, or None if they haven't started."""
    p = session_path(project, data_dir)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_session(project: str, data_dir=None) -> bool:
    try:
        session_path(project, data_dir).unlink()
        return True
    except (FileNotFoundError, OSError):
        return False


def business_facts(iv: "SpecInterview") -> list[dict]:
    """The durable knowledge PRISM keeps about THIS customer's business, once
    the interview is READY: the domain, the entities/rules they confirmed, and
    every Q&A. Recorded as memories so PRISM's brain becomes their expert."""
    facts: list[dict] = []
    dom = iv.domain()
    if dom:
        facts.append({"name": f"domain-{dom}",
                      "text": f"This customer's business is a {dom}."})
    for e in iv.spec.get("entities", []) or []:
        fields = ", ".join(f.get("name", "") for f in e.get("fields", []) or [])
        rules = "; ".join(f"{r.get('type')}({r.get('field')})"
                          for r in e.get("rules", []) or [])
        facts.append({"name": f"entity-{e.get('name')}",
                      "text": f"Tracks {e.get('name')} with fields: {fields}."
                              + (f" Rules: {rules}." if rules else "")})
    for qa in iv.answered:
        facts.append({"name": "clarification",
                      "text": f"Q: {qa.get('question')} A: {qa.get('answer')}"})
    return facts


def record_facts(project: str, facts: list[dict], data_dir=None) -> str:
    """Write the customer's confirmed business facts as a durable markdown
    memory under the project, so PRISM's brain ingests it and stays their
    expert. Returns the path written."""
    p = session_path(project, data_dir).with_name("business-facts.md")
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Business knowledge — {project}", ""]
    for f in facts:
        lines.append(f"- {f['text']}")
    p.write_text("\n".join(lines), encoding="utf-8")
    return str(p)


def finalize_build(project: str, spec: dict, data_dir=None,
                   facts: list | None = None) -> dict:
    """READY -> a real customer app. Renders + deploys the Magic backend,
    writes the generated Hyperlambda + business facts as the project SOURCE,
    registers the project's source_path, and ingests the code into the project
    BRAIN — so the customer sees their code, brain and understand as one system.
    """
    from pathlib import Path
    from prism_service import config
    from prism_service.services import magic_app_builder as ab
    dd = Path(data_dir) if data_dir is not None else Path(config.DATA_DIR)
    src = dd / "projects" / project / "magic_app"
    src.mkdir(parents=True, exist_ok=True)
    if facts:  # business knowledge lands with the code so the brain learns it
        (src / "business-facts.md").write_text(
            "# Business knowledge\n\n"
            + "\n".join(f"- {f['text']}" for f in facts), encoding="utf-8")
    # 1. the generated code (their CODE artifact)
    app = ab.render_app(spec)
    for path, content in app["files"].items():
        (src / Path(path).name).write_text(content, encoding="utf-8")
    (src / "_schema.hl").write_text(app["schema"], encoding="utf-8")
    # 2. deploy to the live Magic tenant
    ab.deploy_app(spec)
    # 3. register the project source (so /understand shows it)
    try:
        from prism_service.services import source_service as ss
        ss.set_source_path(project, str(src))
    except Exception:
        pass
    # 4. ingest into the project brain (their BRAIN + understand artifact)
    docs = 0
    try:
        from prism_service.engines.brain_engine import Brain
        base = dd / "projects" / project
        brain = Brain(brain_db=str(base / "brain.db"),
                      graph_db=str(base / "graph.db"),
                      scores_db=str(base / "scores.db"))
        docs = brain.ingest([str(src)])
    except Exception:
        pass
    mod = spec.get("module", project)
    endpoints = []
    for e in spec.get("entities", []) or []:
        endpoints += [f"GET magic/modules/{mod}/{e['name']}",
                      f"POST magic/modules/{mod}/{e['name']}"]
    return {"project": project, "module": mod, "endpoints": endpoints,
            "source": str(src), "brain_docs": docs}
