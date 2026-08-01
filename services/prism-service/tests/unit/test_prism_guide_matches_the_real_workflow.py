"""The MCP surface and its guide must tell the truth (conductor task f1e7e228).

`prism_guide` is the first thing an agent reads here, and today it teaches the
WRONG loop: the curated text names `conductor_work` zero times while naming
`conductor_advance` 7 times and `conductor_gate` 6 times, presenting them as
"drive the per-task SDLC". The server's own connect-instructions say the
opposite (`prism_service/mcp/instructions.py:19-29`): `conductor_work` is the
single loop verb and advance/gate/workflow_state are admin/debug behind
`tool_profile=all`.

ANTI-VACUITY (the load-bearing design constraint, AC-2). `_version_banner()`
(`mcp/tools.py:1892`) prepends the 270k-char PRISM_VERSION_NOTES changelog into
the overview literal, so `assert "<tool>" in guide` passes vacuously for 10 of
the 28 interactive tools. Every assertion below therefore runs against:
  * the RENDERED guide, obtained through the REAL tool dispatcher
    (`handle_tool("prism_guide", ...)`) — never the `_GUIDE_SECTIONS` source
    literal and never a Python comment;
  * with the changelog banner stripped;
  * isolated to ONE section; and
  * parsed into enclosing BLOCKS, never a fixed character window around a
    match (an explanatory comment or a neighbouring bullet must not be able
    to satisfy an assertion about a different bullet).
"""

from __future__ import annotations

import asyncio
import os
import re
from functools import lru_cache
from pathlib import Path

import pytest

os.environ["PRISM_MCP_AUGMENT_NUDGES"] = "false"

# Workspace root: .../services/prism-service/tests/unit/<this file>
WORKSPACE_ROOT = Path(__file__).resolve().parents[4]

LEGACY_DRIVE_VERBS = (
    "conductor_advance",
    "conductor_gate",
    "workflow_advance",
    "workflow_state",
)

# Sections whose job is to teach a reader how to DRIVE a task. Each is
# asserted independently so an edit scoped to one site leaves the others red
# (AC-6: the roles section is GENERATED at mcp/tools.py:2280-2295 and sits
# OUTSIDE the _GUIDE_SECTIONS literal, so a literal-only edit misses it).
DRIVE_SECTIONS = ("tools", "workflow", "orchestration", "roles", "examples")


# ----------------------------------------------------------------------
# Helpers — rendered, changelog-stripped, section-isolated, block-parsed.
# ----------------------------------------------------------------------


@lru_cache(maxsize=None)
def _render(section: str | None = None) -> str:
    """The guide as a CONNECTED CLIENT receives it: through the real MCP tool
    dispatcher, not by poking the module-private renderer."""
    from prism_service.mcp.tools import handle_tool

    args = {"section": section} if section else {}
    result = asyncio.run(handle_tool("prism_guide", args, project_id="prism"))
    assert result, "prism_guide returned nothing through the dispatcher"
    return result[0].text


def _strip_changelog(text: str) -> str:
    """Remove the PRISM_VERSION_NOTES banner `_version_banner()` prepends."""
    from prism_service.mcp.tools import _version_banner

    return text.replace(_version_banner(), " ")


def _norm(text: str) -> str:
    """Lowercase + collapse whitespace so a line-wrapped phrase still matches."""
    return " ".join(text.split()).lower()


def _blocks(section_text: str) -> list[str]:
    """Split a section into logical markdown blocks (headings, bullets,
    numbered steps, paragraphs), each internally whitespace-collapsed.

    This is what "parse the enclosing block" means in AC-5/AC-6: an
    admin/debug label sitting in a DIFFERENT bullet cannot excuse a legacy
    verb, and no fixed character window is ever taken around a match.
    """
    blocks: list[list[str]] = []
    for raw in section_text.splitlines():
        line = raw.rstrip()
        starts_block = (
            not line.strip()
            or line.lstrip().startswith(("-", "*", "#", ">", "|", "```"))
            or re.match(r"^\s*\d+[.)]\s", line) is not None
        )
        if starts_block or not blocks:
            blocks.append([line])
        else:
            blocks[-1].append(line)
    return [b for b in (_norm("\n".join(x)) for x in blocks) if b]


@lru_cache(maxsize=None)
def _curated_full() -> str:
    """The whole rendered guide, changelog stripped, normalized."""
    return _norm(_strip_changelog(_render(None)))


