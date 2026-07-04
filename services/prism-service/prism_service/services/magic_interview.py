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
import re

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
        if chk["blocking"]:
            state = EXHAUSTED if self.round >= self.max_rounds else CLARIFYING
            return state, chk["blocking"]
        # Queue semantics (owner feedback: one question at a time): each
        # clarifying question is asked AT MOST ONCE — a "no" dismisses it
        # (the spec doesn't change, and find_gaps would otherwise regenerate
        # it forever), while refine-added entities can surface new ones.
        # Blocking gaps above are exempt: those must be RESOLVED, not waved off.
        asked = {a.get("question") for a in self.answered}
        fresh = [g for g in chk["clarifying"] if g["question"] not in asked]
        if fresh and self.round < self.max_rounds:
            return CLARIFYING, fresh
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
        # Deterministic first: answers that carry structure fold straight
        # into the spec BEFORE the stochastic refine, so the interview can
        # never re-ask what the customer already said (owner: b3aca498 —
        # "yeah - booked, done, or no-show" must become the enum on the spot).
        for qa in qa_pairs:
            gap = next((g for g in self.pending
                        if g.get("question") == qa.get("question")), None)
            if gap:
                _fold_answer(self.spec, gap, qa.get("answer") or "")
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
                "pending": self.pending,
                "description": getattr(self, "description", "")}

    @classmethod
    def from_dict(cls, d: dict) -> "SpecInterview":
        obj = cls.__new__(cls)
        obj.spec = d["spec"]; obj.max_rounds = d.get("max_rounds", 6)
        obj.round = d.get("round", 0); obj.answered = d.get("answered", [])
        obj._prev_fp = d.get("prev_fp")
        obj.state = d.get("state"); obj.pending = d.get("pending", [])
        obj.description = d.get("description", "")
        return obj


_DECLINE = re.compile(
    r"^\s*(ha\s+)?(no|nope|nah|skip|not (needed|really)|don.?t)", re.IGNORECASE)


