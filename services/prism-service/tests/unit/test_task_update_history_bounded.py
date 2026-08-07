"""RED scaffold — task history must not embed entire large-field snapshots
on every edit (team-lead ask: task page loads slow, `history` dominates the
GET /api/tasks/{id} payload — 141 KB of 166 KB on one real task, because
`TaskService.update()` dumps the FULL old and new value of every changed
field, including multi-KB `plan_doc`/`plan_diagram` text, via `repr()` into
the history row's `details` column).

Measured live: one "updated" history row on task 0784729f was 28,545 bytes,
carrying the entire before AND after `plan_doc`. history() has no cap and
GET /api/tasks/{id} returns every row's `details` verbatim, so a task with
many plan/description edits ships its whole revision history as JSON on
every page load, even though PlanView's Timeline only ever displays a
truncated summary until a row is manually expanded.

The frontend's `turnSummary`/`parseTransition` (TaskDetailPage.tsx) for the
"updated" action only look at the FRONT of `details` (a leading
`<field>: ...` or `<field>:'a'->'b'` token), never a trailing marker, so a
bounded preview at the tail is safe — unlike "advance_task"/"gate_decide"
rows, which carry a load-bearing trailing `validation=`/`reason=` marker
that `update()` never writes (confirmed: those markers are appended only in
conductor_service.py / mcp/tools.py, a disjoint code path).

FAILS today: `update()` embeds the complete field value with no cap.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _task_svc(tmp_path, name="tasks.db"):
    from prism_service.services.task_service import TaskService
    return TaskService(str(tmp_path / name))


# A single edit of one large field must never cost anywhere near this much;
# the real bug produced a 28 KB single-field row.
MAX_DETAILS_BYTES_FOR_ONE_LARGE_EDIT = 2000


def test_editing_a_large_plan_doc_does_not_embed_the_whole_thing_twice(tmp_path):
    svc = _task_svc(tmp_path)
    task = svc.create(title="big plan doc task")

    huge_old = "OLD " + ("alpha beta gamma delta " * 400)  # ~9.6 KB
    huge_new = "NEW " + ("epsilon zeta eta theta " * 400)  # ~9.6 KB
    assert len(huge_old) > 8000 and len(huge_new) > 8000

    svc.update(task.id, plan_doc=huge_old)
    svc.update(task.id, plan_doc=huge_new)

    rows = svc.history(task.id)
    plan_doc_rows = [r for r in rows if r.action == "updated" and r.details.startswith("plan_doc:")]
    assert plan_doc_rows, f"no 'updated' row recorded plan_doc; rows={[r.details[:60] for r in rows]}"

    for row in plan_doc_rows:
        assert len(row.details) < MAX_DETAILS_BYTES_FOR_ONE_LARGE_EDIT, (
            f"a single plan_doc edit produced a {len(row.details)}-byte history "
            f"row (must stay under {MAX_DETAILS_BYTES_FOR_ONE_LARGE_EDIT}) — the "
            "full old+new text is still being embedded verbatim"
        )
        # Still recognizable as an edit of plan_doc (the frontend's
        # "edited <field>" fallback matches on this leading token).
        assert row.details.startswith("plan_doc:")


def test_a_short_field_diff_is_still_shown_in_full_uncapped():
    """Regression guard: the cap must not degrade the common case — short
    scalar diffs (status/priority/etc, always well under the cap) still
    render their exact old/new values, since PlanView's FLOW_FIELDS pills
    parse `field:'from'->'to'` verbatim with no summarization."""
    from prism_service.services.task_service import _history_value_repr

    assert _history_value_repr("pending") == "'pending'"
    assert _history_value_repr("done") == "'done'"
    assert _history_value_repr(3) == "3"
    assert _history_value_repr(None) == "None"


def test_large_value_repr_is_capped_with_a_length_marker():
    from prism_service.services.task_service import _history_value_repr

    huge = "x" * 5000
    out = _history_value_repr(huge)
    assert len(out) < 500, f"capped repr is still {len(out)} chars"
    assert "4800 chars" in out or "+4800" in out or "4,800" in out, (
        f"capped repr should say how much was elided, got: {out[:200]!r}"
    )
