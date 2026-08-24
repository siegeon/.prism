"""Task e8fc073b — task pages load fast even when jobs are busy.

Root cause (server): `_collect_jobs` (api/jobs.py:28-41) constructs a FRESH
`UnderstandEngine` per request; `.queue` (engines/understand_engine.py:
118-124) always finds `self._queue is None` and rebuilds a `JobQueue`
(inference/queue.py:82-90), which `read_text()`s the WHOLE
`understand_queue.jsonl` and replays every line through `_apply` on every
single GET /api/jobs. jobs.py:37 also iterates `queue._jobs.values()`
directly, unguarded by the queue's own lock.

Root cause (client): `useScanActivity` (web/src/lib/scan-activity.ts:41-91)
is a private per-mount useEffect/setTimeout loop with no shared cache and no
in-flight guard; five separate mounts (LiveBar, LiveStatusStrip, Sidebar,
SettingsPage JobsPanel, SettingsPage ScanProgress) each poll /api/jobs
independently.

FAILS TODAY because:
  - `UnderstandEngine` has no process-wide queue registry — every
    `UnderstandEngine(pid)` constructs its OWN `JobQueue` (AC-1, AC-2).
  - `JobQueue` has no `snapshot()` method at all (AC-4).
  - `api/jobs.py`'s `_collect_jobs` reads `queue._jobs.values()` directly
    (AC-4).
  - `scan-activity.ts` has no shared/module-level poller or in-flight guard
    — each `useScanActivity()` call owns a private timer, and
    `SettingsPage.tsx`'s `JobsPanel`/`ScanProgress` each run their own
    separate `setInterval`/`setTimeout` loop against /api/jobs (AC-5, AC-6).

AC-9/AC-10 (cold task-page load timing, browser network capture) are
observational and are pinned with screenshot/timing evidence at
verify_green_state, not by this pytest file. AC-11 (scope guards) is
enforced by the conductor's own control-plane check on the branch diff.

The SPA ships no JS test runner, so the TSX/TS-facing ACs are pinned by
asserting the ACTUAL source, brace-balanced from each construct's own
start (never a fixed character window or a comment) — the convention
already established by test_heavy_polls_are_scoped.py and
test_conductor_page_animated_cleanup_ui.py.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_PKG = _SERVICE_ROOT / "prism_service"
_WEB = _PKG / "web" / "src"


def _read(rel: str) -> str:
    p = _WEB / rel
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def _read_py(rel: str) -> str:
    p = _PKG / rel
    assert p.exists(), f"expected source missing: {p}"
    return p.read_text(encoding="utf-8")


def _walk_braces(src: str, brace: int) -> str:
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
    raise AssertionError(f"unbalanced braces scanning from index {brace}")


def _fn_body_from_signature_line(src: str, marker: str) -> str:
    """Body of a `marker(...): T {` / `marker(...) {` signature — opens
    from the LAST `{` on the signature's own line (so an inline TS
    return-type object annotation on the same line can't be mistaken for
    the body), then balances braces from there."""
    i = src.index(marker)
    eol = src.index("\n", i)
    return _walk_braces(src, src.rindex("{", i, eol))


def _match_parens(src: str, open_idx: int) -> int:
    """Index of the matching ')' for the '(' at open_idx."""
    depth = 0
    j = open_idx
    while j < len(src):
        if src[j] == "(":
            depth += 1
        elif src[j] == ")":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    raise AssertionError(f"unbalanced parens scanning from index {open_idx}")


def _top_level_fn_body(src: str, fn_name: str) -> str:
    """Body of `function {fn_name}(...) {...}`, robust to a multi-line
    destructured/typed parameter list — balances PARENS first to find
    where the param list actually closes, then balances BRACES from the
    first '{' after that (the real body open), never a fixed window."""
    marker = f"function {fn_name}("
    i = src.index(marker)
    paren_open = i + len(marker) - 1
    paren_close = _match_parens(src, paren_open)
    brace_open = src.index("{", paren_close)
    return _walk_braces(src, brace_open)


# ---------------------------------------------------------------------------
# Backend: JobQueue construction cost + registry (AC-1, AC-2, AC-3, AC-4)
# ---------------------------------------------------------------------------

def _client(monkeypatch, tmp_path, project_ids):
    """A bare FastAPI app carrying only the jobs router, with
    project_data_dir and list_projects redirected into tmp_path so no
    real project data is touched."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import jobs as jobs_api
    from prism_service.engines import understand_engine as ue

    monkeypatch.setattr(ue, "project_data_dir", lambda pid: (tmp_path / pid))
    monkeypatch.setattr(jobs_api, "list_projects", lambda: list(project_ids))

    app = FastAPI()
    app.include_router(jobs_api.router, prefix="/api/jobs")
    return TestClient(app), jobs_api, ue


