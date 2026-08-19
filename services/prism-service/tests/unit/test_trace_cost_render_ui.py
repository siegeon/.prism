"""Red tests for task 1bc0b316 — "Render per-step cost and tokens on the
task Trace".

The PRISM SPA has NO JS test runner, so UI acceptance criteria are pinned
by asserting the ACTUAL web source (TSX) — same pattern as
tests/unit/test_task_page_loading_skeleton_ui.py.

The trace payload already carries the money (agent_runs_data.py:489
per-step ``"cost_usd": cost``, :516 ``totals["cost_usd"]``); this slice is
pure render in pages/TaskDetailPage.tsx. These tests FAIL against the
current source: neither ``type TraceStep`` nor ``type TaskTrace`` declares
cost_usd, TraceStepRow renders no dollar cell, and TraceView has no cost
tile.

Function bodies are extracted BRACE-BALANCED from each function's own
declaration — never a fixed character window — so a comment near the code
cannot satisfy an assertion (lesson: match the rendered construct, not
prose near it).

AC-4 (label/omit whole-session-sum rows) is deliberately NOT pinned: the
step payload built at agent_runs_data.py:484-499 exposes only
step/role/model/tokens/cost_usd/gate_state/gate_source/ts — there is no
field that distinguishes a whole-session-sum row from a per-step row, so
per the approved plan the render OMITS rather than guesses and no pin
exists to write until d39bb5bd lands a discriminator.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PAGE = _ROOT / "prism_service" / "web" / "src" / "pages" / "TaskDetailPage.tsx"


def _src() -> str:
    return _PAGE.read_text(encoding="utf-8")


def _fn_body(src: str, name: str) -> str:
    """Return the brace-balanced BODY of ``function <name>(...) {...}``.

    Walks past the (possibly destructured, typed) parameter list by paren
    depth, then extracts the body by brace depth — so assertions run
    against the function's actual code, never a fixed-width slice that a
    nearby comment could drift into.
    """
    decl = src.find(f"function {name}")
    assert decl != -1, f"TaskDetailPage.tsx must declare function {name}"
    i = src.find("(", decl)
    assert i != -1
    depth = 0
    while i < len(src):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    body_open = src.find("{", i)
    assert body_open != -1, f"function {name} must have a body"
    depth = 0
    j = body_open
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[body_open + 1 : j]
        j += 1
    raise AssertionError(f"unbalanced braces in function {name}")


def _type_block(src: str, name: str) -> str:
    """Return the brace-balanced block of ``type <name> = {...}``."""
    decl = src.find(f"type {name} = {{")
    assert decl != -1, f"TaskDetailPage.tsx must declare type {name}"
    open_i = src.find("{", decl)
    depth = 0
    j = open_i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[open_i : j + 1]
        j += 1
    raise AssertionError(f"unbalanced braces in type {name}")


# ---------------------------------------------------------------- AC-1 —
# per-step $: the TraceStep type declares cost_usd and TraceStepRow reads
# it from the payload step (a dollar cell beside the existing token cell).


def test_trace_step_type_declares_cost_usd():
    block = _type_block(_src(), "TraceStep")
    assert re.search(r"\bcost_usd\??\s*:", block), (
        "type TraceStep must declare a cost_usd field mirroring the trace "
        "payload's per-step cost_usd (agent_runs_data.py:489)"
    )


def test_trace_step_row_renders_payload_cost():
    body = _fn_body(_src(), "TraceStepRow")
    assert re.search(r"step\.cost_usd\b", body), (
        "TraceStepRow must read step.cost_usd — the per-step dollar figure "
        "comes from the trace payload, not from anywhere else"
    )


# ---------------------------------------------------------------- AC-2 —
# totals tile: TaskTrace.totals declares cost_usd and TraceView renders a
# cost TraceKpi tile sourced from it.


def test_task_trace_totals_type_declares_cost_usd():
    block = _type_block(_src(), "TaskTrace")
    assert re.search(r"\bcost_usd\??\s*:", block), (
        "type TaskTrace's totals must declare cost_usd mirroring the "
        "payload's totals.cost_usd (agent_runs_data.py:516)"
    )


def test_trace_view_renders_totals_cost_tile():
    body = _fn_body(_src(), "TraceView")
    assert re.search(r"\bcost_usd\b", body), (
        "TraceView must read the payload's totals cost_usd for the tile"
    )
    kpis = re.findall(r"<TraceKpi\b[^/]*?/>", body, flags=re.S)
    cost_kpis = [k for k in kpis if re.search(r"(?i)cost", k)]
    assert cost_kpis, (
        "TraceView must render a TraceKpi tile whose label names cost, "
        "next to the existing Total tokens / Steps / Sessions tiles"
    )
    assert any("cost_usd" in k for k in cost_kpis), (
        "the cost tile's value must be derived from the payload's "
        "cost_usd, not from any other figure"
    )


# ---------------------------------------------------------------- AC-3 —
# stop_if, pinned directly: the client never recomputes cost. The Trace
# render path reads cost_usd verbatim — no per-token rate constant, no
# tokens-times-rate arithmetic, no multiplication on cost_usd itself.


def test_trace_path_never_recomputes_cost():
    src = _src()
    bodies = _fn_body(src, "TraceStepRow") + _fn_body(src, "TraceView")
    assert not re.search(r"(?i)(usd_per|per_tok\w*_?(rate|usd|price)|token_?(rate|price)|price_per)", bodies), (
        "stop_if: the Trace path must not carry a per-token rate/price "
        "constant — cost comes from the payload"
    )
    assert not re.search(r"cost_usd\s*\*|\*\s*[\w.]*cost_usd", bodies), (
        "stop_if: cost_usd must be rendered, never multiplied — the "
        "client does not recompute cost"
    )
    assert not re.search(r"\btokens[\w.]*\s*\*\s*[\d.]", bodies), (
        "stop_if: no tokens-times-literal arithmetic in the Trace render "
        "path — a token count must never be turned into dollars client-side"
    )


# ---------------------------------------------------------------- AC-5 —
# honest absence: a row/tile with no attributable cost renders an em-dash,
# never a fabricated $0.00 (the payload coerces DB NULL to 0.0, so falsy
# cost means "not attributed", exactly like the existing token cell).


def test_step_row_cost_dash_not_zero():
    body = _fn_body(_src(), "TraceStepRow")
    cost_exprs = [
        m.group(0)
        for m in re.finditer(r"\{[^{}]*cost_usd[^{}]*\?[^{}]*:[^{}]*\}", body)
    ]
    assert any("—" in e for e in cost_exprs), (
        "TraceStepRow's dollar cell must fall back to an em-dash when the "
        "step has no attributed cost (a `cost ? $ : —` conditional), the "
        "same honest-absence pattern as its token cell"
    )


def test_trace_path_never_renders_fabricated_zero_dollars():
    src = _src()
    bodies = _fn_body(src, "TraceStepRow") + _fn_body(src, "TraceView")
    assert not re.search(r"\$0\.00", bodies), (
        "AC-5: an absent cost renders an em-dash, never a literal $0.00"
    )
