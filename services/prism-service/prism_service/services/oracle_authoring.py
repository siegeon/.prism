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
READS from ``oracle_spec`` (``OracleSpec``, ``_pytest_ids_from_task``); it
must never import anything that requires editing that module.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Optional

from prism_service.services.oracle_spec import OracleSpec, _pytest_ids_from_task


def validate_for_authoring(oracle: str = "", proof_type: str = "",
                           verify: Optional[list] = None,
                           likely_misfire: str = "") -> tuple:
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
        ``oracle`` non-empty, and ``verify[]`` yields zero runnable pytest
        node ids. Oracle-less tasks and manual-evidence proof_types
        (demo/browser/review/decision/artifact/metric/unset) never
        populate this — their honest browser/manual fallback is a
        legitimate spec, not an error.
    """
    oracle_s = str(oracle or "").strip()
    proof_type_l = str(proof_type or "").strip().lower()
    verify_l = list(verify or [])
    if not oracle_s:
        return None, []
    duck = SimpleNamespace(oracle=oracle_s, likely_misfire=likely_misfire or "",
                           proof_type=proof_type_l, verify=verify_l)
    spec = OracleSpec.from_task(duck)
    domain_errors: list = []
    if proof_type_l == "test" and not _pytest_ids_from_task(duck):
        domain_errors.append(
            "proof_type=test but verify[] contains no runnable pytest node "
            "ids — add e.g. "
            "services/prism-service/tests/unit/test_x.py::test_y to "
            "verify[] (a bare path with '::' or a pytest invocation)."
        )
    return spec.as_dict(), domain_errors