def _section_names() -> list[str]:
    """Every section name the guide can render.

    `_GUIDE_SECTIONS` is touched here ONLY as a NAME ENUMERATOR — no assertion
    in this file ever reads its text (AC-2: assertions run on rendered output).
    A name that renders identically to the full guide is not a real section.
    """
    from prism_service.mcp.tools import _GUIDE_SECTIONS

    return list(_GUIDE_SECTIONS.keys())


@lru_cache(maxsize=None)
def _sections() -> dict[str, str]:
    """name -> the section's own RENDERED text (changelog-stripped, raw
    newlines preserved so `_blocks` can still see markdown structure)."""
    full = _curated_full()
    out: dict[str, str] = {}
    for name in _section_names():
        body = _strip_changelog(_render(name))
        assert _norm(body), f"section {name!r} renders empty"
        assert _norm(body) in full, (
            f"section {name!r} exists but never renders inside "
            f"prism_guide(None) — it is unreachable to every reader"
        )
        out[name] = body
    return out


def _all_blocks() -> list[str]:
    """Every block of every section — for discipline items that may live in
    whichever section the rewrite chooses, still asserted block-locally."""
    out: list[str] = []
    for body in _sections().values():
        out.extend(_blocks(body))
    return out


def _section(name: str) -> str:
    """One section, normalized, for whole-section substring checks."""
    return _norm(_sections()[name])


def _render_order() -> list[str]:
    """The ACTUAL order sections appear in the rendered guide (AC-10)."""
    full = _curated_full()
    secs = _sections()
    return sorted(secs, key=lambda n: full.index(_norm(secs[n])))


# ----------------------------------------------------------------------
# AC-2 — the helper itself is the first thing under test. If a future edit
# bypasses the changelog strip, EVERY other assertion in this file goes
# vacuous, so the guard has to fail loudly rather than silently pass.
# ----------------------------------------------------------------------


def test_curated_slice_actually_strips_the_changelog():
    from prism_service.__version__ import PRISM_VERSION_NOTES

    raw = _render(None)
    curated = _curated_full()

    assert len(raw) > 200_000, (
        "the raw render is expected to be dominated by PRISM_VERSION_NOTES; "
        f"got {len(raw)} chars — re-check _version_banner()"
    )
    assert len(curated) < 30_000, (
        f"curated guide is {len(curated)} chars: the changelog strip failed, "
        "so every assertion in this file would pass vacuously"
    )

    # Text that exists ONLY in the changelog must not survive the strip.
    # Derived from the live notes rather than hardcoded, so a version bump
    # can never quietly turn this guard into a no-op.
    changelog_only = _norm(PRISM_VERSION_NOTES)[:200]
    assert len(changelog_only) == 200
    assert changelog_only not in curated, (
        "changelog text leaked into the curated slice"
    )


def test_every_section_is_isolated_and_shorter_than_the_whole_guide():
    full = _curated_full()
    for name, body in _sections().items():
        norm = _norm(body)
        assert norm != full, f"section {name!r} is not isolated from the guide"
        assert len(norm) < len(full)


# ----------------------------------------------------------------------
# AC-3 — REGISTRY -> GUIDE. Every tool on the DEFAULT surface a normal
# session connects with must be documented in curated text (mcp/tools.py
# :1764-1781). A default-surface tool the guide never mentions is a hole.
# ----------------------------------------------------------------------


def test_every_interactive_tool_is_documented_in_curated_guide():
    from prism_service.mcp.tools import tool_names_for_profile

    curated = _curated_full()
    surface = sorted(tool_names_for_profile("interactive"))
    assert surface, "interactive profile resolved to an empty surface"

    missing = [name for name in surface if name not in curated]
    assert not missing, (
        f"{len(missing)} of {len(surface)} default-surface tools are absent "
        f"from the curated guide (they appear only inside the changelog): "
        f"{missing}"
    )


# ----------------------------------------------------------------------
# AC-4 — GUIDE -> REGISTRY. Every tool the guide tells you to call must
# exist. A guide naming a retired tool is a ghost instruction: the reader
# calls it and gets "Unknown tool — not registered on this MCP server".
# ----------------------------------------------------------------------

_CALL_RE = re.compile(r"`([a-z][a-z0-9_]{2,})\(")


def test_guide_names_no_ghost_tools():
    from prism_service.mcp.tools import TOOLS

    registered = {tool.name for tool in TOOLS}
    mentioned = set(_CALL_RE.findall(_curated_full()))
    assert mentioned, "no backticked tool calls found — the regex is broken"

    ghosts = sorted(n for n in mentioned if n not in registered)
    assert not ghosts, (
        f"the guide instructs the reader to call {len(ghosts)} name(s) that "
        f"resolve to no entry in TOOLS: {ghosts}"
    )


