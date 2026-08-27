"""A killed step agent halts the drive honestly (task bb388e9d).

Seen twice on 2026-08-17 (wf_95bab5dd-39f, wf_1dec1101-044): when the
permission/safety classifier blocks one of implement.js's spawned step
agents, agent() resolves to null and the script crashes dereferencing it -
"null is not an object (evaluating 't.step')" at workflow.js:1033 and
"(evaluating 'graph.entities')" at workflow.js:706 - so the drive dies with
a TypeError instead of halting with the blocker's reason.

The workflow scripts have no JS test runner (repo convention: JS ACs are
pinned by asserting the ACTUAL source), so this pins three null-guard sites
in .claude/workflows/implement.js:

1. `locate` - dereferenced as `locate.halt_reason` right after assignment.
2. `graph` - dereferenced as `graph.entities` right after assignment.
3. the step loop's `res` - pushed into `trace` and later read back as
   `t.step` when the trace is summarized.

Each guard must route its null case to an explicit `kind: 'blocked'` halt
carrying a non-empty reason naming the documented causes (classifier kill,
user skip, terminal API error after retries) - never a silent no-op and
never a bare crash.
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_WORKFLOW = _REPO_ROOT / ".claude" / "workflows" / "implement.js"


def _source() -> str:
    return _WORKFLOW.read_text(encoding="utf-8")


def _reason_snippet(src: str, guard_at: int) -> str:
    """Grab the guard block's body (from the guard to its closing brace-ish end)."""
    return src[guard_at : guard_at + 400]


def test_locate_null_guard_precedes_halt_reason_access():
    src = _source()

    agent_at = src.index("const locate = await agent(")
    guard_at = src.index("if (!locate)")
    deref_at = src.index("locate.halt_reason")

    assert agent_at < guard_at < deref_at, (
        "a null/falsy guard on `locate` itself must run after the agent() "
        "call and before `locate.halt_reason` is ever dereferenced - "
        "otherwise a classifier-killed locate agent crashes here"
    )

    block = _reason_snippet(src, guard_at)
    assert "kind: 'blocked'" in block or 'kind: "blocked"' in block, (
        "a null `locate` must halt with kind='blocked', not silently "
        "fall through or rethrow an unhandled exception"
    )


def test_graph_null_guard_precedes_entities_access():
    src = _source()

    agent_at = src.index("const graph = await agent(")
    guard_at = src.index("if (!graph)")
    deref_at = src.index("graph.entities")

    assert agent_at < guard_at < deref_at, (
        "a null/falsy guard on `graph` itself (not just a `|| []` fallback "
        "on a property) must run before `graph.entities` is dereferenced"
    )

    block = _reason_snippet(src, guard_at)
    assert "kind: 'blocked'" in block or 'kind: "blocked"' in block, (
        "a null `graph` must halt with kind='blocked', not throw "
        "'Cannot read properties of null (reading entities)'"
    )


def test_step_loop_guards_null_res_before_trace_push():
    src = _source()

    guard_at = src.index("if (!res)")
    push_at = src.index("trace.push(res)")

    assert guard_at < push_at, (
        "a null `res` from the step-loop agent() call must be caught and "
        "halted BEFORE it is pushed into `trace` - otherwise the later "
        "`trace.map((t) => ({ step: t.step, ... }))` summary dereferences "
        "`t.step` on a null entry and crashes"
    )

    block = _reason_snippet(src, guard_at)
    assert "kind: 'blocked'" in block or 'kind: "blocked"' in block, (
        "a null step-loop `res` must halt with kind='blocked' rather than "
        "being silently dropped or crashing later at `t.step`"
    )
    assert "break" in block, (
        "the null-res guard must break out of the step loop immediately, "
        "matching the file's existing halted-then-break convention"
    )


def test_blocked_halt_reason_is_non_empty_and_descriptive():
    src = _source()

    guard_sites = [
        src.index("if (!locate)"),
        src.index("if (!graph)"),
        src.index("if (!res)"),
    ]

    for guard_at in guard_sites:
        block = _reason_snippet(src, guard_at)
        reason_at = block.index("reason")
        reason_tail = block[reason_at : reason_at + 300]

        assert (
            "classifier" in reason_tail
            or "skip" in reason_tail
            or "API error" in reason_tail
        ), (
            "the halt reason must name at least one of the documented "
            "null causes (classifier kill / user skip / terminal API "
            "error) so the invoking thread learns the real blocker "
            "instead of a generic, unhelpful message"
        )
