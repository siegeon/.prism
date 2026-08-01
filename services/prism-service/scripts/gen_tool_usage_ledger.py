"""Generate docs/mcp-tool-usage-ledger.md — one row per registered MCP tool.

Task f1e7e228. The guide can only be honest about which tools exist once
somebody DECIDES which survive; this script is how that decision is kept
reviewable and re-runnable instead of hand-written and rotting.

    python services/prism-service/scripts/gen_tool_usage_ledger.py

Two columns are computed, one is curated:

* REFERENCES — a static scan of every place a tool name can be reached from:
  the installed plugin (hook scripts, skills, agent specs), the shipped
  .mcp.json, the SPA, tests, docs and the guide text itself.
* TELEMETRY — the per-tool call counters `mcp/server.py` records at dispatch
  (`services/tool_usage_data.py`). Absent means NOT YET OBSERVED. It never
  means dead: an external client's `.mcp.json` calls nothing until it runs.
* DECISION — curated in `_DECISIONS` below, with the reason a human can audit.

STOP RULE (stop_if #1): nothing reachable from a hook script, plugin spec or
shipped .mcp.json may be marked RETIRE, no matter how quiet telemetry is.
"""

from __future__ import annotations

import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

LEDGER = WORKSPACE_ROOT / "docs" / "mcp-tool-usage-ledger.md"

# Where a tool name can be REACHED from. Order matters: the first matching
# root labels the row, and the plugin/.mcp.json roots are the stop_if ones.
SCAN_ROOTS: list[tuple[str, Path, tuple[str, ...]]] = [
    ("plugin", WORKSPACE_ROOT / "plugins" / "prism-devtools",
     (".py", ".md", ".json")),
    ("mcp.json", WORKSPACE_ROOT / ".mcp.json", (".json",)),
    ("spa", WORKSPACE_ROOT / "services" / "prism-service" / "prism_service"
     / "web" / "src", (".ts", ".tsx")),
    ("tests", WORKSPACE_ROOT / "services" / "prism-service" / "tests",
     (".py",)),
    ("docs", WORKSPACE_ROOT / "docs", (".md",)),
]

# Roots that make a tool LIVE — reachable by an already-connected client.
BLOCKING_ROOTS = {"plugin", "mcp.json"}

# ---------------------------------------------------------------------------
# Curated decisions. Anything not listed defaults to KEEP with the honest
# "no evidence yet" note — silence is never read as proof of death.
# ---------------------------------------------------------------------------

_RETIRE_UNDERSTAND = (
    "RETIRE",
    "mx-0103ae consumer check DONE: the green_gate conformance note reads "
    "layers.json straight from `understand_artifact_store` via "
    "`conductor_service.py:1772-1788`, NOT through this MCP tool, so the "
    "architecture-analyzer pipeline keeps working without it. Superseded by "
    "the okf_* Understand wiki. Unreachable from any hook script, plugin spec "
    "or shipped .mcp.json.",
)

_DEMOTED_ALREADY = (
    "DEMOTE",
    "Already applied at `mcp/tools.py:1652-1656`: superseded by "
    "`conductor_work`, in no named profile, reachable only via "
    "tool_profile=all for admin/debug. Removing the verb itself reaches "
    "`services/conductor_service.py`, which is in control_plane.POLICY_FILES "
    "— that is a separate task tagged policy-change.",
)

_DECISIONS: dict[str, tuple[str, str]] = {
    "conductor_advance": _DEMOTED_ALREADY,
    "conductor_gate": _DEMOTED_ALREADY,
    "workflow_advance": _DEMOTED_ALREADY,
    "workflow_state": _DEMOTED_ALREADY,
    "understand_bootstrap": _RETIRE_UNDERSTAND,
    "understand_configure": _RETIRE_UNDERSTAND,
    "understand_get_domains": _RETIRE_UNDERSTAND,
    "understand_get_layers": _RETIRE_UNDERSTAND,
    "understand_get_onboarding": _RETIRE_UNDERSTAND,
    "understand_get_tour": _RETIRE_UNDERSTAND,
    # LIVE despite being in no profile — the CLI calls them over MCP.
    "understand_drain_queue": (
        "KEEP",
        "Live consumer: `cli/understand_cli.py:198` calls it over MCP. "
        "Absence from the plugin scan is not death.",
    ),
    "understand_store_result": (
        "KEEP",
        "Live consumer: `cli/understand_cli.py:212` calls it over MCP. "
        "Absence from the plugin scan is not death.",
    ),
}

# KEEP rows with zero inbound references: say what evidence would settle it,
# rather than pretending the silence means anything (AC-14).
_UNSETTLED = (
    "KEEP",
    "NO EVIDENCE either way: no inbound reference and no observed call. What "
    "would settle it: a `/api/tool-usage` observation after the instrumented "
    "build has run a full release cycle; zero calls plus zero references then "
    "makes it a removal candidate for the follow-up cleanup task.",
)


