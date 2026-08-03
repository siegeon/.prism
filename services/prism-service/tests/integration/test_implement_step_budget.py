"""task dcbd284f: a drive halts instead of retrying forever.

Driving 763ee039 through implement.js ran 3.9 HOURS, ~2.5 of them inside a
single step whose subprocess had wedged. The only stall guard counts
HAND-BACKS (implement.js `seen[job.step] > 3`) and it is evaluated BEFORE
`await agent(...)`, so while that await is pending the loop is still on its
first iteration and `seen[step]` can never reach 4. A step that never
returns is structurally invisible to it.

HOW THIS SUITE VERIFIES, and why it is not a regex over the source. This
task's own `likely_misfire` names the trap: "asserting the budget constant
exists in the source instead of proving a blocked run actually returns; a
source-text assertion passes while the drive still hangs forever." So the
budget arithmetic and the halt construction live in a MARKED, pure,
side-effect-free block of implement.js, and this suite EXECUTES that block
under node with stubbed inputs. It feeds real values in and asserts the
numbers and the halt object that come back, so it discriminates behaviour,
not spelling.

WHY THE BLOCK IS EXECUTED RATHER THAN IMPORTED: implement.js is a workflow
script, not a module. Its body runs on load and calls `agent()`, which
exists only inside the Workflow runtime, so pytest cannot import it.

WHAT THIS SUITE DOES NOT PROVE, stated so no reader mistakes it: the task
oracle also asks for an end-to-end run, launching implement.js against a
fixture task whose step blocks and watching the workflow RETURN. That needs
the Workflow runtime and real agent tokens, which pytest cannot spend. This
suite proves the budget resolves, clamps, escalates for known-slow steps,
reaches the step prompt, and yields a halt object carrying step/budget/
elapsed. It does not prove a live blocked drive returns.

The script cannot hold its own clock: implement.js's pre-flight fails the
drive on any `Date.now`/`new Date(` in .claude/workflows/*.js, and the
Workflow runtime throws on them (they break resume). So the deadline is
enforced by the step AGENT, which has a real clock through bash, exactly as
GATE_WAIT_S already works.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
# tests/integration -> tests -> prism-service -> services -> .prism
_REPO_ROOT = _HERE.parents[4]
_IMPLEMENT_JS = _REPO_ROOT / ".claude" / "workflows" / "implement.js"

# The pure block implement.js must expose for this suite to execute.
_OPEN = "// <step-budget>"
_CLOSE = "// </step-budget>"

# Contract bounds. The default must halt the 763ee039 incident EARLY enough
# that the owner would notice a difference: that step burned ~2.5h, so a
# default of hours would change nothing observable (misfire two).
_MAX_DEFAULT_S = 3600


@pytest.fixture(scope="module")
def src() -> str:
    assert _IMPLEMENT_JS.exists(), f"missing workflow script: {_IMPLEMENT_JS}"
    return _IMPLEMENT_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def block(src: str) -> str:
    """The pure, executable step-budget block of implement.js."""
    if _OPEN not in src or _CLOSE not in src:
        pytest.fail(
            f"implement.js must delimit its step-budget logic with {_OPEN} "
            f"... {_CLOSE} so this suite can EXECUTE it instead of pattern "
            "matching the source. Neither marker was found."
        )
    body = src.split(_OPEN, 1)[1].split(_CLOSE, 1)[0]
    assert body.strip(), "the step-budget block is empty"
    return body


def _node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node is not installed; cannot execute the budget block")
    return exe


def _run(block: str, args: dict, expr: str):
    """EXECUTE the budget block under node with `args` as the run input.

    Returns the evaluated `expr`. This is what makes the suite behavioural:
    the numbers asserted below are produced by the real code, not read out
    of the source text.
    """
    script = (
        f"const _in = {json.dumps(args)};\n"
        f"{block}\n"
        f"console.log(JSON.stringify({expr}));\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "probe.mjs"
        probe.write_text(script, encoding="utf-8")
        proc = subprocess.run(
            [_node(), str(probe)],
            capture_output=True, text=True, timeout=60,
        )
    if proc.returncode != 0:
        pytest.fail(
            f"the step-budget block did not execute under node "
            f"(rc={proc.returncode}). It must be pure and side-effect free, "
            f"depending only on `_in`.\nstderr:\n{proc.stderr.strip()}"
        )
    return json.loads(proc.stdout.strip())


def _strip_comments(js: str) -> str:
    """Drop // and /* */ comments, preserving ' " ` string bodies.

    Load-bearing: this repo has been burned three times by a source-reading
    assertion that was satisfied by an explanatory COMMENT rather than by
    the code. Every source assertion below runs on stripped text.
    """
    out, i, n = [], 0, len(js)
    while i < n:
        c = js[i]
        if c in "'\"`":
            out.append(c)
            i += 1
            while i < n:
                out.append(js[i])
                if js[i] == "\\":
                    i += 2
                    if i - 1 < n:
                        out.append(js[i - 1])
                    continue
                if js[i] == c:
                    i += 1
                    break
                i += 1
            continue
        if js.startswith("//", i):
            while i < n and js[i] != "\n":
                i += 1
            continue
        if js.startswith("/*", i):
            i = js.find("*/", i)
            i = n if i < 0 else i + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _fn_body(js: str, name: str) -> str:
    """Body of `function <name>(...) { ... }`, brace-matched.

    Never a fixed character window around a match: a comment above the code
    silently pushes the real statement out of such a window.
    """
    marker = f"function {name}("
    at = js.find(marker)
    assert at >= 0, f"implement.js no longer defines function {name}()"
    start = js.find("{", at)
    depth, i, n = 0, start, len(js)
    while i < n:
        if js[i] == "{":
            depth += 1
        elif js[i] == "}":
            depth -= 1
            if depth == 0:
                return js[start:i + 1]
        i += 1
    raise AssertionError(f"unbalanced braces in {name}()")


def test_ac1_budget_reads_args_and_defaults_to_a_finite_value(block):
    """AC-1: read from args, finite positive default."""
    assert "_in.step_budget_s" in block, (
        "the budget must be overridable per run via args, the way "
        "GATE_WAIT_S reads _in.gate_wait_s"
    )
    default = _run(block, {}, "STEP_BUDGET_S")
    assert isinstance(default, (int, float)), f"not a number: {default!r}"
    assert default > 0, "a zero/negative default is no bound at all"
    assert default == default and default not in (float("inf"),), (
        "the default must be finite; Infinity recreates the unbounded run"
    )
    assert default <= _MAX_DEFAULT_S, (
        f"default {default}s is too generous to change what the owner sees: "
        "the incident step burned ~2.5h, so a multi-hour default would have "
        "halted nothing worth noticing"
    )


def test_ac2_budget_is_clamped_at_both_ends(block):
    """AC-2: a caller cannot pass 0 or a huge value and unbound the run."""
    lo = _run(block, {"step_budget_s": 0}, "STEP_BUDGET_S")
    assert lo > 0, "step_budget_s=0 must clamp up to a positive floor"
    hi = _run(block, {"step_budget_s": 999999}, "STEP_BUDGET_S")
    assert hi < 999999, "a huge step_budget_s must clamp down to a ceiling"
    assert hi > lo, "the ceiling must sit above the floor"
    mid = _run(block, {"step_budget_s": 900}, "STEP_BUDGET_S")
    assert mid == 900, f"an in-range override must pass through, got {mid}"


def test_ac3_a_known_slow_step_gets_a_larger_allowance(block):
    """AC-3: verify_green_state runs a real suite; it needs more room."""
    got = _run(
        block, {},
        "[stepBudgetFor('verify_green_state'), stepBudgetFor('locate'), "
        "STEP_BUDGET_S]",
    )
    slow, quick, default = got
    assert slow > quick, (
        "verify_green_state must get a larger allowance than a quick step, "
        f"got slow={slow} quick={quick}"
    )
    assert quick == default, (
        f"an ordinary step should get the default, got {quick} vs {default}"
    )
    assert "verify_green_state" in block, (
        "the slow-step escalation must name the step in the code path that "
        "picks the number, not merely define an unused constant"
    )


def test_ac4_the_deadline_reaches_the_step_agent(src):
    """AC-4: the budget must bound a step that NEVER RETURNS.

    The script has no clock, so the sub-agent enforces the deadline. That
    only works if the resolved number is interpolated into the prompt the
    agent actually receives. This is contract stop_if item 1: a budget
    compared only BETWEEN loop iterations is exactly the guard that already
    exists and already fails to catch a hung step.
    """
    body = _fn_body(_strip_comments(src), "workerPrompt")
    assert ("stepBudgetFor(" in body) or ("STEP_BUDGET_S" in body), (
        "workerPrompt() must interpolate the step's budget; a budget the "
        "agent never sees cannot bound a call that never returns"
    )
    lowered = body.lower()
    assert any(w in lowered for w in ("deadline", "budget", "elapsed")), (
        "the prompt must instruct the agent to stop and report when its "
        "deadline passes"
    )


def test_ac5_exhaustion_names_step_budget_and_actual_elapsed(block):
    """AC-5: "over budget" without a duration cannot tell slow from wedged."""
    halt = _run(
        block, {},
        "budgetHalt('verify_green_state', 1800, 9123, 2)",
    )
    assert isinstance(halt, dict), f"budgetHalt must return an object: {halt!r}"
    assert halt.get("at") == "verify_green_state", (
        f"the halt must name the step it stopped at, got {halt.get('at')!r}"
    )
    reason = str(halt.get("reason") or "")
    assert reason.strip(), "the halt must carry a reason, never an empty one"
    for needle, what in (
        ("verify_green_state", "the step"),
        ("1800", "the budget it exceeded"),
        ("9123", "how long it actually ran"),
    ):
        assert needle in reason, (
            f"the halt reason must name {what}; missing {needle!r} in "
            f"{reason!r}"
        )


def test_ac6_a_budget_halt_is_a_halt_not_a_failed_run(block, src):
    """AC-6: contract stop_if item 2, never discard committed green work."""
    halt = _run(block, {}, "budgetHalt('verify_green_state', 1800, 9123, 2)")
    for banned in ("failed", "status", "error"):
        assert banned not in {str(k).lower() for k in halt}, (
            f"a budget halt must not carry a {banned!r} field; it is a halt "
            "over work that may be committed and green, not a failed run"
        )
    stripped = _strip_comments(src)
    assert "budgetHalt(" in stripped.split(_OPEN)[0] + stripped.split(
        _CLOSE, 1)[-1], (
        "the drive loop must actually CALL budgetHalt(); a helper defined "
        "and never invoked halts nothing"
    )
    assert "halted," in stripped or "halted:" in stripped, (
        "the halt must be returned in the workflow result alongside the "
        "drive's trace, so the report still carries what was produced"
    )


def _string_after(js: str, key: str) -> str:
    """The quoted string literal that follows `key:` in the source."""
    at = js.find(key)
    assert at >= 0, f"implement.js no longer declares {key}"
    i = at + len(key)
    while i < len(js) and js[i] not in "'\"`":
        i += 1
    quote, i = js[i], i + 1
    out = []
    while i < len(js):
        if js[i] == "\\":
            out.append(js[i + 1])
            i += 2
            continue
        if js[i] == quote:
            break
        out.append(js[i])
        i += 1
    return "".join(out)


def test_ac7_the_new_arg_is_documented_in_meta(src, block):
    """AC-7: discoverable the way gate_wait_s already is."""
    when = _string_after(src, "whenToUse:")
    assert "step_budget_s" in when, (
        "meta.whenToUse must document step_budget_s, the way it documents "
        "gate_wait_s; an override nobody can discover is not an override"
    )
    default = _run(block, {}, "STEP_BUDGET_S")
    assert str(int(default)) in when, (
        f"meta.whenToUse must state the default ({int(default)}) so a "
        "caller knows what they are changing"
    )
