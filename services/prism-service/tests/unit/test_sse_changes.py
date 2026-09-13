"""Pins GET /sse/changes -- the SSE replacement for the SPA's shared
GET /api/changes 1Hz poll (owner 2026-09-13: "why are you hammering the
server with polling rather than updating with streaming").

Driven by services/wakeups.py's wait()/changed_since() rather than the
in-process `bus` other /sse/* routes use, specifically because wakeups
already bridges CROSS-PROCESS signals: the 2026-09-13 worker-host process
split moved the nine standing workers into a separate OS process from the
API, so a signal raised there would never reach an in-process asyncio
queue -- but wakeups.wait() polls a small sqlite table under the data dir
every 250ms and picks it up regardless of which process called signal().
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

from prism_service.routes.sse import _gen_changes
from prism_service.services import wakeups


class _FakeRequest:
    """Just enough of a Starlette Request for _gen_changes: it only ever
    calls `is_disconnected()`. Always reports connected."""

    async def is_disconnected(self) -> bool:
        return False


def setup_function(_fn) -> None:
    wakeups._reset_for_tests()


# ---------------------------------------------------------------------------
# changed_since -- which kind(s) actually moved since a baseline
# ---------------------------------------------------------------------------

def test_changed_since_reports_a_kind_newer_than_the_baseline():
    since = time.time()
    time.sleep(0.01)
    wakeups.signal("task_changed", "prism", task_id="t-1")
    changes = wakeups.changed_since({"task_changed", "shipped"}, "prism", since)
    assert len(changes) == 1
    kind, project, ts, task_id = changes[0]
    assert kind == "task_changed"
    assert project == "prism"
    assert ts > since
    assert task_id == "t-1"


def test_changed_since_ignores_a_signal_before_the_baseline():
    wakeups.signal("task_changed", "prism")
    since = time.time()
    changes = wakeups.changed_since({"task_changed"}, "prism", since)
    assert changes == []


def test_changed_since_scopes_by_project():
    since = time.time()
    wakeups.signal("task_changed", "other-project")
    changes = wakeups.changed_since({"task_changed"}, "prism", since)
    assert changes == []


def test_changed_since_sees_a_wildcard_signal():
    since = time.time()
    wakeups.signal("shipped", "*")
    changes = wakeups.changed_since({"shipped"}, "prism", since)
    assert len(changes) == 1
    assert changes[0][0] == "shipped"


def test_changed_since_reports_each_kind_that_moved():
    since = time.time()
    wakeups.signal("task_changed", "prism")
    wakeups.signal("shipped", "prism")
    changes = wakeups.changed_since({"task_changed", "shipped"}, "prism", since)
    kinds = {c[0] for c in changes}
    assert kinds == {"task_changed", "shipped"}


# ---------------------------------------------------------------------------
# debug_sources -- GET /api/changes?debug=1's "which call site is noisy"
# ---------------------------------------------------------------------------

def test_debug_sources_names_the_calling_file_and_line():
    wakeups.signal("task_changed", "prism")
    sources = wakeups.debug_sources()
    assert len(sources) == 1
    row = sources[0]
    assert row["kind"] == "task_changed"
    assert "test_sse_changes.py:" in row["source"]
    assert row["count"] == 1


def test_debug_sources_counts_repeat_calls_from_the_same_site():
    for _ in range(3):
        wakeups.signal("activity", "prism")
    sources = wakeups.debug_sources()
    assert sources[0]["count"] == 3


def test_debug_sources_ranks_the_busiest_site_first():
    for _ in range(5):
        wakeups.signal("task_changed", "prism")
    wakeups.signal("shipped", "prism")
    sources = wakeups.debug_sources()
    assert sources[0]["kind"] == "task_changed"
    assert sources[0]["count"] == 5


# ---------------------------------------------------------------------------
# GET /sse/changes -- the real generator, driven directly (see _FakeRequest
# above: TestClient's synchronous HTTP layer buffers a StreamingResponse's
# chunks unpredictably against an infinite generator, which hung a first
# version of this test; pulling frames straight off _gen_changes is the
# same code path (the route handler does nothing but wrap it in a
# StreamingResponse) without that layer).
# ---------------------------------------------------------------------------

async def _next_data_event(gen) -> dict:
    async for chunk in gen:
        line = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
        if line.startswith("data: "):
            return json.loads(line[len("data: "):line.index("\n")])
    raise AssertionError("generator ended with no data event")


def test_sse_changes_streams_a_real_signal_to_a_connected_client():
    async def run():
        gen = _gen_changes(_FakeRequest(), "prism")
        first = await gen.__anext__()
        assert first == b": connected\n\n"
        # Signal from THIS (the test) process -- the same-process path,
        # picked up via the in-memory threading.Condition notify_all().
        wakeups.signal("task_changed", "prism", task_id="t-42")
        event = await _next_data_event(gen)
        assert event["kind"] == "task_changed"
        assert event["project"] == "prism"
        assert event["task_id"] == "t-42"
        assert isinstance(event["at"], (int, float))
        await gen.aclose()

    asyncio.run(run())


def test_signal_in_a_separate_os_process_reaches_the_sse_stream(tmp_path):
    """The load-bearing case: a wakeups.signal() call made from a CHILD
    process (standing in for the worker-host split) must still surface on
    this endpoint, via wakeups.wait()'s cross-process sqlite poll -- not
    just the in-process `bus` every other /sse/* route relies on."""
    data_dir = os.environ["PRISM_DATA_DIR"]
    ready_path = tmp_path / "ready"
    script = (
        "import os, time\n"
        f"os.environ['PRISM_DATA_DIR'] = {data_dir!r}\n"
        "from prism_service.services import wakeups as w\n"
        f"open({str(ready_path)!r}, 'w').write('ready')\n"
        "time.sleep(0.2)\n"
        "w.signal('shipped', 'prism')\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.time() + 15.0
        while not ready_path.exists() and time.time() < deadline:
            time.sleep(0.02)
        assert ready_path.exists(), "child process never started"

        async def run():
            gen = _gen_changes(_FakeRequest(), "prism")
            first = await gen.__anext__()
            assert first == b": connected\n\n"
            event = await _next_data_event(gen)
            assert event["kind"] == "shipped"
            assert event["project"] == "prism"
            await gen.aclose()

        asyncio.run(run())
    finally:
        proc.wait(timeout=5.0)
        assert proc.returncode == 0