# ----------------------------------------------------------------------
# AC-5 / AC-6 — THE LOOP. `conductor_work` is the single drive verb
# (mcp/instructions.py:19-29). Asserted at EVERY drive-teaching site
# independently, including the GENERATED `roles` section spliced outside
# the _GUIDE_SECTIONS literal, so a literal-only edit still leaves red.
# ----------------------------------------------------------------------


@pytest.mark.parametrize("section", DRIVE_SECTIONS)
def test_drive_section_names_conductor_work(section):
    text = _section(section)
    assert "conductor_work" in text, (
        f"the {section!r} section teaches how to drive a task but never names "
        "conductor_work, the server's single loop verb"
    )


@pytest.mark.parametrize("section", DRIVE_SECTIONS)
def test_legacy_drive_verbs_are_labelled_admin_only(section):
    """A legacy verb may still be MENTIONED, but only inside a block that
    says it is admin/debug behind tool_profile=all. The check parses the
    ENCLOSING BLOCK, never a character window around the match, so a label
    in a neighbouring bullet cannot launder an unlabelled instruction."""
    offenders = []
    for block in _blocks(_sections()[section]):
        named = [v for v in LEGACY_DRIVE_VERBS if v in block]
        if not named:
            continue
        labelled = ("admin" in block or "debug" in block) and \
            "tool_profile=all" in block
        if not labelled:
            offenders.append((named, block[:160]))

    assert not offenders, (
        f"{len(offenders)} block(s) in the {section!r} section present a "
        f"superseded verb as a way to work, with no admin/debug + "
        f"tool_profile=all label: {offenders}"
    )


# ----------------------------------------------------------------------
# AC-7 — the WORKED EXAMPLES, not just a keyword hit somewhere. A guide
# that mentions conductor_work in prose while its example flows still show
# the hand-drive teaches the hand-drive (mcp/tools.py:2120-2165; the
# crash-recovery flow opens with `workflow_state()` at :2161).
# ----------------------------------------------------------------------


def test_examples_show_the_conductor_work_loop():
    ex = _section("examples")
    assert "conductor_work" in ex
    # The loop shape, not just the name: claim a job, do it, report an outcome.
    assert "outcome" in ex, "the example loop never reports an outcome"
    assert "proof" in ex, "the example loop never produces proof"


def test_crash_recovery_example_resumes_through_the_conductor():
    """Picking up after a crash is where a driver is most likely to
    hand-drive. Today that flow opens with `workflow_state()`."""
    blocks = _blocks(_sections()["examples"])
    recovery = [b for b in blocks if "crash" in b or "picking up" in b]
    assert recovery, "the examples section lost its crash-recovery flow"

    start = blocks.index(recovery[0])
    flow = blocks[start:start + 8]
    assert any("conductor_work" in b for b in flow), (
        "the crash-recovery flow never calls conductor_work — it teaches the "
        f"reader to resume by hand: {flow[:4]}"
    )


def test_implement_a_feature_example_drives_the_conductor():
    blocks = _blocks(_sections()["examples"])
    impl = [b for b in blocks if "implement a feature" in b]
    assert impl, "the examples section lost its 'implement a feature' flow"

    start = blocks.index(impl[0])
    flow = blocks[start:start + 10]
    assert any("conductor_work" in b for b in flow), (
        "the daily implement loop never mentions the conductor at all"
    )


# ----------------------------------------------------------------------
# AC-8 — THE EVIDENCE DISCIPLINE, one independent test per item so a
# partial rewrite fails loudly instead of once. These are the eight things
# a driver gets stuck on today and the guide never mentions.
# ----------------------------------------------------------------------


def test_guide_teaches_the_oracle():
    """Regression guard — already green: the oracle is tied to the gate's
    proof shape today, and the rewrite must not lose that."""
    hits = [b for b in _all_blocks() if "oracle" in b and "proof_type" in b]
    assert hits, (
        "the guide never ties the oracle to the proof_type the gate checks"
    )


def test_guide_teaches_likely_misfire():
    assert "likely_misfire" in _curated_full(), (
        "the guide never mentions likely_misfire, so no driver ever writes one"
    )


