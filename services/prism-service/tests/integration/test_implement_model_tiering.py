"""task f4043364: pin implement.js's SDLC-role model tiering.

A drive used to spend the frontier model on every step (measured:
wf_15798a7d-99a, 74.7 min / 1.25M tokens, 11/11 agents on opus, ~4% of
elapsed in real pytest). implement.js:111-123 fixed this by mapping each
step's model to the capability tier PRISM already publishes per SDLC role
(sm=frontier, qa=balanced, dev=fast) via modelFor(role, step, isGate), with
every gate pinned to frontier regardless of role and an unknown/empty role
falling back to balanced (never the cheapest mechanical tier).

RETROFIT NOTE (task f4043364): the change these tests pin already landed on
this branch as commit 4fbb98e, hand-written with no task/test - the exact
"drive the conductor first" violation CLAUDE.md warns about (see the memory
this task's own description cites). These tests read the ACTUAL RENDERED
source text/call expressions (never a fixed character window or a comment)
so they discriminate real behavior: they FAIL against the pre-tiering
parent commit (119f194) and PASS against the current worktree. See this
task's write_failing_tests evidence for the red trace captured against
119f194 in a scratch checkout - the anchor problem this creates for
red_gate is the acknowledged, separate 19e4e7f7 lesson, not something this
file can fix.

Tests pin TIER NAMES (frontier/balanced/fast/mechanical) and RENDERED
source patterns, never a vendor model string - a future model-lineup swap
(e.g. TIER_MODEL.frontier moving off 'opus') must not redden this suite.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
# tests/integration -> tests -> prism-service -> services -> .prism
_REPO_ROOT = _HERE.parents[4]
_IMPLEMENT_JS = _REPO_ROOT / ".claude" / "workflows" / "implement.js"


@pytest.fixture(scope="module")
def src() -> str:
    assert _IMPLEMENT_JS.exists(), f"missing workflow script: {_IMPLEMENT_JS}"
    return _IMPLEMENT_JS.read_text(encoding="utf-8")


def _skip_string(text: str, i: int) -> int:
    """Index just past the '...' or "..." string starting at text[i]."""
    quote = text[i]
    i += 1
    while i < len(text):
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == quote:
            return i + 1
        i += 1
    raise AssertionError(f"unterminated string starting at index {i}")


def _skip_template(text: str, i: int) -> int:
    """Index just past the `...` template literal starting at text[i].

    Template literals nest (a `${...}` interpolation can itself contain a
    backtick template, as implement.js's settle prompt does for inline
    `` `command` `` snippets) - so ${ } expressions are tokenized
    recursively via _skip_expr rather than treated as opaque text.
    """
    assert text[i] == "`"
    i += 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            return i + 1
        if ch == "$" and text[i:i + 2] == "${":
            i = _skip_expr(text, i + 1)
            continue
        i += 1
    raise AssertionError(f"unterminated template literal starting at index {i}")


def _skip_expr(text: str, i: int) -> int:
    """Index just past the `{...}` of a `${...}` interpolation, brace-balanced."""
    assert text[i] == "{"
    depth = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch in ("'", '"'):
            i = _skip_string(text, i)
            continue
        if ch == "`":
            i = _skip_template(text, i)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError(f"unterminated ${{...}} expression starting at index {i}")


def _balanced_span(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    """Index of the char matching text[open_idx] (which must be open_ch).

    Skips over string/template literals so prose parens inside an agent()
    prompt string (e.g. "(gitbash resolves localhost->::1...)") never throw
    off the depth count of the surrounding JS call/object/function syntax.
    """
    assert text[open_idx] == open_ch
    depth = 0
    i = open_idx
    while i < len(text):
        ch = text[i]
        # Skip // line comments and /* block */ comments first - an
        # apostrophe in prose ("doesn't", "isn't") is not a string delimiter.
        if ch == "/" and text[i:i + 2] == "//":
            nl = text.find("\n", i)
            i = nl if nl != -1 else len(text)
            continue
        if ch == "/" and text[i:i + 2] == "/*":
            end = text.find("*/", i + 2)
            i = end + 2 if end != -1 else len(text)
            continue
        if ch in ("'", '"'):
            i = _skip_string(text, i)
            continue
        if ch == "`":
            i = _skip_template(text, i)
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise AssertionError(f"unbalanced {open_ch}{close_ch} from index {open_idx}")


def _agent_call_blocks(text: str) -> list[str]:
    """Every `await agent(...)` call site, as its full balanced-paren text.

    Matches the CALL EXPRESSION itself (never a comment or fixed window) by
    counting parens from the opening `(` of `agent(` to its matching close.
    """
    blocks = []
    for m in re.finditer(r"\bagent\(", text):
        if text[max(0, m.start() - 6):m.start()] != "await ":
            continue
        open_idx = m.end() - 1
        close_idx = _balanced_span(text, open_idx, "(", ")")
        blocks.append(text[m.start():close_idx + 1])
    return blocks


def _function_body(text: str, fn_name: str) -> str:
    """The full `{...}` body of `function <fn_name>(...) { ... }`, brace-balanced."""
    m = re.search(rf"function\s+{re.escape(fn_name)}\s*\([^)]*\)\s*\{{", text)
    assert m, f"could not locate function {fn_name} in source"
    open_idx = m.end() - 1
    close_idx = _balanced_span(text, open_idx, "{", "}")
    return text[open_idx:close_idx + 1]


class TestEveryAgentCallSitePassesExplicitModel:
    """AC-1: no `agent()` call site inherits the session default model.

    - oracle: every `await agent(...)` block in implement.js's rendered
      source contains a `model:` key (checked by parsing the balanced call
      expression, not a fixed window), and none passes the internal
      telemetry fallback `meta.model || null` as that key.
    """

    def test_every_await_agent_call_has_an_explicit_model_key(self, src: str) -> None:
        blocks = _agent_call_blocks(src)
        assert len(blocks) >= 6, (
            "expected multiple await agent(...) call sites in implement.js; "
            f"found {len(blocks)} - the extractor may be broken"
        )
        offenders = [b[:80].replace("\n", " ") for b in blocks if "model:" not in b]
        assert not offenders, (
            "await agent(...) call site(s) with no explicit model: (would "
            f"inherit the session's default/frontier model): {offenders}"
        )

    def test_no_call_site_uses_the_bare_meta_model_fallback(self, src: str) -> None:
        # meta.model || null is postAgentRun's OWN internal telemetry default
        # (not a call site) - a call site literally passing that expression
        # as its model: would still silently inherit the session default.
        for block in _agent_call_blocks(src):
            assert "meta.model || null" not in block


def _object_literal(text: str, var_name: str) -> str:
    """The `{ ... }` literal assigned to `const <var_name> = { ... }`."""
    m = re.search(rf"const\s+{re.escape(var_name)}\s*=\s*\{{", text)
    assert m, f"could not locate const {var_name} in source"
    open_idx = m.end() - 1
    close_idx = _balanced_span(text, open_idx, "{", "}")
    return text[open_idx:close_idx + 1]


class TestModelResolvesFromJobRole:
    """AC-2: the mapping keys off job.role (sm/qa/dev), not a step-name table.

    - oracle: ROLE_TIER is keyed by exactly {sm, qa, dev} mapped to the
      published tiers, and the pull-loop's own agent() call literally reads
      `modelFor(job.role, job.step, isGate)` - the SERVER-supplied field,
      not a client-side step->model table.
    """

    def test_role_tier_is_keyed_by_role_not_by_step_name(self, src: str) -> None:
        role_tier_src = _object_literal(src, "ROLE_TIER")
        keys = set(re.findall(r"(\w+):\s*'[a-z]+'", role_tier_src))
        assert keys == {"sm", "qa", "dev"}, (
            f"ROLE_TIER must be keyed by the published SDLC roles only, got {keys}"
        )

    def test_role_tier_honours_the_published_tiers(self, src: str) -> None:
        role_tier_src = _object_literal(src, "ROLE_TIER")
        mapping = dict(re.findall(r"(\w+):\s*'([a-z]+)'", role_tier_src))
        assert mapping.get("sm") == "frontier"
        assert mapping.get("qa") == "balanced"
        assert mapping.get("dev") == "fast"

    def test_drive_loop_passes_job_role_into_model_for(self, src: str) -> None:
        blocks = _agent_call_blocks(src)
        loop_blocks = [
            b for b in blocks
            if "isGate ? gatePrompt(job) : workerPrompt(job)" in b
        ]
        assert len(loop_blocks) == 1, "expected exactly one pull-loop agent() call"
        assert "modelFor(job.role, job.step, isGate)" in loop_blocks[0], (
            "the pull-loop step agent must resolve its model from the "
            "SERVER-supplied job.role, not a hardcoded step-name lookup"
        )


class TestGateStepsAlwaysResolveFrontier:
    """AC-3: story_gate/plan_gate/red_gate/green_gate never cheapen below frontier.

    - oracle: GATE_STEPS names exactly the four gates, and modelFor's isGate
      branch is the FIRST return in the function body - unconditional on
      role, so no role branch below it can ever cheapen a gate.
    """

    def test_gate_steps_constant_names_all_four_gates(self, src: str) -> None:
        m = re.search(r"const GATE_STEPS = \[([^\]]*)\]", src)
        assert m, "GATE_STEPS constant not found"
        steps = set(re.findall(r"'([a-z_]+)'", m.group(1)))
        assert steps == {"story_gate", "plan_gate", "red_gate", "green_gate"}

    def test_model_for_returns_frontier_for_any_gate_unconditionally(self, src: str) -> None:
        body = _function_body(src, "modelFor")
        gate_return = re.search(
            r"if\s*\(\s*isGate\s*\)\s*return\s*TIER_MODEL\.frontier", body,
        )
        assert gate_return, (
            "modelFor must return TIER_MODEL.frontier whenever isGate is true"
        )
        first_return = re.search(r"return\s+", body)
        assert first_return and first_return.start() == (
            gate_return.start() + gate_return.group(0).index("return")
        ), (
            "the isGate check must be the FIRST branch in modelFor, so a "
            "gate can never fall through to a role-based (cheaper) tier"
        )


class TestUnknownOrEmptyRoleFallsBackToBalanced:
    """AC-4: an unknown/empty role never falls to the mechanical (cheapest) tier.

    - oracle: modelFor's final fallback expression is literally
      `TIER_MODEL[tier] || TIER_MODEL.balanced` (never `.mechanical`), and
      MECHANICAL_STEPS is a small, named, hardcoded set of the two
      non-judgment steps (pre-flight, settle) - not a stand-in for "role
      unknown" (both call modelFor with role='' by construction).
    """

    def test_role_lookup_fallback_is_balanced_not_mechanical(self, src: str) -> None:
        body = _function_body(src, "modelFor")
        m = re.search(
            r"return\s+TIER_MODEL\[tier\]\s*\|\|\s*TIER_MODEL\.(\w+)", body,
        )
        assert m, "modelFor must fall back via `TIER_MODEL[tier] || TIER_MODEL.<tier>`"
        assert m.group(1) == "balanced", (
            "an unknown/empty role must fall back to TIER_MODEL.balanced, "
            f"not TIER_MODEL.{m.group(1)}"
        )

    def test_mechanical_tier_is_reserved_for_named_fetch_and_report_steps(self, src: str) -> None:
        m = re.search(r"const MECHANICAL_STEPS = \[([^\]]*)\]", src)
        assert m, "MECHANICAL_STEPS constant not found"
        steps = set(re.findall(r"'([a-z-]+)'", m.group(1)))
        assert steps == {"pre-flight", "settle"}


def test_model_for_signature_takes_role_as_first_parameter(src: str) -> None:
    assert re.search(
        r"function\s+modelFor\s*\(\s*role\s*,\s*step\s*,\s*isGate\s*\)", src,
    ), (
        "modelFor must accept role as its first parameter so callers pass "
        "job.role, not a step name, as the primary key"
    )
