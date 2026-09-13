"""/workflows shows the whole board's LIVE occupancy by default, always.

Task a928f3d5 (child of 0b5dd37c) originally taught this page to
auto-attach to whichever task looked "working"/"driving" the instant the
page mounted, with no click needed. That auto-attach itself became the next
defect: SUPERSEDED 2026-09-13 (owner, verbatim, with a screenshot of the
conductor canvas replaying "LOADING RUN · 9/11/2026" while real work was
in flight elsewhere): "the playing is supposed to be IN the graph like in
a normal game, and it should be real time as we get updates from the
process." An unrequested auto-attach silently swaps the live whole-board
occupancy view for a single task's instance/replay overlay -- exactly the
"random animations" / stale-run complaint this page has fought before, just
with a different trigger. The fix is not a smarter selector; it's that
mounting the page must never open an instance view on its own. An instance
view (a run's replay, or a task's live current-step progress) now opens
ONLY from an explicit user action: clicking a rail pill, or a `?task=`
deep link.

The SPA has no JS test runner, so this pins the actual TSX source, the
convention used by tests/unit/test_conductor_page_animated_cleanup_ui.py.
"""

from __future__ import annotations

from pathlib import Path

_SRC = (Path(__file__).resolve().parent.parent.parent
        / "prism_service" / "web" / "src")
_PAGE = _SRC / "pages" / "WorkflowsPage.tsx"


def _read() -> str:
    return _PAGE.read_text(encoding="utf-8")


def test_no_effect_auto_attaches_to_a_working_or_driving_task_on_mount():
    """The old auto-attach effect picked a task off `activity?.state` with
    no user action behind it at all. That selector must be gone outright --
    not narrowed, not re-guarded -- because the DEFECT was mounting into an
    instance view unasked, not which task got picked."""
    src = _read()
    assert 'if (live) openConductorInstance(live);' not in src, (
        "an effect is still auto-selecting a task and opening its instance "
        "view on mount; the board must default to live whole-board "
        "occupancy and open an instance only from an explicit user action")


def test_open_conductor_instance_is_reachable_only_from_explicit_triggers():
    """The two legitimate call sites: a rail-pill click, and the `?task=`
    deep link effect (itself gated on a URL param the user or a link
    supplied, never on ambient task state alone)."""
    src = _read()
    assert src.count("openConductorInstance(") >= 2, (
        "expected at least the click-handler and ?task= call sites")
    click_site = 'onClick: task ? () => openConductorInstance(task) : undefined,'
    assert click_site in src, "the rail-pill click call site is missing"
    start = src.index('const taskParam = searchParams.get("task");')
    deep_link_effect = src[
        start:
        src.index("openConductorInstance(task)", start) + len("openConductorInstance(task)")
    ]
    assert "if (!taskParam) return;" in deep_link_effect, (
        "the ?task= deep link effect must bail out with no task param -- "
        "it is the only remaining automatic open path and must stay "
        "conditioned on an explicit URL the user or a link supplied")


def test_live_occupancy_is_the_default_view_when_nothing_is_working():
    """With no instance open the definition poll re-applies the board's
    real occupancy -- the honest whole-board view, always the default."""
    src = _read()
    # SUPERSEDED LITERAL (7.13.309, "a drilled behaviour layer's own
    # occupancy poll never froze"): the guard grew `|| selected.parent_id`
    # so a nested behaviour layer keeps polling live occupancy during a
    # replay. The property this test pins still holds -- with no instance
    # open (`!viewingInstanceRef.current`) the poll re-applies the board's
    # real occupancy -- the condition merely gained a second way in.
    assert ("if (selected && (!viewingInstanceRef.current || "
            "selected.parent_id)) {") in src, (
        "the live-occupancy branch guard changed; the board would no longer "
        "fall back to whole-board occupancy")
