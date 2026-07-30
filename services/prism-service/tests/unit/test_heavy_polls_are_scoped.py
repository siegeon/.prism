"""The last two heavy polls get scoped (task d5465a25).

Follow-on to task 842248bd. Two calls ride the /tasks board's 5s poll while
rendering nothing:

  - GET /api/version?notes=true (262 KB changelog) fires on Sidebar MOUNT
    (lib/version.ts:83-98's useVersionNotes) for a hover-only tooltip that
    the user may never open.
  - GET /api/conductor/state (56 KB/call) ships `session_outcomes` (93% of
    the payload, api/conductor.py:26) unconditionally, even though neither
    of its two frontend pollers (ConductorPage.tsx, LiveBar.tsx) reads it —
    SessionsPage/SessionDetailPage get their own copy from the separate
    /api/sessions route (api/sessions.py:25).

FAILS TODAY because:
  - GET /api/conductor/state has no `include_outcomes` param at all; it
    always includes `session_outcomes` in the default response.
  - lib/version.ts's useVersionNotes() fires its fetch inside a bare mount
    useEffect(..., []) with no explicit trigger.
  - Sidebar.tsx's footer div has no onMouseEnter/onFocus wiring to defer
    that fetch.

The SPA ships no JS test runner, so the TSX/TS-facing acceptance criteria
(AC-1, AC-2, AC-7) are pinned by asserting the ACTUAL source, brace-balanced
from each function/tag's own start (never a fixed character window or a
comment), matching the convention in test_conductor_page_animated_cleanup_ui.py
and test_task_list_projection.py (from 842248bd).
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"


def _read(rel: str) -> str:
    p = _WEB / rel
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def _brace_body(src: str, marker: str) -> str:
    """Return the brace-balanced body of whatever `{...}` immediately
    follows the FIRST occurrence of `marker`, walking open/close braces by
    COUNT rather than searching for a hoped-for closing token — a stray
    brace inside an earlier string/comment can't truncate or overrun this,
    unlike a fixed-offset slice."""
    i = src.index(marker)
    brace = src.index("{", i)
    depth = 0
    j = brace
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[brace:j + 1]
        j += 1
    raise AssertionError(f"unbalanced braces scanning from marker {marker!r}")


# ---------------------------------------------------------------------------
# Backend: GET /api/conductor/state session_outcomes projection (AC-3, AC-4, AC-5)
# ---------------------------------------------------------------------------

def _client(monkeypatch, outcomes=None):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import conductor as conductor_api

    class _Svc:
        def exploration_rate(self):
            return 0.1

        def get_variants(self):
            return []

        def get_scores(self):
            return {}

        def get_session_outcomes(self, limit=200):
            return outcomes or []

        def get_retired(self):
            return []

        def managed_tasks(self):
            return [{"id": "t1", "title": "Some task"}]

        def step_buckets(self):
            return {"implement": 1}

    class _Ctx:
        conductor_svc = _Svc()

    monkeypatch.setattr(conductor_api, "get_project", lambda p: _Ctx())
    app = FastAPI()
    app.include_router(conductor_api.router, prefix="/api/conductor")
    return TestClient(app)


def test_state_default_omits_session_outcomes(monkeypatch):
    client = _client(monkeypatch, outcomes=[{"session_id": "s1"}])
    r = client.get("/api/conductor/state", params={"project": "prism"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "session_outcomes" not in body, (
        "default /api/conductor/state must OMIT session_outcomes (93% of "
        f"the payload, unread by ConductorPage/LiveBar); got keys {list(body.keys())}")


def test_state_include_outcomes_opt_in_returns_full_list(monkeypatch):
    client = _client(monkeypatch, outcomes=[{"session_id": "s1"}, {"session_id": "s2"}])
    r = client.get("/api/conductor/state",
                    params={"project": "prism", "include_outcomes": "true"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("session_outcomes"), (
        "explicit include_outcomes=true opt-in must still return the full list")
    assert len(body["session_outcomes"]) == 2


def test_state_default_still_carries_board_keys(monkeypatch):
    client = _client(monkeypatch)
    r = client.get("/api/conductor/state", params={"project": "prism"})
    assert r.status_code == 200, r.text
    body = r.json()
    for key in ("managed_tasks", "step_buckets", "board_health"):
        assert key in body, (
            f"default response must still carry {key!r} — ConductorPage/"
            "LiveBar read it; got keys " + str(list(body.keys())))


# ---------------------------------------------------------------------------
# Frontend source contracts (AC-1, AC-2, AC-7) — no JS test runner, so the
# ACTUAL TSX/TS source is parsed, brace-balanced from each construct's own
# start, never a fixed window or a comment.
# ---------------------------------------------------------------------------

def test_use_version_notes_no_longer_autofetches_on_mount():
    src = _read("lib/version.ts")
    body = _brace_body(src, "export function useVersionNotes(")
    assert "useEffect(" not in body, (
        "useVersionNotes must no longer auto-fetch inside a mount useEffect "
        "— the changelog fetch must be triggered explicitly (hover/focus), "
        f"never on render; got body: {body!r}")
    assert "ensureLoaded" in body, (
        "useVersionNotes must expose an explicit trigger (e.g. ensureLoaded) "
        "for the caller to invoke on hover/focus")


def test_ensure_loaded_short_circuits_when_already_cached():
    src = _read("lib/version.ts")
    hook_body = _brace_body(src, "export function useVersionNotes(")
    fn_body = _brace_body(hook_body, "ensureLoaded")
    assert "notesCached !== null" in fn_body, (
        "ensureLoaded must short-circuit when notesCached is already "
        "populated, so a second hover/focus does not re-download the "
        f"262 KB changelog; got body: {fn_body!r}")


def test_sidebar_footer_triggers_notes_load_on_hover_or_focus():
    src = _read("components/Sidebar.tsx")
    # The footer row is the div carrying title={versionNotes} — the hover
    # tooltip target — located by its OWN opening tag, not a fixed window.
    i = src.index("title={versionNotes}")
    tag_start = src.rindex("<div", 0, i)
    tag_end = src.index(">", i)
    tag = src[tag_start:tag_end]
    assert "onMouseEnter=" in tag or "onFocus=" in tag, (
        "the version footer div must trigger the notes load on hover/focus "
        f"(not on mount); got tag: {tag!r}")


def test_sidebar_still_renders_plain_version_unconditionally():
    src = _read("components/Sidebar.tsx")
    assert "useVersion()" in src, (
        "Sidebar must still call the lean useVersion() hook unconditionally")
    assert "version?.version" in src, (
        "Sidebar's footer must still render the plain version string")
