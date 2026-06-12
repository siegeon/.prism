"""RED scaffold — agent-run telemetry must travel a SANDBOX-REAL channel
(task 0b34b6f7).

implement.js postAgentRun() POSTs via global `fetch` — which does NOT
exist in the Workflow script sandbox. Every emit throws
'fetch is not defined' (swallowed non-fatally at implement.js:146-148),
so GET /api/agent-runs returns zero rows for every real drive: the
agent_runs spine is dead in production while the source-substring tests
in test_implement_agent_run_emitter.py stay green (false-green).

These tests pin the ACCEPTANCE channel, not absence-of-error:

  * no workflow script may call `fetch(` at all — the sandbox has no
    fetch/Node net API, and a guarded fetch still lands zero rows;
  * a channel that EXISTS in the sandbox must carry the row: either
    (A) a workflow step contract instructs a Bash `curl` POST to
    /api/agent-runs/ingest, or (B) prism_service derives agent_runs
    rows server-side from conductor calls it already receives;
  * whichever channel exists must carry the agent_runs PK fields
    (run_id, agent_id, step) plus session_id, so rows actually LAND
    and GET /api/agent-runs filters can return this run's step agents.

ALL FAIL today: implement.js:141 still calls `await fetch(...)`, no
curl contract exists in any workflow prompt, and no prism_service
module outside the HTTP ingest route writes agent_runs rows.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_REPO = _SERVICE_ROOT.parent.parent
_WORKFLOWS = _REPO / ".claude" / "workflows"
_PKG = _SERVICE_ROOT / "prism_service"

# Modules ALLOWED to reference the write seam without counting as a
# server-side derivation channel: the pure data helper (definition
# site), the HTTP route the dead fetch targeted, and changelog prose.
_NOT_DERIVATION = {
    "api/agent_runs.py",
    "services/agent_runs_data.py",
    "__version__.py",
}

# A real invocation of the (nonexistent) sandbox global `fetch` —
# not a mention inside a word like `prefetch` or a property access.
_FETCH_CALL = re.compile(r"(?<![\w.$])fetch\s*\(")


def _workflow_sources() -> dict[str, str]:
    srcs = {p.name: p.read_text(encoding="utf-8")
            for p in sorted(_WORKFLOWS.glob("*.js"))}
    assert srcs, f"no workflow scripts found under {_WORKFLOWS}"
    return srcs


def _curl_ingest_window(src: str) -> str | None:
    """Text window of a Bash-curl instruction targeting the ingest
    route (URL + ~800 chars of trailing payload), or None."""
    m = re.search(r"curl[^\0]{0,800}?/api/agent-runs/ingest", src)
    if not m:
        return None
    return src[m.start():m.end() + 800]


def _derivation_modules() -> list[Path]:
    """prism_service modules OUTSIDE the HTTP route + pure data helper
    that write agent_runs rows — the server-side derivation seam."""
    found = []
    for p in sorted(_PKG.rglob("*.py")):
        rel = p.relative_to(_PKG).as_posix()
        if rel in _NOT_DERIVATION:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if "upsert_agent_run" in text or "INSERT INTO agent_runs" in text:
            found.append(p)
    return found


def test_workflow_scripts_never_call_fetch():
    """The Workflow sandbox has no global fetch (and no Node net API).
    Any fetch() call throws 'fetch is not defined' on every drive —
    the acceptance criterion is ZERO such lines in the drive log, which
    is only structurally guaranteed when no script calls fetch at all.
    A try/catch-guarded fetch is explicitly NOT a fix: it silences the
    log line while GET /api/agent-runs still returns {"rows": []}."""
    offenders = []
    for name, src in _workflow_sources().items():
        for m in _FETCH_CALL.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            offenders.append(f"{name}:{line}")
    assert not offenders, (
        "workflow scripts call fetch(), which does not exist in the "
        "Workflow sandbox — every drive logs 'fetch is not defined' "
        f"and zero telemetry rows land: {offenders}"
    )


def test_a_sandbox_real_telemetry_channel_exists():
    """The row must travel a channel that EXISTS where it fires:
    (A) step agents POST via Bash curl per their step contract, or
    (B) the daemon derives agent_runs rows server-side from conductor
    calls it already receives. Workflow-sandbox HTTP is impossible."""
    channel_a = any(
        _curl_ingest_window(s) for s in _workflow_sources().values())
    channel_b = _derivation_modules()
    assert channel_a or channel_b, (
        "no sandbox-real telemetry channel: no workflow step contract "
        "instructs a Bash curl POST to /api/agent-runs/ingest, and no "
        "prism_service module outside the HTTP ingest route derives "
        "agent_runs rows server-side — GET /api/agent-runs stays "
        "{'rows': []} for every real drive"
    )


def test_channel_carries_pk_fields_so_rows_land():
    """Rows LANDING — not absence-of-error — is the receipt. Whichever
    channel exists must carry the agent_runs PK (run_id, agent_id,
    step) plus session_id, or the ingest upsert cannot key the row and
    GET /api/agent-runs?session_id=... cannot return this run's
    step agents."""
    required = ("run_id", "agent_id", "step", "session_id")
    windows = [w for w in (_curl_ingest_window(s)
                           for s in _workflow_sources().values()) if w]
    derivers = _derivation_modules()
    if windows:
        missing = [f for f in required
                   if not any(f in w for w in windows)]
        assert not missing, (
            f"curl ingest contract drops required fields {missing} — "
            "the upsert cannot key the row; it will not land correctly"
        )
    elif derivers:
        blob = "\n".join(p.read_text(encoding="utf-8", errors="ignore")
                         for p in derivers)
        missing = [f for f in required if f not in blob]
        assert not missing, (
            f"server-side derivation drops required fields {missing} "
            f"(modules: {[str(p) for p in derivers]})"
        )
    else:
        raise AssertionError(
            "no telemetry channel exists at all — implement.js still "
            "fetch()es into a sandbox that has no fetch "
            "(implement.js:141), so no row ever reaches "
            "/api/agent-runs/ingest and no rows land"
        )
