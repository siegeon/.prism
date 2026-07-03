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
