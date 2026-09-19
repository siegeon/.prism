"""Authoring-time oracle validation — "the oracle like a compiler"
(task b78a193c).

WHY A SEPARATE FILE: ``oracle_spec.py`` is one of the enumerated gate-POLICY
files (``control_plane.POLICY_FILES``) — the candidate-controls-judge tooth
refuses ANY worktree diff to it, because a task under test must never be able
to edit the very judge that scores it. Authoring-time validation is a
CONSUMER of the spec derivation (it calls ``OracleSpec.from_task`` the same
way the gate does, just earlier), not part of the judge itself — it never
runs during a gate decision. Living outside the policy surface lets feature
tasks like this one add/evolve authoring checks without ever touching the
judge, so the tooth never has to fire for legitimate, additive work. Only
READS from ``oracle_spec`` (``OracleSpec``); it
must never import anything that requires editing that module.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from prism_service.services.oracle_spec import OracleSpec


# Repo root: this file is <root>/services/prism-service/prism_service/services/
_REPO_ROOT = Path(__file__).resolve().parents[4]


def _is_test_path(rel: str) -> bool:
    r = rel.replace("\\", "/").lower()
    return "/tests/" in r or r.startswith("tests/") or "/test_" in r


def _unwired_reason(allowed_files: Optional[list]) -> str:
    """A slice that introduces a NEW module while touching NO existing
    production module is unwired — nothing will ever construct it.

    This is the exact shape that let epic 0784729f ship five green slices
    whose feature could not run: `register_adapter` had no production caller
    because every test injected its own adapter (mx-8f4666).

    Deliberately quiet for the legitimate shapes: an empty list, a tests-only
    slice, or any slice that touches something already on disk. A validator
    that refuses honest work gets routed around, which is worse than none.
    """
    files = [str(f).strip() for f in (allowed_files or []) if str(f).strip()]
    prod = [f for f in files if not _is_test_path(f)]
    if not prod:
        return ""  # nothing to wire (empty list, or tests only)
    existing = [f for f in prod if (_REPO_ROOT / f).exists()]
    if existing:
        return ""  # touches something real — a caller can be edited there
    new_modules = [f for f in prod if f.endswith(".py") or f.endswith(".tsx")]
    if not new_modules:
        return ""
    named = ", ".join(Path(f).name for f in new_modules[:3])
    return (
        f"unwired slice: {named} would be created but allowed_files names no "
        "EXISTING production module, so nothing will construct it (this is how "
        "an epic ships green slices whose feature cannot run). Add the wiring "
        "file — the api module, main.py, project_context.py or whichever "
        "module instantiates it — to allowed_files, or fold this into the "
        "slice that does."
    )


# An oracle written only in test vocabulary describes a verify command, not an
# outcome. It must ALSO name something a person can reach.
_TEST_VOCAB = re.compile(
    r"\b(focused tests|contract tests|unit tests?|integration tests?|"
    r"tests? (prove|show|pin|assert|demonstrate)|test suite)\b", re.I)
_SURFACE = re.compile(
    r"(https?://|/api/|\b(page|screen|tab|view|button|dashboard|sidebar|"
    r"settings|browser|ui)\b|\buser (opens|clicks|sees|visits|selects|runs)\b|"
    r"\b(opens|clicks|sees|visits)\b|\bcli\b|\bcommand line\b|\$ )", re.I)


def _oracle_surface_reason(oracle: str) -> str:
    """Refuse an oracle that is satisfiable with every collaborator mocked."""
    if not _TEST_VOCAB.search(oracle):
        return ""      # not a test-vocabulary oracle at all
    if _SURFACE.search(oracle):
        return ""      # names tests AND a surface — the target shape
    return (
        "oracle names only tests, no user-reachable surface: an oracle that "
        "passes with every collaborator mocked proves the slice, not the "
        "outcome. Name what a person can reach — a URL, an /api path, the "
        "screen or the command — alongside the tests."
    )


def validate_for_authoring(oracle: str = "", proof_type: str = "",
                           verify: Optional[list] = None,
                           likely_misfire: str = "",
                           allowed_files: Optional[list] = None) -> tuple:
    """Authoring-time OracleSpec validation ("the oracle like a compiler").

    Derives the spec the same way ``from_task`` would at gate time, straight
    from the raw would-be task fields, so a hard contradiction surfaces
    BEFORE the task exists rather than lazily at green_gate. Returns
    ``(spec_summary, domain_errors)``:
      - ``spec_summary``: ``OracleSpec.as_dict()`` when ``oracle`` is
        non-empty (adapter/target/spec_hash/derived); ``None`` when there is
        nothing to derive (no oracle given) — that stays the honest no-op
        it is today.
      - ``domain_errors``: human strings, each naming exactly how to
        repair. The ONLY hard contradiction: ``proof_type=="test"``,
        ``oracle`` non-empty, and ``verify[]`` names nothing runnable at
        all. The test MECHANISM is deliberately not judged here — that is
        the ontology's rule and the validator's call against the project
        being built. Oracle-less tasks and manual-evidence proof_types
        (demo/browser/review/decision/artifact/metric/unset) never
        populate this — their honest browser/manual fallback is a
        legitimate spec, not an error.
    """
    oracle_s = str(oracle or "").strip()
    proof_type_l = str(proof_type or "").strip().lower()
    verify_l = list(verify or [])
    if not oracle_s:
        return None, []
    # Authoring checks that stop the 0784729f shape being written down again
    # (mx-8f4666): a slice nothing constructs, and an oracle that passes fully
    # mocked. Both feed the SAME domain_errors the callers already treat as
    # fatal, so no caller logic changes shape.
    structural: list = []
    for reason in (_unwired_reason(allowed_files), _oracle_surface_reason(oracle_s)):
        if reason:
            structural.append(reason)
    duck = SimpleNamespace(oracle=oracle_s, likely_misfire=likely_misfire or "",
                           proof_type=proof_type_l, verify=verify_l)
    spec = OracleSpec.from_task(duck)
    domain_errors: list = list(structural)
    # THE MECHANISM IS THE PROJECT'S, NOT THIS FUNCTION'S (owner, 2026-09-18).
    # This check used to demand a runnable PYTEST node id, which silently made
    # pytest the only expressible proof in the whole tracker: a task targeting
    # Hyperlambda modules, a C# xUnit suite or a browser demo could not be
    # written down at all, because `verify[]` had to look like Python. That is
    # a static rule pretending to be a judgement. Whether an entry is a valid
    # test is a question about the project BEING BUILT, and it belongs to the
    # agentic validator that can read that project's own test convention --
    # not to a substring match here. So authoring keeps only the structural
    # claim it can honestly make: a task that promises a test proof must name
    # SOMETHING runnable. What that something is, the validator decides.
    if proof_type_l == "test" and not [
            v for v in verify_l if str(v or "").strip()]:
        domain_errors.append(
            "proof_type=test but verify[] is empty — name at least one "
            "runnable target for this project's own test mechanism "
            "(a pytest node id, a `dotnet test` suite, a Hyperlambda "
            "endpoint test, ...). The mechanism is not required to be "
            "pytest; the validator checks it against the project."
        )
    return spec.as_dict(), domain_errors