def test_jobqueue_init_runs_once_across_n_sequential_requests(tmp_path, monkeypatch):
    """AC-1: a second /api/jobs request must not re-read the queue log from
    the beginning — JobQueue.__init__ runs once (not N times) across N
    sequential requests against the same project."""
    from prism_service.inference import queue as queue_mod

    client, jobs_api, ue = _client(monkeypatch, tmp_path, ["proj-a"])

    calls = {"n": 0}
    orig_init = queue_mod.JobQueue.__init__

    def counting_init(self, *a, **kw):
        calls["n"] += 1
        return orig_init(self, *a, **kw)

    monkeypatch.setattr(queue_mod.JobQueue, "__init__", counting_init)

    for _ in range(3):
        r = client.get("/api/jobs", params={"limit": 200})
        assert r.status_code == 200, r.text

    assert calls["n"] == 1, (
        f"JobQueue.__init__ ran {calls['n']} times across 3 sequential "
        "GET /api/jobs requests against the SAME project — expected exactly "
        "1 (a process-wide queue registry keyed off the resolved project "
        "data dir), got repeated construction driven by api/jobs.py:34 "
        "building a fresh UnderstandEngine (and therefore a fresh JobQueue) "
        "on every request"
    )


def test_understand_engine_queue_registry_keyed_by_resolved_data_dir(tmp_path, monkeypatch):
    """AC-2: UnderstandEngine(pid).queue returns the SAME object across two
    separate engine instances for the same project, and a DIFFERENT object
    for a different project."""
    from prism_service.engines import understand_engine as ue

    monkeypatch.setattr(ue, "project_data_dir", lambda pid: (tmp_path / pid))

    e1 = ue.UnderstandEngine("proj-a")
    e2 = ue.UnderstandEngine("proj-a")
    assert e1.queue is e2.queue, (
        "two UnderstandEngine instances for the SAME project must share ONE "
        "JobQueue via a process-wide registry keyed by resolved "
        "project_data_dir — engines/understand_engine.py:114-124 currently "
        "sets self._queue = None per instance, so every instance builds its "
        "own private JobQueue"
    )

    e3 = ue.UnderstandEngine("proj-b")
    assert e3.queue is not e1.queue, (
        "a DIFFERENT project must get a DIFFERENT JobQueue object — the "
        "registry must key on the resolved project_data_dir, not collapse "
        "every project onto one shared queue"
    )


def test_job_appended_after_snapshot_is_visible_on_next_read(tmp_path, monkeypatch):
    """AC-3: a job appended to the log by a writer after a snapshot was
    built is visible on the VERY NEXT /api/jobs read (no stale-frozen-scan
    regression from caching the queue). Regression guard: passes today
    (every request rebuilds from disk); must keep passing once the
    registry (AC-1/AC-2) caches the JobQueue object."""
    client, jobs_api, ue = _client(monkeypatch, tmp_path, ["proj-a"])

    r0 = client.get("/api/jobs", params={"limit": 200})
    assert r0.status_code == 200, r0.text
    assert r0.json()["count"] == 0

    engine = ue.UnderstandEngine("proj-a")
    jid = engine.queue.enqueue("docs", "deadbeef", "scopehash1")

    r1 = client.get("/api/jobs", params={"limit": 200})
    assert r1.status_code == 200, r1.text
    ids = {j["id"] for j in r1.json()["jobs"]}
    assert jid in ids, (
        f"job {jid} enqueued after the first /api/jobs read must be "
        f"visible on the very next read; got job ids {ids}"
    )