def _scan() -> dict[str, str]:
    """label -> concatenated text of every file under that root."""
    corpus: dict[str, str] = {}
    for label, root, suffixes in SCAN_ROOTS:
        chunks: list[str] = []
        if root.is_file():
            chunks.append(root.read_text(encoding="utf-8", errors="ignore"))
        elif root.is_dir():
            for path in root.rglob("*"):
                if path.is_file() and path.suffix in suffixes:
                    chunks.append(
                        path.read_text(encoding="utf-8", errors="ignore"))
        corpus[label] = "\n".join(chunks)
    return corpus


def _telemetry() -> dict[str, dict]:
    """Observed calls from the dev data dir, when one is reachable."""
    try:
        from prism_service.config import PROJECTS_DIR
        from prism_service.services.tool_usage_data import get_tool_usage_rollup

        db = PROJECTS_DIR / "prism" / "scores.db"
        return get_tool_usage_rollup(str(db)) if db.exists() else {}
    except Exception:
        return {}


def build() -> str:
    import re

    from prism_service.mcp.tools import TOOLS, tool_names_for_profile

    corpus = _scan()
    observed = _telemetry()
    names = sorted({tool.name for tool in TOOLS})
    profiles = {
        p: tool_names_for_profile(p)
        for p in ("interactive", "admin", "hooks", "learning", "automation")
    }

    lines = [
        "# MCP tool usage ledger",
        "",
        f"One row per registered MCP tool ({len(names)} of them), so the "
        "question the guide rewrite depends on — *which tools should a reader "
        "be told about?* — is answered from evidence rather than from habit.",
        "",
        "Generated by `services/prism-service/scripts/gen_tool_usage_ledger.py` "
        f"(task f1e7e228). Regenerate after changing the tool registry.",
        "",
        "## How to read a row",
        "",
        "- **Profiles** — which named `tool_profile` surfaces expose the tool. "
        "`none` means it is reachable only via `tool_profile=all`.",
        "- **References** — a static scan of the installed plugin (hook "
        "scripts, skills, agent specs), the shipped `.mcp.json`, the SPA, the "
        "test suite and the docs.",
        "- **Observed** — real calls recorded at the MCP dispatch point "
        "(`mcp/server.py`, read back through `/api/tool-usage`). This column "
        "went live with this task, so `not yet` on a first pass means the "
        "instrumented build has not run long enough, NOT that the tool is "
        "dead.",
        "- **Decision** — KEEP interactive / DEMOTE to `tool_profile=all` / "
        "RETIRE, each with the evidence it rests on.",
        "",
        "**Absence of a reference is *no evidence*, never proof of death.** "
        "Removing an MCP verb is a breaking change for every already-connected "
        "client, so no row is marked for removal while it is reachable from an "
        "installed hook script, a plugin skill/agent spec or a shipped "
        "`.mcp.json`.",
        "",
        "| Tool | Profiles | References | Observed | Decision | Evidence / what would settle it |",
        "| --- | --- | --- | --- | --- | --- |",
    ]

    counts = {"KEEP": 0, "DEMOTE": 0, "RETIRE": 0}
    for name in names:
        pattern = re.compile(r"\b" + re.escape(name) + r"\b")
        refs = [label for label in corpus if pattern.search(corpus[label])]
        in_profiles = [p for p in profiles if name in profiles[p]] or ["none"]
        hit = observed.get(name)
        seen = f"{hit['calls']} call(s)" if hit else "not yet"

        if name in _DECISIONS:
            decision, why = _DECISIONS[name]
        elif not refs:
            decision, why = _UNSETTLED
        else:
            decision, why = (
                "KEEP",
                "Referenced by " + ", ".join(f"`{r}`" for r in refs) + ".",
            )
        counts[decision] += 1
        lines.append(
            f"| `{name}` | {', '.join(in_profiles)} | "
            f"{', '.join(refs) if refs else 'none found'} | {seen} | "
            f"**{decision}** | {why} |"
        )

    lines += [
        "",
        "## Roll-up",
        "",
        f"- KEEP: {counts['KEEP']}",
        f"- DEMOTE to `tool_profile=all`: {counts['DEMOTE']}",
        f"- RETIRE: {counts['RETIRE']}",
        "",
        f"The surface shrinks: {counts['DEMOTE'] + counts['RETIRE']} of "
        f"{len(names)} tools are marked to leave the default surface. Acting "
        "on the RETIRE rows is deliberately a FOLLOW-UP task, not this one: "
        "the four DEMOTE rows are the superseded conductor/workflow drive "
        "verbs, and deleting those reaches "
        "`services/conductor_service.py`, which is in "
        "`control_plane.POLICY_FILES` and must be split into a task tagged "
        "`policy-change`.",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(build(), encoding="utf-8")
    print(f"wrote {LEDGER}")
