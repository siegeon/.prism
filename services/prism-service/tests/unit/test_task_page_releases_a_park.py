"""UI contract test: a governance park gets a real Release button.

The PRISM SPA has NO JS test runner, so UI contracts are pinned by asserting
the ACTUAL TSX source -- the same pattern as
tests/unit/test_gate_banner_refreshes_on_gate_arrival.py and
test_step_rail_uses_task_own_workflow.py.

BUG (verified live): a task parked by dispatch-guard: or resume-actuator:
rendered a red "BLOCKED BECAUSE" banner with no owner-facing lever -- the
generic "-> in_progress" status button left the task blocked, and there was
no other affordance. "parked for a person" was a dead end for the person.

FIX: the BLOCKED banner grows a `park-release` button, mounted only under a
`governancePark` condition (blocked_reason starts with dispatch-guard: or
resume-actuator:), whose click handler POSTs
/api/conductor/park/release. The generic "-> in_progress" transition button
is disabled (not rerouted) while a governance park is present.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_TASK_DETAIL_PAGE = (
    _HERE.parent.parent.parent
    / "prism_service"
    / "web"
    / "src"
    / "pages"
    / "TaskDetailPage.tsx"
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _extract_braced_expr(src: str, marker: str) -> str:
    """Return the JSX `{...}` expression starting at the first `{` after
    `marker`, matched by BRACE DEPTH -- never a fixed slice -- so a comment
    or unrelated literal near the marker can't satisfy an assertion in
    place of the real guard (repeat failure mode across this suite; see
    test_gate_banner_refreshes_on_gate_arrival.py's identical helper)."""
    idx = src.index(marker)
    start = src.index("{", idx)
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces scanning forward from marker {marker!r}")


def test_governance_park_condition_matches_the_two_prefixes():
    src = _read(_TASK_DETAIL_PAGE)
    assert "const governancePark = " in src, \
        "a governancePark condition must be computed once and reused by " \
        "both the banner button and the generic transition button"
    start = src.index("const governancePark =")
    decl = src[start : src.index(";", start)]
    assert 'task.status === "blocked"' in decl, \
        "governancePark must require the task to actually be blocked"
    assert 'task.blocked_reason?.startsWith("dispatch-guard:")' in decl, \
        "governancePark must recognize the dispatch-guard: prefix"
    assert 'task.blocked_reason?.startsWith("resume-actuator:")' in decl, \
        "governancePark must recognize the resume-actuator: prefix"


def test_park_release_button_is_mounted_under_the_blocked_banner():
    src = _read(_TASK_DETAIL_PAGE)

    # Anchor on the real "Blocked because" banner (the top-of-page one that
    # is always visible, not the read-only history duplicate lower on the
    # Overview tab) and parse its ACTUAL JSX block by brace depth -- a
    # stray comment mentioning park-release near the banner must not
    # satisfy this test.
    banner_marker = '{task.status === "blocked" && task.blocked_reason && (\n        <Card>'
    assert banner_marker in src, "the top BLOCKED BECAUSE banner Card must still exist"
    banner_block = _extract_braced_expr(src, banner_marker.split("<Card>")[0].strip())

    assert 'id="park-release"' in banner_block, \
        "a real button with id=park-release must be rendered inside the " \
        "BLOCKED BECAUSE banner"
    assert "governancePark &&" in banner_block, \
        "the park-release button must be conditioned on governancePark, " \
        "not always rendered"
    assert 'aria-label="Release this park and try again"' in banner_block, \
        "the park-release button needs an accessible label naming its effect"
    assert "Release · try again" in banner_block, \
        "the park-release button must carry the specified label"
    assert "onClick={releasePark}" in banner_block, \
        "the park-release button must invoke the releasePark handler"


def test_release_park_posts_the_park_release_route_and_reports_errors_inline():
    src = _read(_TASK_DETAIL_PAGE)

    assert "const releasePark = async () => {" in src, \
        "a releasePark handler must exist"
    fn_block = _extract_braced_expr(src, "const releasePark = async () => ")

    assert "`/api/conductor/park/release?project=${project}`" in fn_block, \
        "releasePark must POST to /api/conductor/park/release with the " \
        "page's project param, matching the sibling-agent's route contract"
    assert 'method: "POST"' in fn_block, "the release call must be a POST"
    assert "task_id: id" in fn_block, "the release call must name the task"

    # A non-ok response (the contract's HTTP 409 for a non-governance
    # block, or any other refusal) must surface inline, never a silent
    # no-op and never only a toast.
    assert "!r.ok || body.ok === false" in fn_block, \
        "a non-2xx / ok:false response must be detected"
    assert "setParkReleaseError(" in fn_block, \
        "a failed release must set an inline error, not just a toast"

    # And that error state must actually be rendered next to the button.
    banner_marker = '{task.status === "blocked" && task.blocked_reason && (\n        <Card>'
    banner_block = _extract_braced_expr(src, banner_marker.split("<Card>")[0].strip())
    assert "parkReleaseError &&" in banner_block, \
        "the inline release error must render inside the blocked banner"


def test_generic_in_progress_transition_is_disabled_not_rerouted_during_a_park():
    src = _read(_TASK_DETAIL_PAGE)

    assert 'const parkedTransition = governancePark && target === "in_progress";' in src, \
        "the generic transition loop must compute a parkedTransition flag " \
        "for the in_progress target specifically"

    start = src.index("const parkedTransition")
    end = src.index("})}", start) + len("})}")
    button_block = src[start:end]

    assert "disabled={busy || parkedTransition}" in button_block, \
        "the generic in_progress button must disable while parked, " \
        "never silently proceed and leave the task blocked"
    assert 'title={parkedTransition ? "parked by a governance guard' in button_block, \
        "the disabled generic button must explain itself with a title " \
        "pointing at Release · try again"