def test_guide_teaches_workspace_root_relative_verify():
    g = _curated_full()
    assert "task.verify" in g or "verify=" in g, "the guide never names task.verify"
    assert "workspace-root-relative" in g or "workspace root" in g, (
        "the guide never says task.verify must be workspace-root-relative — "
        "the exact mistake that parks red_gate with 'no tests ran'"
    )


def test_guide_teaches_the_tests_only_red_commit_anchor():
    g = _curated_full()
    assert "[task:" in g, "the guide never shows the [task:<id>] commit trailer"
    assert "tests-only" in g or "tests only" in g, (
        "the guide never says the red step lands a TESTS-ONLY commit — the "
        "commit the red machine seat anchors to"
    )


def test_guide_teaches_reading_gate_readiness_not_the_stale_snapshot():
    g = _curated_full()
    assert "/api/conductor/gate/readiness" in g, (
        "the guide never points at the live gate readiness endpoint"
    )
    assert "gate_reason" in g, (
        "the guide never warns that task.gate_reason is a stale snapshot"
    )


def test_guide_teaches_the_distinct_actor_rule():
    hits = [b for b in _all_blocks()
            if "distinct actor" in b or "distinct-actor" in b]
    assert hits, "the guide never states the distinct-actor gate rule"
    assert any("gate" in b for b in hits), (
        "distinct-actor is mentioned but never tied to the gate it governs"
    )


def test_guide_teaches_demo_and_review_green_gates_are_human_only():
    hits = [b for b in _all_blocks()
            if "proof_type" in b and ("demo" in b or "review" in b)]
    assert hits, "the guide never explains proof_type=demo / review"
    assert any("human" in b for b in hits), (
        "the guide never says a proof_type=demo/review green_gate is "
        "human-only by design, so a driver tries to game it"
    )


def test_guide_cites_evidence_into_the_prism_store_not_an_external_host():
    hits = [b for b in _all_blocks() if "evidence" in b and "/api/tasks/" in b]
    assert hits, (
        "the guide never shows where evidence goes: it must live under "
        "data_dir/evidence/<task_id>/ and be cited as "
        "![](/api/tasks/<id>/evidence/<file>)"
    )
    assert any("data_dir/evidence/" in b for b in hits), (
        "the guide cites an evidence URL but never names the on-disk store"
    )
    assert any("![](" in b for b in hits), (
        "the guide never shows the markdown citation form completion_proof needs"
    )

    external = ("claude.ai", "imgur", "gist.github", "pastebin", "s3.amazonaws")
    leaked = [host for host in external for b in hits if host in b]
    assert not leaked, f"evidence guidance points at an external host: {leaked}"


# ----------------------------------------------------------------------
# AC-9 — container-era language is gone. v6.0.0 made native (pip/pipx) THE
# distribution; the overview still tells the reader data lives in a Docker
# volume (mcp/tools.py:1921).
# ----------------------------------------------------------------------


def test_overview_describes_the_native_data_dir_not_a_container_volume():
    overview = _section("overview")
    for stale in ("/data volume", "inside the container"):
        assert stale not in overview, (
            f"the overview still describes the container era: {stale!r}"
        )
    assert "data dir" in overview or "data_dir" in overview, (
        "the overview no longer says where data actually lives"
    )


# ----------------------------------------------------------------------
# AC-10 — the guide's own section list must match what it renders. Today
# the self-description (mcp/tools.py:2037-2038) omits `roles`, which the
# render order at mcp/tools.py:2299-2300 does include: a reader asking for
# a section they were never told about is the guide lying about itself.
# ----------------------------------------------------------------------


def test_guide_self_description_lists_every_section_it_renders():
    tools_section = _section("tools")
    hits = [b for b in _blocks(_sections()["tools"]) if "prism_guide(" in b]
    assert hits, "the tools section no longer documents prism_guide itself"

    advertised = " ".join(hits)
    missing = [n for n in _render_order() if n not in advertised]
    assert not missing, (
        f"prism_guide renders {_render_order()} but its own section list "
        f"never advertises {missing} — ask for it and you get the whole guide"
    )
    assert "roles" in tools_section


# ----------------------------------------------------------------------
# AC-17 — TRUER, not longer. The measure is whether a naive driver
# succeeds; a guide nobody finishes is worse than the one it replaced.
# Baseline measured on this branch's base: 21,025 chars of `_curated_full()`
# (changelog stripped AND whitespace-normalized) out of a 291,792-char raw
# render. The plan's 21,457 figure is the same text un-normalized.
# ----------------------------------------------------------------------

