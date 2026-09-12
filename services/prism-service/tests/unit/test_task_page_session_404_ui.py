"""UI contract for the task-page session-404 defect (observed live on task
a65c66e5-b8a7-44b4-a223-f1342cfaaa14, 2026-09-12): the task page linked a
session whose transcript had not been parsed yet, GET /api/sessions/{id}
404d (only session_outcomes was consulted, never task_sessions), and the
archify task-map probe 404d on every load of any task with no built map --
both logged as failed requests in the browser console on a page that had
done nothing wrong.

The PRISM SPA has NO JS test runner, so these ACs are pinned by asserting
the ACTUAL TSX source (the same convention as
test_conductor_page_animated_cleanup_ui.py): SessionDetailPage renders a
"no transcript yet" state instead of a false all-zeros KPI grid, and
ArchifyMaps treats the server's built=false shape the same as "no map yet".
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_SESSION_DETAIL = _SRC / "pages" / "SessionDetailPage.tsx"
_ARCHIFY_MAPS = _SRC / "components" / "maps" / "ArchifyMaps.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_session_detail_page_declares_has_transcript_field():
    src = _read(_SESSION_DETAIL)
    assert "has_transcript" in src, \
        "SessionDetail must carry the server's has_transcript flag"


def test_session_detail_page_renders_no_transcript_state_before_kpi_grid():
    src = _read(_SESSION_DETAIL)
    assert "session.has_transcript === false" in src, \
        "a linked-but-unscored session must be branched on explicitly"
    assert "No transcript yet" in src, \
        "the no-transcript state must say so in plain words, not render zeros"
    # The no-transcript branch must appear BEFORE the KPI grid computation,
    # so an unscored session never reaches the misleading all-zero Kpi row.
    no_transcript_idx = src.index("session.has_transcript === false")
    kpi_idx = src.index('Kpi label="Tokens"')
    assert no_transcript_idx < kpi_idx, \
        "the no-transcript branch must short-circuit before the KPI grid renders"


def test_archify_maps_meta_type_declares_built_flag():
    src = _read(_ARCHIFY_MAPS)
    assert "built?: boolean" in src, \
        "ArchifyMeta must carry the server's built flag for kind=task"


def test_archify_maps_treats_built_false_as_not_built():
    src = _read(_ARCHIFY_MAPS)
    assert 'meta.built === false' in src, \
        "the empty/build-button branch must also fire on a 200 built:false response, " \
        "not only on a null meta from a 404"