def _fold_answer(spec: dict, gap: dict, answer: str) -> None:
    """Deterministically fold a structured answer into the spec. Enum values
    ('booked, done, or no-show') become the enum rule; a yes to a min bound
    becomes the rule. Declines fold nothing (the ask-once queue dismisses)."""
    ent_name, field = gap.get("entity"), gap.get("field")
    if not ent_name or not field or _DECLINE.match(answer or ""):
        return
    ent = next((e for e in spec.get("entities", []) or []
                if (e.get("name") or "").lower() == str(ent_name).lower()), None)
    if ent is None:
        return
    kind = gap.get("kind")
    if kind == "missing_enum":
        # strip filler, split on commas/or/slashes; keep short value tokens
        text = re.sub(r"^(like i said|i said|yes|yeah|yep|sure|ok(ay)?)[,:\-\s]*",
                      "", (answer or "").strip(), flags=re.IGNORECASE)
        vals = [v.strip().strip(".").lower()
                for v in re.split(r",|/| or | and ", text)]
        vals = [v for v in vals if v and len(v) <= 24][:8]
        if len(vals) >= 2 and not any(
                r.get("type") == "enum" and r.get("field") == field
                for r in ent.get("rules", []) or []):
            ent.setdefault("rules", []).append(
                {"type": "enum", "field": field, "values": vals})
    elif kind == "maybe_min":
        if not any(r.get("type") == "min" and r.get("field") == field
                   for r in ent.get("rules", []) or []):
            ent.setdefault("rules", []).append(
                {"type": "min", "field": field, "value": 0})


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
    desc = getattr(iv, "description", "")
    if desc:
        facts.append({"name": "in-their-words",
                      "text": f"The owner describes the business: {desc}"})
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
    # Each customer project is its OWN app: module + db are namespaced by
    # PROJECT, never by the drafted name — two salons must never share
    # tables or overwrite each other's hosted app (owner: multiplicity).
    spec = dict(spec)
    spec["module"] = project
    spec["db"] = project
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
    # 1b. the generated UI (their FACE) — deterministic Puck JSON + design
    # tokens from the SAME spec, so backend and UI never drift. The preview
    # page renders app.json; per-entity files are the drag-drop editor seeds.
    try:
        from prism_service.services import magic_ui as mui
        from prism_service.services import design_intel as di
        # per-industry design pass (keyless BM25): derive the industry
        # from the confirmed domain fact or the app's db/module name,
        # then let design_intel pick a tasteful per-vertical palette. A
        # design failure falls back to neutral tokens, never blocks.
        # the customer's own words are the strongest design signal ("I run
        # a dog grooming salon" -> salon palette); db/module is the fallback.
        industry = spec.get("db") or spec.get("module") or ""
        words = " ".join(f.get("text", "") for f in (facts or [])
                         if f.get("name") == "in-their-words")
        if words:
            industry = words
        try:
            tokens = di.design_tokens(industry)
        except Exception:
            tokens = None
        ui_out = mui.render_ui(spec, tokens=tokens)
        ui_dir = src / "ui"
        ui_dir.mkdir(parents=True, exist_ok=True)
        (ui_dir / "app.json").write_text(
            json.dumps(ui_out, indent=2), encoding="utf-8")
        for ent, page in ui_out["pages"].items():
            (ui_dir / f"{ent}.puck.json").write_text(
                json.dumps(page, indent=2), encoding="utf-8")
    except Exception:
        pass
    # 2. deploy to the live Magic tenant
    ab.deploy_app(spec)
    # 2b. the customer's REAL app: a standalone frontend hosted BY Magic —
    # they walk away with a URL, not a preview inside PRISM.
    app_url = None
    try:
        from prism_service.services import magic_ui as mui2
        from prism_service.services import magic_client as mc2
        tokens2 = None
        try:
            from prism_service.services import design_intel as di2
            seed2 = " ".join(f.get("text", "") for f in (facts or [])
                             if f.get("name") == "in-their-words") \
                or spec.get("db") or spec.get("module") or ""
            tokens2 = di2.design_tokens(seed2)
        except Exception:
            pass
        html = mui2.render_frontend(spec, tokens=tokens2)
        (src / "index.html").write_text(html, encoding="utf-8")
        path = ab.deploy_frontend(spec.get("module") or project, html)
        base = (mc2.status() or {}).get("url", "").rstrip("/")
        app_url = f"{base}{path}" if base else path
    except Exception:
        pass
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
    # 5. durable MEMORIES — the confirmed business facts land in PRISM's
    # memory system so memory_recall knows THIS customer's business, and the
    # completed spec teaches the gap detector (persisted archetype learning).
    memories = 0
    try:
        from prism_service.services.memory_service import MemoryService
        mem = MemoryService(str(dd / "projects" / project / "mulch"))
        for i, f in enumerate(facts or []):
            mem.store(domain="business",
                      name=f"{f.get('name', 'fact')}-{i}",
                      description=f.get("text", ""),
                      type="fact", classification="business-knowledge",
                      evidence={"source": "magic interview"},
                      memory_type="semantic")
            memories += 1
    except Exception:
        pass
    try:
        from prism_service.services import magic_spec_gaps as _gaps
        _gaps.learn_from_spec(spec)
    except Exception:
        pass
    mod = spec.get("module", project)
    endpoints = []
    for e in spec.get("entities", []) or []:
        endpoints += [f"GET magic/modules/{mod}/{e['name']}",
                      f"POST magic/modules/{mod}/{e['name']}"]
    return {"project": project, "module": mod, "endpoints": endpoints,
            "source": str(src), "brain_docs": docs, "memories": memories,
            "app_url": app_url,
            "preview_url": f"/magic/preview?project={project}"}


# --- conversational onboarding (the PI panel drives this over one tiny tool) --
#
# converse() is the single server-side entrypoint the PI panel's magic_interview
# tool proxies to. The 0.6b model only relays TEXT: a business description on the
# first turn, the customer's replies on later turns. ALL the heavy lifting —
# drafting the draft spec, pairing answers to pending questions, advancing the
# state machine, auto-building on ready — happens HERE, deterministically.


def _slm_refine(spec: dict, answered: list) -> dict:
    """SLM-backed spec refinement (mirror of api/magic.py's interview refine):
    fold the customer's clarifications into the running spec. On any SLM hiccup
    the spec is returned unchanged — the state machine copes and re-asks."""
    import json as _json
    import re as _re
    from prism_service.services import magic_ai
    qa = "\n".join(f"Q: {a['question']}\nA: {a['answer']}" for a in answered)
    prompt = (f"Current app spec (JSON):\n{_json.dumps(spec)}\n\n"
              f"Clarifications from the customer:\n{qa}\n\nOutput the UPDATED "
              "app spec incorporating the clarifications, same JSON shape. "
              "Output ONLY JSON.")
    try:
        text, _ = magic_ai.local_complete(
            [{"role": "system", "content": "You update a JSON app spec."},
             {"role": "user", "content": prompt}])
        m = _re.search(r"\{.*\}", _re.sub(r"<think>.*?</think>", "", text,
                                          flags=_re.DOTALL), _re.DOTALL)
        return _json.loads(m.group(0)) if m else spec
    except Exception:
        return spec


