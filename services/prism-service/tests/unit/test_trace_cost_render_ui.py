"""Task 1bc0b316: the task Trace RENDERS per-step cost and tokens.

Follow-up to 9a51e670, which wired the data but changed no UI ("no UI code
changes" was that plan's own constraint), so nothing drew it. The payload
now carries per-step cost_usd, per-session cost_total and totals.cost_usd
(agent_runs_data.py, PR #2329); this slice is the pure render.

The ticket's ONLY stop_if is "client recomputes cost instead of reading the
payload", so that is pinned directly and first: no rate table, no price
constant, no tokens-times-rate expression anywhere in the Trace path. Every
dollar on screen must trace to a payload field.

Convention (test_conductor_page_animated_cleanup_ui.py): the PRISM SPA has
no JS test runner, so UI ACs are pinned by asserting the ACTUAL TSX source.
Assertions strip comments first and parse the enclosing function body by
brace balance rather than a fixed window — a comment mentioning cost must
never satisfy a pin.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


def _strip_comments(src: str) -> str:
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
    return src


def _read(*parts: str) -> str:
    path = _WEB.joinpath(*parts)
    assert path.exists(), f"expected {path} to exist"
    return _strip_comments(path.read_text(encoding="utf-8"))


def _fn(src: str, name: str) -> str:
    """The brace-balanced body of `function <name>(...) { ... }` — so a pin
    lands inside the renderer it names, never on a neighbour."""
    m = re.search(rf"function\s+{name}\s*\(", src)
    assert m, f"function {name} not found"
    # Balance the PARAMETER list first. These renderers destructure their
    # props (`function TraceStepRow({ step, max })`), so the first "{" after
    # the name opens the destructuring pattern, not the body — pinning that
    # slice would assert against the wrong text entirely.
    depth, i = 1, m.end()
    while depth:
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
        i += 1
    open_idx = src.index("{", i)
    depth = 0
    for i in range(open_idx, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[open_idx:i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _detail() -> str:
    return _read("pages", "TaskDetailPage.tsx")


# --------------------------------------------------------------------------
# The stop_if, pinned first and hardest.
# --------------------------------------------------------------------------

def test_the_client_never_computes_cost():
    """The ticket's only stop_if. A dollar figure the client DERIVED is a
    second source of truth for money, and it silently disagrees with the
    payload the moment a rate changes."""
    src = _detail()
    trace_path = _fn(src, "TraceStepRow") + _fn(src, "TraceView") + _fn(src, "TraceKpi")

    banned = re.compile(
        r"\b(RATE|PRICE|PRICING|COST_PER|PER_TOKEN|PER_1K|USD_PER)\w*\b", re.IGNORECASE)
    hit = banned.search(trace_path)
    assert not hit, (
        f"the Trace path declares a pricing constant ({hit.group(0)!r}) — cost "
        "must be READ from the payload, never derived client-side")

    # Multiplication alone is NOT the smell — TraceStepRow legitimately
    # computes a bar width as (100 * tokens) / max, which is a percentage and
    # has nothing to do with money. The stop_if is arithmetic that PRODUCES a
    # dollar figure, so only flag a multiplying line that is also talking
    # about cost.
    money = re.compile(r"cost|usd|price|dollar", re.IGNORECASE)
    for line in trace_path.splitlines():
        if "*" in line and money.search(line):
            raise AssertionError(
                f"the Trace path derives a money value by arithmetic: {line.strip()!r} "
                "— cost must be READ from the payload (the ticket's stop_if)")


def test_every_rendered_dollar_traces_to_a_payload_field():
    """Positive half of the same rule: the row and the tile read cost off
    the payload objects."""
    src = _detail()
    assert re.search(r"step\.cost_usd", _fn(src, "TraceStepRow")), (
        "TraceStepRow must read step.cost_usd from the payload")
    assert re.search(r"totals\??\.cost_usd", _fn(src, "TraceView")), (
        "TraceView's cost tile must read totals.cost_usd from the payload")


# --------------------------------------------------------------------------
# The render itself.
# --------------------------------------------------------------------------

def test_the_payload_types_carry_cost():
    """The TS types must admit the fields the service already sends, or the
    render is reading something TypeScript says does not exist."""
    src = _detail()
    step_type = src[src.index("type TraceStep"):src.index("type TraceSession")]
    assert "cost_usd" in step_type, "type TraceStep must declare cost_usd"
    totals_type = src[src.index("type TaskTrace"):src.index("type TaskTrace") + 400]
    assert "cost_usd" in totals_type, "TaskTrace totals must declare cost_usd"


def test_a_step_row_shows_its_own_cost():
    """AC-1: the per-step dollar figure is on the row, beside its tokens."""
    row = _fn(_detail(), "TraceStepRow")
    assert "fmtUsd" in row, (
        "TraceStepRow must format its cost through the shared fmtUsd")


def test_the_trace_tab_shows_a_total_cost_tile():
    """AC-2: one totals tile, reading the payload's own total."""
    view = _fn(_detail(), "TraceView")
    assert re.search(r'TraceKpi[^>]*label="Total cost"', view), (
        'TraceView must render a TraceKpi labelled "Total cost"')