CURATED_BASELINE_CHARS = 21_025
CURATED_HARD_CAP_CHARS = 24_000


def test_curated_guide_gets_truer_not_longer():
    size = len(_curated_full())
    assert size <= CURATED_HARD_CAP_CHARS, (
        f"curated guide is {size} chars against a {CURATED_BASELINE_CHARS}-char "
        f"baseline and a {CURATED_HARD_CAP_CHARS}-char cap: the rewrite grew "
        "the guide instead of correcting it"
    )


# ----------------------------------------------------------------------
# AC-11 — "is this tool still used?" must become answerable FROM DATA.
# `call_tool` (mcp/server.py:122-179) authorizes, profile-checks, team-
# scopes, dispatches — and writes nothing. Driven through the SDK's real
# CallToolRequest handler and read back over a SEPARATE HTTP call, so an
# in-memory counter or a never-mounted router cannot pass this.
# ----------------------------------------------------------------------


def _dispatch(name, project_id, profile="interactive", arguments=None):
    from mcp import types

    from prism_service.mcp.request_context import (
        PrismRequestContext,
        use_request_context,
    )
    from prism_service.mcp.server import server

    handler = server.request_handlers[types.CallToolRequest]
    req = types.CallToolRequest(
        method="tools/call",
        params=types.CallToolRequestParams(name=name, arguments=arguments or {}),
    )
    ctx = PrismRequestContext(project_id=project_id, tool_profile=profile)
    with use_request_context(ctx):
        return asyncio.run(handler(req)).root


def _tool_usage_client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import api_router

    app = FastAPI()
    app.include_router(api_router)
    return TestClient(app)


def _read_usage(project: str) -> list[dict]:
    resp = _tool_usage_client().get("/api/tool-usage", params={"project": project})
    assert resp.status_code == 200, (
        f"GET /api/tool-usage returned {resp.status_code}: there is no read "
        "surface for per-tool telemetry, so 'is this tool used?' stays "
        "unanswerable from data"
    )
    payload = resp.json()
    return payload.get("rows", payload) if isinstance(payload, dict) else payload


def test_tool_usage_route_is_registered_on_the_production_api_router():
    """A defined-but-unmounted router 404s in production. Read the mounted
    paths off the OpenAPI schema (version-agnostic)."""
    client = _tool_usage_client()
    paths = set(client.app.openapi()["paths"])
    assert any(p.startswith("/api/tool-usage") for p in paths), (
        "no /api/tool-usage read surface is mounted on the production "
        f"api_router (mounted sample: {sorted(paths)[:8]})"
    )


def test_dispatching_a_tool_records_per_tool_telemetry(tmp_path, monkeypatch):
    from prism_service import config
    from prism_service.project_context import release_project

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    project = "tool-usage-probe-f1e7e228"
    release_project(project)

    ok_result = _dispatch("prism_status", project_id=project)
    assert ok_result is not None

    rows = _read_usage(project)
    hits = [r for r in rows if r.get("tool") == "prism_status"]
    assert hits, (
        "dispatching prism_status through the real MCP handler recorded no "
        f"telemetry row; read surface returned {rows!r}"
    )
    row = hits[0]
    assert row.get("project") == project
    assert row.get("tool_profile") == "interactive"
    assert row.get("ok") in (1, True)
    assert row.get("ts"), "telemetry row carries no timestamp"


def test_a_rejected_tool_call_is_recorded_as_an_error(tmp_path, monkeypatch):
    """Rejections are the most interesting signal: a tool a session keeps
    reaching for on the wrong profile is evidence FOR keeping it."""
    from prism_service import config
    from prism_service.project_context import release_project

    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path)
    project = "tool-usage-reject-f1e7e228"
    release_project(project)

    _dispatch("brain_index_doc", project_id=project,
              arguments={"path": "x", "content": "y"})

    rows = _read_usage(project)
    hits = [r for r in rows if r.get("tool") == "brain_index_doc"]
    assert hits, "a profile-rejected call left no telemetry row at all"
    assert hits[0].get("ok") in (0, False), (
        "a profile-rejected call was recorded as a success"
    )


# ----------------------------------------------------------------------
# AC-12 / AC-13 / AC-14 — the owner-readable ledger. The guide can only be
# honest about which tools exist once somebody DECIDES which survive; the
# ledger is that decision, committed and reviewable.
# ----------------------------------------------------------------------

LEDGER_PATH = WORKSPACE_ROOT / "docs" / "mcp-tool-usage-ledger.md"
DECISIONS = ("KEEP", "DEMOTE", "RETIRE")