def draft_spec(description: str) -> dict:
    """Derive a first-cut app spec from the customer's plain-language business
    description via ONE local model call. An empty/failed draft returns {} —
    the interview then opens with the gap detector's own questions, so the
    conversation still starts (graceful degradation, never a crash)."""
    import json as _json
    import re as _re
    from prism_service.services import magic_ai
    desc = (description or "").strip()
    if not desc:
        return {}
    prompt = (f'A customer describes their business:\n"{desc}"\n\n'
              "Design the smallest useful app spec as JSON of EXACTLY this "
              "shape:\n"
              '{"module": "<short_snake_name>", "entities": [{"name": '
              '"<snake_plural>", "fields": [{"name": "<snake>", "type": '
              '"TEXT|INTEGER|DECIMAL|DATE|BOOLEAN"}], "rules": []}]}\n'
              "Choose the 1-3 core things this business must track. "
              "Output ONLY JSON.")
    try:
        text, _ = magic_ai.local_complete(
            [{"role": "system", "content": "You design a JSON app spec from a "
              "business description."},
             {"role": "user", "content": prompt}])
        m = _re.search(r"\{.*\}", _re.sub(r"<think>.*?</think>", "", text,
                                          flags=_re.DOTALL), _re.DOTALL)
        return _json.loads(m.group(0)) if m else {}
    except Exception:
        return {}


def converse(project: str, description: str | None = None,
             answers: list | None = None, data_dir=None,
             refine=None, draft=None) -> dict:
    """One conversational turn for a customer building an app by TALKING.

    First visit (no saved session): `description` is drafted into a spec and the
    interview opens. Later visits: `answers` are paired to the pending questions
    IN ORDER (pad/truncate) and folded in. Persists/resumes the session per
    project automatically; on `ready` it auto-builds the app ONCE and surfaces
    `preview_url`. `refine`/`draft` are injectable for tests (default = SLM)."""
    refine = refine or _slm_refine
    draft = draft or draft_spec
    proj = (project or "").strip()
    if isinstance(answers, str):
        answers = [answers]
    replies = [a.strip() for a in (answers or [])
               if isinstance(a, str) and a.strip()]
    desc = (description or "").strip()

    sess = load_session(proj, data_dir) if proj else None
    was_ready = bool(sess) and sess.get("state") == READY
    if not sess:
        seed = desc or " ".join(replies)
        iv = SpecInterview(draft(seed) if seed else {}, max_rounds=10)
        iv.description = seed          # their words drive the design pass
    else:
        iv = SpecInterview.from_dict(sess)
        if iv.state == CLARIFYING:
            pending = iv.questions()
            src = replies or ([desc] if desc else [])
            qa = [{"question": pending[i], "answer": src[i]}
                  for i in range(min(len(pending), len(src)))]
            if qa:
                iv.answer(qa, refine)

    artifacts = None
    if proj:
        save_session(proj, iv.to_dict(), data_dir)
        if iv.state == READY and not was_ready:   # build ONCE, on transition
            facts = business_facts(iv)
            record_facts(proj, facts, data_dir)
            try:                                    # auto-build the customer app
                artifacts = finalize_build(proj, iv.spec, data_dir=data_dir,
                                           facts=facts)
            except Exception as e:                  # surface, don't crash the turn
                artifacts = {"error": str(e)[:160]}

    if proj and iv.state == READY and artifacts is None:
        # Resumed while ready: if the prior build never landed (no ui/app.json
        # on disk), self-heal by rebuilding NOW instead of advertising a
        # broken app. A healthy prior build skips this (file exists).
        from pathlib import Path as _Path
        from prism_service import config as _config
        base = _Path(data_dir) if data_dir is not None else _Path(_config.DATA_DIR)
        built = base / "projects" / proj / "magic_app" / "ui" / "app.json"
        if not built.is_file():
            facts = business_facts(iv)
            record_facts(proj, facts, data_dir)
            try:
                artifacts = finalize_build(proj, iv.spec, data_dir=data_dir,
                                           facts=facts)
            except Exception as e:
                artifacts = {"error": str(e)[:160]}

    # HONEST ready: never hand out a preview_url for a failed build — the
    # panel's "app is ready" banner keys off it (bit on corefit: 502 + no
    # theme behind a green banner).
    preview_url = None
    build_failed = isinstance(artifacts, dict) and artifacts.get("error")
    if iv.state == READY and not build_failed:
        if isinstance(artifacts, dict) and artifacts.get("preview_url"):
            preview_url = artifacts["preview_url"]
        elif proj:
            preview_url = f"/magic/preview?project={proj}"
    return {"state": iv.state, "domain": iv.domain(),
            "questions": iv.questions(), "round": iv.round,
            "preview_url": preview_url, "artifacts": artifacts}