def test_jobs_read_path_goes_through_lock_guarded_snapshot(tmp_path, monkeypatch):
    """AC-4: the jobs read path never iterates the mutable job table
    unguarded — reads go through a lock-guarded snapshot(), not private
    queue._jobs access."""
    from prism_service.inference import queue as queue_mod

    client, jobs_api, ue = _client(monkeypatch, tmp_path, ["proj-a"])
    engine = ue.UnderstandEngine("proj-a")
    engine.queue.enqueue("docs", "deadbeef", "scopehash1")

    assert hasattr(queue_mod.JobQueue, "snapshot"), (
        "JobQueue must expose a snapshot() method returning a lock-guarded "
        "copy of the job rows — inference/queue.py has no such method today"
    )

    calls = {"n": 0}
    orig_snapshot = queue_mod.JobQueue.snapshot

    def spy_snapshot(self, *a, **kw):
        calls["n"] += 1
        return orig_snapshot(self, *a, **kw)

    monkeypatch.setattr(queue_mod.JobQueue, "snapshot", spy_snapshot)

    rows = jobs_api._collect_jobs(None)
    assert calls["n"] >= 1, (
        "_collect_jobs must read job rows via queue.snapshot(), never via "
        "queue._jobs.values() directly (api/jobs.py:37)"
    )
    assert len(rows) == 1


def test_jobs_py_source_no_longer_touches_private_job_table():
    """Companion source check for AC-4: the private-attribute access at
    api/jobs.py:37 must be gone, replaced by a snapshot() call."""
    src = _read_py("api/jobs.py")
    assert "._jobs.values()" not in src, (
        "api/jobs.py must not read queue._jobs.values() directly (private, "
        "unguarded by the queue's own lock) — read via queue.snapshot() "
        f"instead; jobs.py source:\n{src}"
    )
    assert ".snapshot(" in src, (
        "api/jobs.py must call queue.snapshot() to read job rows"
    )


# ---------------------------------------------------------------------------
# Frontend source contracts (AC-5, AC-6, AC-7) — no JS test runner, so the
# ACTUAL TSX/TS source is parsed, brace-balanced from each construct's own
# start, never a fixed window or a comment.
# ---------------------------------------------------------------------------

def test_use_scan_activity_no_longer_owns_a_private_poll_timer():
    """AC-5: useScanActivity must stop scheduling its OWN private
    setTimeout(load, ...) loop — it must derive from a shared poller so
    every mounted surface rides one /api/jobs poll instead of each
    surface starting its own timer."""
    src = _read("lib/scan-activity.ts")
    hook_body = _fn_body_from_signature_line(src, "export function useScanActivity(")
    assert "setTimeout(load" not in hook_body, (
        "useScanActivity must stop scheduling its own private "
        f"setTimeout(load, ...) loop; got body: {hook_body!r}"
    )


def test_settings_page_imports_a_shared_poller_not_just_the_type():
    """AC-5: SettingsPage.tsx must import a shared poller/hook from
    lib/scan-activity (not just the ScanJob type) so JobsPanel and
    ScanProgress can stop running their own independent /api/jobs polls."""
    src = _read("pages/SettingsPage.tsx")
    import_lines = [
        line for line in src.splitlines()
        if 'from "@/lib/scan-activity"' in line
    ]
    assert import_lines, "SettingsPage.tsx must import from @/lib/scan-activity"
    assert not all(line.strip().startswith("import type") for line in import_lines), (
        "SettingsPage.tsx currently imports ONLY `type ScanJob` from "
        "lib/scan-activity — it must also import a real (non-type) shared "
        f"poller hook; got import lines: {import_lines!r}"
    )