def test_absent_cost_renders_a_dash_never_a_measured_looking_zero():
    """AC-5. Runs captured before cost existed report 0.0, which is an
    ABSENCE, not a measurement — the live payload shows exactly this
    (totals.cost_usd 0.0 across 15 steps on task 9a51e670). Printing
    "$0.00" would assert a measurement nobody took."""
    src = _detail()
    row, view = _fn(src, "TraceStepRow"), _fn(src, "TraceView")
    for name, body in (("TraceStepRow", row), ("TraceView", view)):
        # The dash must hang off the COST expression. Asserting a bare "—"
        # somewhere in the body would pass on the token column's existing
        # fallback and never bite.
        assert re.search(r'cost[^;\n]*(—|&mdash;)', body, re.IGNORECASE), (
            f"{name} must fall back to a dash on the COST value when it is "
            "absent — not merely contain a dash somewhere")
        assert '"$0.00"' not in body and "'$0.00'" not in body, (
            f"{name} hardcodes $0.00 — an absent measurement must not read "
            "as a measured zero")


def test_the_money_formatter_is_shared_not_re_declared():
    """One money formatter for the SPA. SpendPanel already had a private
    fmtUsd; a second copy in the Trace would be two roundings of the same
    dollars, free to drift."""
    fmt = _read("lib", "format.ts")
    assert re.search(r"export\s+function\s+fmtUsd\s*\(", fmt), (
        "fmtUsd must live in lib/format.ts beside fmtTokens")
    detail = _detail()
    assert re.search(r'fmtUsd[^;]*from\s+"@/lib/format"', detail, re.DOTALL) or \
        re.search(r'fmtUsd[^;]*from\s+"\.\./lib/format"', detail, re.DOTALL), (
        "TaskDetailPage must import the shared fmtUsd, not declare its own")
    assert not re.search(r"function\s+fmtUsd\s*\(", detail), (
        "TaskDetailPage re-declares fmtUsd instead of importing it")
    spend = _read("components", "SpendPanel.tsx")
    assert not re.search(r"function\s+fmtUsd\s*\(", spend), (
        "SpendPanel must consume the shared fmtUsd now that one exists")


def test_session_sum_token_rows_are_not_dressed_up_as_per_step_truth():
    """AC-4 / the recorded likely_misfire. Measured on the live payload:
    step keys are ['step','role','model','tokens','cost_usd','gate_state',
    'gate_source','ts'] — there is NO field marking a row whose tokens came
    from whole-session backfill (agent_runs_data._backfill_session_tokens).
    Since the render cannot DETECT such a row, it must not invent a claim
    about one: no badge asserting per-step truth, and no cost fabricated to
    sit beside it. The token figure itself stays exactly as it renders
    today — correcting it is d39bb5bd's job, not this slice's.
    """
    row = _fn(_detail(), "TraceStepRow")
    assert not re.search(r"per[-_ ]?step\s+truth|verified\s+per[-_ ]?step", row,
                         re.IGNORECASE), (
        "the row must not assert per-step accuracy it cannot establish")
    # Cost is the only NEW claim this slice makes, and it is payload-sourced.
    assert "step.cost_usd" in row
