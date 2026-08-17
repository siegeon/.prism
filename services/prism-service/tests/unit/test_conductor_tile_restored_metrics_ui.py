"""Pins the FOUR tile surfaces deleted by c9166c4 ("tile replicates the
e825e00a prototype", v6.8.37) and never restored: SlicesBar (epic hero line),
EtaCountdownBar (draining MM:SS bar), the TileHero 2x2 MetricCell grid, and
the session_quiet_s prop into <TokenTurns>. Task ac91be51.

RED-first: every assertion below fails against the CURRENT ConductorPage.tsx
(none of MetricCell/EtaCountdownBar/SlicesBar exist; the TokenTurns call site
omits session_quiet_s; the TileHero doc comment claims a metric grid that
isn't rendered). They must all go GREEN once the restore lands, with no
change to this file.

Convention (repo lesson, see test_gate_banner_refreshes_on_gate_arrival.py):
source-reading assertions are matched by BRACE DEPTH, never a fixed character
window or a bare substring near a comment - a comment above an element has
false-satisfied window-sliced assertions three separate times in this repo.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest  # noqa: F401  (registers this module with pytest)

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent  # tests/unit -> tests -> prism-service
_WEB_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_CONDUCTOR_PAGE = _WEB_SRC / "pages" / "ConductorPage.tsx"
_TOKEN_TURNS = _WEB_SRC / "components" / "conductor" / "TokenTurns.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _page() -> str:
    assert _CONDUCTOR_PAGE.exists(), f"missing {_CONDUCTOR_PAGE}"
    return _read(_CONDUCTOR_PAGE)


def _function_body(src: str, name: str) -> str:
    """Full body of `function name(...) { ... }`, matched by PAREN depth over
    the signature then BRACE depth over the body - never a fixed window, so a
    same-named mention elsewhere (comment, other component) can't satisfy an
    assertion meant for THIS function."""
    sig = f"function {name}("
    if sig not in src:
        raise AssertionError(f"no `{sig}` in source - component not defined")
    start = src.index(sig)
    i = start + len(sig) - 1
    depth = 0
    while i < len(src):
        ch = src[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    else:
        raise AssertionError(f"unbalanced parens in `{name}` signature")
    body_start = src.index("{", i)
    depth = 0
    j = body_start
    while j < len(src):
        ch = src[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[body_start:j + 1]
        j += 1
    raise AssertionError(f"unbalanced braces in `{name}` body")


def _extract_tag(src: str, tag: str, after: int = 0) -> tuple[str, int]:
    """(text, start_idx) of the full opening tag `<Tag ...>`/`.../>`, matched
    by BRACE depth over `{...}` prop expressions so a `>` inside a prop
    expression cannot truncate the match. Raises if the tag is not mounted
    anywhere (a component that only COMPILES but is never used must fail
    here, per the ticket's stop_if)."""
    marker = f"<{tag}"
    if marker not in src[after:]:
        raise AssertionError(f"{marker} is never mounted in the source")
    idx = src.index(marker, after)
    depth = 0
    i = idx
    while i < len(src):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == ">" and depth == 0:
            return src[idx:i + 1], idx
        i += 1
    raise AssertionError(f"unterminated <{tag} ...> tag")


def _enclosing_brace_expr(src: str, idx: int) -> str:
    """The `{...}` JSX expression ENCLOSING position `idx`, found by scanning
    backward for the nearest `{` whose forward brace-depth match closes AFTER
    `idx` - the real guard condition wrapping a mounted tag, never a fixed
    window or a comment above it (repeat false-satisfy failure mode in this
    repo)."""
    i = idx - 1
    close_depth = 0
    while i >= 0:
        ch = src[i]
        if ch == "}":
            close_depth += 1
        elif ch == "{":
            if close_depth == 0:
                depth = 0
                j = i
                while j < len(src):
                    c2 = src[j]
                    if c2 == "{":
                        depth += 1
                    elif c2 == "}":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if j < len(src) and j > idx:
                    return src[i:j + 1]
            else:
                close_depth -= 1
        i -= 1
    raise AssertionError("no enclosing {...} JSX expression found")


def _tag_count(src: str, tag: str) -> int:
    return len(re.findall(rf"<{re.escape(tag)}\b", src))


def _comment_block(src: str, first_line_marker: str) -> str:
    """The contiguous `//` comment block that STARTS with
    `first_line_marker` - located by its own stable opening text (the
    `// Name -` convention this file already uses for every component), not
    by proximity to the function signature: TileHero's doc sits above
    SDLC_STEPS, several declarations before `function TileHero(` itself."""
    idx = src.index(first_line_marker)
    line_start = src.rfind("\n", 0, idx) + 1
    out: list[str] = []
    for line in src[line_start:].splitlines():
        s = line.strip()
        if s.startswith("//"):
            out.append(s)
        else:
            break
    assert out, f"no comment block starting with {first_line_marker!r}"
    return "\n".join(out)


# ---------------------------------------------------------------------------
# AC-1 - TileHero: 2x2 grid of 4 MetricCell instances beside the ring
# ---------------------------------------------------------------------------

def test_tilehero_renders_four_metric_cells_beside_the_ring():
    src = _page()
    body = _function_body(src, "TileHero")
    n = _tag_count(body, "MetricCell")
    assert n == 4, (
        "TileHero must mount exactly 4 <MetricCell> instances (Current phase "
        f"/ Time left / Throughput / Idle) beside the ring - found {n}"
    )
    for label in ("Current phase", "Time left", "Throughput", "Idle"):
        assert label in body, f"MetricCell label {label!r} missing from TileHero"
    assert "grid-cols-2" in body, "the 4 cells must lay out as a 2x2 grid"
    assert "strokeDashoffset" in body, "the grid must sit beside the completion ring"


def test_tilehero_metric_values_source_real_task_fields():
    src = _page()
    body = _function_body(src, "TileHero")
    assert "task.phase_progress" in body, (
        "Time left / Throughput must be sourced from task.phase_progress, not fabricated"
    )
    assert "task.activity" in body, (
        "Idle must be sourced from task.activity (task_motion_s), not fabricated"
    )


def test_metric_grid_uses_honest_placeholder_when_no_data():
    src = _page()
    body = _function_body(src, "TileHero")
    assert "—" in body, (
        "Time left / Throughput / Idle must fall back to the honest em-dash "
        "placeholder, never a frozen fabricated value on a paused tile (stop_if)"
    )


def test_metric_cell_component_renders_label_and_value_props():
    src = _page()
    body = _function_body(src, "MetricCell")
    assert "{label}" in body and "{value}" in body, (
        "MetricCell must render its label/value PROPS, not a hardcoded string"
    )


# ---------------------------------------------------------------------------
# AC-2 - EtaCountdownBar: drains MM:SS while working, eta_s>5 && eta_total_s>0
# ---------------------------------------------------------------------------

def test_eta_countdown_bar_is_mounted_and_gated_on_real_eta_fields():
    src = _page()
    _tag_text, idx = _extract_tag(src, "EtaCountdownBar")
    guard = _enclosing_brace_expr(src, idx)
    assert "eta_s" in guard and re.search(r">\s*5\b", guard), (
        f"EtaCountdownBar's mount guard must require eta_s > 5 - guard={guard!r}"
    )
    assert "eta_total_s" in guard and re.search(r">\s*0\b", guard), (
        f"EtaCountdownBar's mount guard must require eta_total_s > 0 - guard={guard!r}"
    )
    assert "actWorking" in guard or "actState" in guard, (
        "EtaCountdownBar must be gated on the tile's honest activity state so "
        "it is ABSENT (not frozen) when paused/awaiting_gate/done (stop_if)"
    )


def test_eta_countdown_bar_drains_mmss_on_a_tick():
    src = _page()
    body = _function_body(src, "EtaCountdownBar")
    assert "setInterval" in body, (
        "the bar must tick down locally (setInterval), not render a static number"
    )
    assert re.search(r"/\s*60", body) and re.search(r"%\s*60", body), (
        "the countdown must format MM:SS (minutes = n/60, seconds = n%60)"
    )


def test_eta_countdown_bar_mounts_at_the_tile_bottom():
    src = _page()
    tile_body = _function_body(src, "TaskTile")
    assert "<EtaCountdownBar" in tile_body, (
        "EtaCountdownBar must mount inside TaskTile's own returned tree"
    )
    idx_eta = tile_body.index("<EtaCountdownBar")
    for earlier_tag in ("<TileHero", "<TokenTurns"):
        if earlier_tag in tile_body:
            assert tile_body.index(earlier_tag) < idx_eta, (
                f"{earlier_tag} must precede <EtaCountdownBar> - it mounts at the tile BOTTOM"
            )


# ---------------------------------------------------------------------------
# AC-3 - SlicesBar: epic hero line, present only when task.subtasks non-empty
# ---------------------------------------------------------------------------

def test_slicesbar_mounted_and_gated_on_real_subtasks():
    src = _page()
    tag_text, idx = _extract_tag(src, "SlicesBar")
    assert "task.subtasks" in tag_text, (
        "SlicesBar must be fed task.subtasks (AC-6: genuinely consumed, not dead type residue)"
    )
    guard = _enclosing_brace_expr(src, idx)
    assert "subtasks" in guard and ".length" in guard, (
        f"SlicesBar's mount guard must check task.subtasks?.length so a leaf "
        f"task never renders it - guard={guard!r}"
    )


def test_slicesbar_renders_the_epic_hero_line_format():
    src = _page()
    body = _function_body(src, "SlicesBar")
    assert "Slice" in body, "SlicesBar must render the 'Slice N/M' prefix"
    assert "▶" in body, "SlicesBar must render the ▶ focus marker on the active/next slice"
    assert "subtasks.length" in body, "the M in 'Slice N/M' must be the real subtasks count"


# ---------------------------------------------------------------------------
# AC-4 - <TokenTurns> receives a real session_quiet_s prop
# ---------------------------------------------------------------------------

def test_tokenturns_call_site_passes_session_quiet_s():
    src = _page()
    tag_text, _idx = _extract_tag(src, "TokenTurns")
    assert "session_quiet_s={" in tag_text, (
        "the <TokenTurns> call site must pass session_quiet_s so its shipped "
        "quiet-clock branch (TokenTurns.tsx:66,:73) receives real data"
    )
    assert "task.activity" in tag_text and "task.phase_progress" in tag_text and "??" in tag_text, (
        f"session_quiet_s must read task.activity?.session_quiet_s ?? "
        f"task.phase_progress?.session_quiet_s - got {tag_text!r}"
    )


def test_tokenturns_component_still_has_the_quiet_clock_branch():
    tt = _read(_TOKEN_TURNS)
    assert "session_quiet_s" in tt, "TokenTurns.tsx must still declare/consume session_quiet_s"


# ---------------------------------------------------------------------------
# AC-5 - the TileHero doc comment matches what TileHero itself renders
# ---------------------------------------------------------------------------

def test_tilehero_doc_comment_only_claims_surfaces_tilehero_itself_renders():
    src = _page()
    comment = _comment_block(src, "// TileHero —")
    body = _function_body(src, "TileHero")
    if re.search(r"metric grid|MetricCell", comment, re.I):
        assert "<MetricCell" in body, (
            "the doc comment claims a metric grid - TileHero's OWN JSX must "
            "actually render it (the comment must describe reality)"
        )
    for stale in ("10 real sdlc steps", "role caption under each", "gates drawn as", "[g] diamond"):
        assert stale not in comment.lower(), (
            f"TileHero's doc comment still claims {stale!r} - that is "
            "LabeledTimeline's surface, not TileHero's own JSX (:345-353 residue)"
        )


# ---------------------------------------------------------------------------
# AC-6 - ManagedTask.subtasks is genuinely consumed, not dead type residue
# ---------------------------------------------------------------------------

def test_managed_task_subtasks_field_is_consumed_not_dead_residue():
    # SUPERSEDED ANCHOR (merge with task 40c29b83's useConductorState hook):
    # the private `type ManagedTask = {` moved into the shared hook, whose
    # ManagedTask declares `subtasks`; ConductorPage keeps a narrow local
    # extension (`Omit<SharedManagedTask, ...>`). The INVARIANT is unchanged:
    # subtasks must be READ by a real call site (SlicesBar), never dead type
    # residue.
    src = _page()
    type_start = src.index("type ManagedTask = Omit<SharedManagedTask")
    type_end = src.index("};", type_start)
    usage_src = src[type_end:]
    assert "task.subtasks" in usage_src, (
        "ManagedTask.subtasks (shared hook type) must be READ by a real "
        "call site (SlicesBar), not merely declared on the type"
    )