def test_jobs_panel_no_longer_runs_its_own_poll_interval():
    """AC-5: JobsPanel must stop scheduling its own setInterval(load, 5000)
    against /api/jobs and consume the shared poller instead."""
    src = _read("pages/SettingsPage.tsx")
    body = _top_level_fn_body(src, "JobsPanel")
    assert "setInterval(load" not in body, (
        "JobsPanel must stop scheduling its own setInterval(load, 5000) "
        f"against /api/jobs; body head: {body[:600]!r}"
    )


def test_scan_progress_no_longer_runs_its_own_poll_timeout():
    """AC-5, AC-8: ScanProgress must stop scheduling its own
    setTimeout(poll, ...) loop against /api/jobs and consume the shared
    poller instead, while still rendering per-row progress (regression
    guard for the Settings scan-progress bar, verified live at
    verify_green_state)."""
    src = _read("pages/SettingsPage.tsx")
    body = _top_level_fn_body(src, "ScanProgress")
    assert "setTimeout(poll" not in body, (
        "ScanProgress must stop scheduling its own setTimeout(poll, ...) "
        f"loop against /api/jobs; body head: {body[:600]!r}"
    )


def test_shared_poller_has_an_inflight_guard_set_before_await_cleared_in_finally():
    """AC-6: the shared poller refuses to stack — while a request is
    outstanding, a scheduled tick issues no second request (in-flight
    guard set before await, cleared in finally)."""
    src = _read("lib/scan-activity.ts")
    assert "inFlight" in src, (
        "scan-activity.ts's shared poller must track an in-flight guard "
        "(e.g. `let inFlight`) so a scheduled tick issues no second "
        "request while one is outstanding"
    )
    set_true_idx = src.index("inFlight = true")
    await_idx = src.index("await api.get")
    assert set_true_idx < await_idx, (
        "the in-flight guard must be set BEFORE the await, not after — "
        f"found 'inFlight = true' at {set_true_idx}, "
        f"'await api.get' at {await_idx}"
    )
    finally_idx = src.index("finally", await_idx)
    clear_idx = src.index("inFlight = false", finally_idx)
    assert clear_idx > finally_idx, (
        "the in-flight guard must be cleared inside a finally block, after "
        "the await, so it always clears even on a thrown/rejected fetch"
    )


def test_cadence_and_hidden_tab_discipline_survive():
    """AC-7: cadence/hidden-tab discipline unchanged — POLL_HOT_MS stays
    2000, POLL_COLD_MS stays 10000, and the document.hidden skip survives
    the refactor into a shared poller. Regression guard (task c38ef597)."""
    src = _read("lib/scan-activity.ts")
    assert "const POLL_HOT_MS = 2000;" in src, (
        "POLL_HOT_MS must stay 2000ms")
    assert "const POLL_COLD_MS = 10000;" in src, (
        "POLL_COLD_MS must stay 10000ms")
    assert "document.hidden" in src, (
        "the hidden-tab skip (task c38ef597) must survive — a background "
        "tab must not keep polling /api/jobs every 2s forever"
    )


def test_shell_surfaces_still_call_use_scan_activity_unchanged():
    """AC-7 regression guard: LiveStatusStrip and Sidebar keep calling
    useScanActivity() by name — only its internals move to a shared poller,
    the call sites don't change.

    SUPERSEDED for LiveBar.tsx (task d9f082fe follow-up, owner live,
    2026-08-24): LiveBar no longer renders the "queue N" scan-pending
    metadata it used useScanActivity() for — it was simplified to a bare
    live/idle status dot (see LiveBar.tsx's own module docstring), so it
    legitimately no longer calls the hook at all. Dropped from this guard;
    LiveStatusStrip and Sidebar are unaffected and still assert below."""
    for rel in (
        "components/LiveStatusStrip.tsx",
        "components/Sidebar.tsx",
    ):
        src = _read(rel)
        assert "useScanActivity()" in src, (
            f"{rel} must still call useScanActivity() unchanged"
        )