def _ledger_rows() -> dict[str, str]:
    """tool name -> its ledger row, parsed from the markdown table."""
    assert LEDGER_PATH.exists(), (
        f"no committed tool-usage ledger at {LEDGER_PATH}"
    )
    rows: dict[str, str] = {}
    for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        name = cells[0].strip("`") if cells else ""
        if re.fullmatch(r"[a-z][a-z0-9_]+", name):
            rows[name] = line.lower()
    return rows


def test_ledger_carries_one_row_per_registered_tool():
    from prism_service.mcp.tools import TOOLS

    registered = {tool.name for tool in TOOLS}
    rows = _ledger_rows()
    missing = sorted(registered - set(rows))
    assert not missing, (
        f"{len(missing)} of {len(registered)} registered tools have no ledger "
        f"row, so nobody decided anything about them: {missing}"
    )


def test_every_ledger_row_carries_a_decision_and_evidence():
    undecided, unevidenced = [], []
    for name, row in _ledger_rows().items():
        if not any(d.lower() in row for d in DECISIONS):
            undecided.append(name)
        # Evidence = at least one cited reference, or the explicit
        # "no evidence" marker. Silence must never read as proof of death.
        elif row.count("`") < 4 and "no evidence" not in row:
            unevidenced.append(name)
    assert not undecided, f"ledger rows with no KEEP/DEMOTE/RETIRE: {undecided}"
    assert not unevidenced, (
        "ledger rows citing neither a reference nor an explicit "
        f"'no evidence' marker: {unevidenced}"
    )


def test_ledger_is_a_decision_not_a_shrug():
    """R13/AC-14: 73 rows of KEEP is the ask failing with every test green."""
    rows = _ledger_rows()
    shrinking = [n for n, r in rows.items()
                 if "demote" in r or "retire" in r]
    assert shrinking, (
        f"all {len(rows)} ledger rows say KEEP — the surface never shrinks "
        "and the ledger is a 73-row list of 'unsure'"
    )


def test_nothing_reachable_is_marked_retire():
    """R12/AC-13: absence of a reference is NO EVIDENCE, never proof of
    death. Retiring an MCP verb breaks every already-connected client."""
    reachable_roots = [
        WORKSPACE_ROOT / "plugins" / "prism-devtools",
        WORKSPACE_ROOT / ".mcp.json",
    ]
    corpus = ""
    for root in reachable_roots:
        if root.is_file():
            corpus += root.read_text(encoding="utf-8", errors="ignore")
        elif root.is_dir():
            for path in root.rglob("*"):
                if path.is_file() and path.suffix in (".py", ".md", ".json"):
                    corpus += path.read_text(encoding="utf-8", errors="ignore")

    violations = [
        name for name, row in _ledger_rows().items()
        if "retire" in row and name in corpus
    ]
    assert not violations, (
        "these tools are marked RETIRE while still reachable from an "
        f"installed hook script / plugin spec / shipped .mcp.json: {violations}"
    )


def test_understand_rows_check_the_named_consumer_before_retiring():
    """memory mx-0103ae names the architecture-analyzer -> green_gate
    conformance-note consumer as the thing to check first."""
    unchecked = [
        name for name, row in _ledger_rows().items()
        if name.startswith("understand_") and "retire" in row
        and "architecture-analyzer" not in row and "conformance" not in row
    ]
    assert not unchecked, (
        f"understand_* rows marked RETIRE without citing the "
        f"architecture-analyzer consumer check: {unchecked}"
    )


# ----------------------------------------------------------------------
# AC-16 — REGRESSION GUARD. README.md:157 was already corrected on this
# branch's base (commit 29694e1) to label the superseded verbs admin/debug.
# Nothing to fix; this pins it so the contradiction cannot come back.
# ----------------------------------------------------------------------


def test_readme_does_not_advertise_superseded_verbs_as_drive_verbs():
    readme = (WORKSPACE_ROOT / "README.md").read_text(encoding="utf-8")
    offenders = []
    for line in readme.splitlines():
        low = line.lower()
        if not any(v in low for v in ("conductor_advance", "conductor_gate")):
            continue
        labelled = ("admin" in low or "debug" in low) and "tool_profile=all" in low
        if not labelled:
            offenders.append(line.strip()[:140])
    assert not offenders, (
        "README advertises a superseded verb without the admin/debug + "
        f"tool_profile=all label: {offenders}"
    )
