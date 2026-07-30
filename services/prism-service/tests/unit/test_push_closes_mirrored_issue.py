"""RED scaffold — a finished task closes its GitHub issue (task ae67ed5c).

Walking skeleton for the push half of the two-way mirror epic (02672417).
Before this: github_rest.py has no write path at all (only _get/issues/
pulls), and integration_outbox.py has enqueue/pending_items/mark_sent/is_echo
but nothing drains it. Pins the OTHER direction: a done, linked task with
GitHub sync enabled closes its mirrored issue, through the PRODUCTION-
registered adapter (api/integrations.py:register_builtin_adapters), routed
through the existing outbox so a later pull recognizes the echo.

Pre-declared misfire (task.likely_misfire): every test injects a fake
transport and nothing constructs the writer in production, or the entry
point is written as an unattended sweep over every done task. AC-4 and AC-5
below are aimed squarely at those two failure modes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

WS = "workspace-push"
REPO = "siegeon/.prism"


def _store(tmp_path):
    from prism_service.services.integration_store import IntegrationStore

    return IntegrationStore(str(tmp_path / "integrations.db"))


def _outbox(tmp_path):
    from prism_service.services.integration_outbox import IntegrationOutbox

    return IntegrationOutbox(str(tmp_path / "outbox.db"))


def _linked_issue(store, task_id="task-e696d952", display_key="#223",
                   active=True):
    """A container + entity + link, the same shape the PULL path leaves
    behind for a mirrored GitHub issue (work_item_sync._import_one)."""
    from prism_service.models.integration import ExternalEntityInput

    conn = store.ensure_connection(WS, "github", "install-push")
    cont = store.ensure_container(WS, conn.id, "repository", REPO,
                                  display_key=REPO)
    entity, _ = store.upsert_entity(
        WS, conn.id, cont.id,
        ExternalEntityInput(entity_kind="issue", remote_id="I_pushtest",
                            display_key=display_key,
                            url=f"https://github.com/{REPO}/issues/223"))
    link = store.claim_import_link(WS, entity.id, task_id)
    if active:
        store.activate_link(WS, link.id)
    return conn, cont, entity, task_id


def _push():
    from prism_service.services.work_item_sync import push_task_closure

    return push_task_closure


def _sync_prefs(enabled: bool):
    def _fn(workspace_id, provider):
        return enabled
    return _fn


class _NoGithubClient:
    """A GitHub client double that FAILS the test if it is ever reached —
    used everywhere a refusal must happen before any GitHub contact."""

    def close_issue(self, *a, **k):
        raise AssertionError("must never reach GitHub on a refusal path")


class _RecordingWriteTransport:
    def __init__(self, response=None):
        self.calls: list[tuple] = []
        self._response = response or {
            "number": 223, "state": "closed",
            "updated_at": "2026-07-29T00:00:00Z",
        }

    def __call__(self, url, token, body):
        self.calls.append((url, token, dict(body)))
        return self._response


def _bare_adapter(client=None):
    from prism_service.services.github_work import GitHubWorkAdapter

    return GitHubWorkAdapter(client or _NoGithubClient())


# ── AC-1: no external link -> refuse, GitHub never touched ─────────────

def test_task_with_no_external_link_is_never_pushed(tmp_path):
    push = _push()
    store = _store(tmp_path)
    outbox = _outbox(tmp_path)
    registry = {"github": _bare_adapter()}

    result = push(store, outbox, registry, _sync_prefs(True),
                  WS, "task-unlinked", task_is_done=True)

    assert result.eligible is False
    assert result.closed is False
    assert "link" in result.reason.lower()
    assert outbox.pending_items(WS) == []


# ── AC-2: sync_enabled False -> refuse before touching GitHub ──────────

def test_sync_disabled_refuses_before_touching_github(tmp_path):
    push = _push()
    store = _store(tmp_path)
    outbox = _outbox(tmp_path)
    _conn, _cont, _entity, task_id = _linked_issue(store)
    registry = {"github": _bare_adapter()}  # would raise if reached

    result = push(store, outbox, registry, _sync_prefs(False),
                  WS, task_id, task_is_done=True)

    assert result.eligible is False
    assert result.closed is False
    assert "off" in result.reason.lower() or "disabled" in result.reason.lower()
    assert outbox.pending_items(WS) == []


# ── AC-3: dry-run names exactly the one target, writes nothing ─────────

def test_dry_run_names_exactly_the_one_target_and_writes_nothing(tmp_path):
    push = _push()
    store = _store(tmp_path)
    outbox = _outbox(tmp_path)
    _conn, _cont, _entity, task_id = _linked_issue(store, display_key="#223")
    registry = {"github": _bare_adapter()}  # would raise if reached

    result = push(store, outbox, registry, _sync_prefs(True),
                  WS, task_id, task_is_done=True, dry_run=True)

    assert result.eligible is True
    assert result.closed is False
    assert result.repo == REPO
    assert result.issue == "#223"
    assert outbox.pending_items(WS) == [], "a dry-run must enqueue nothing"


# ── AC-4: a real push closes through the PRODUCTION-registered adapter ─

def test_push_closes_through_the_production_registered_adapter(
        tmp_path, monkeypatch):
    # The SAME registry api/integrations.py wires at import time — proves
    # the writer is reachable in production, not only in a test-only path
    # (the 0784729f misfire this task's likely_misfire names explicitly).
    from prism_service.api.integrations import _adapters

    adapter = _adapters.get("github")
    assert adapter is not None, (
        "production must register a github adapter (task f4dd3687)")

    class _FakeCreds:
        def installation_token(self, _=None):
            return "gho_faketoken"

    monkeypatch.setattr(adapter, "_credentials", _FakeCreds())
    write_transport = _RecordingWriteTransport()
    monkeypatch.setattr(adapter._client, "_write_transport", write_transport)

    push = _push()
    store = _store(tmp_path)
    outbox = _outbox(tmp_path)
    _conn, _cont, entity, task_id = _linked_issue(store, display_key="#223")

    result = push(store, outbox, _adapters, _sync_prefs(True),
                  WS, task_id, task_is_done=True, dry_run=False)

    assert result.closed is True, result.reason
    assert write_transport.calls, (
        "the real GithubRestClient.close_issue must be reached")
    url, token, body = write_transport.calls[0]
    assert url.endswith(f"/repos/{REPO}/issues/223")
    assert body == {"state": "closed"}, (
        "close-on-done only: title/body/labels must never be sent")
    assert token == "gho_faketoken"

    # Routed through the outbox (not a direct call at the mutation site): the
    # item left 'pending' and the marker is recognized as our own echo, so a
    # later inbound pull does not fight itself over PRISM's own write.
    assert outbox.pending_items(WS) == []
    marker = f"{entity.remote_id}:2026-07-29T00:00:00Z"
    assert outbox.is_echo(WS, marker) is True


def test_push_is_refused_when_task_is_not_done(tmp_path):
    push = _push()
    store = _store(tmp_path)
    outbox = _outbox(tmp_path)
    _conn, _cont, _entity, task_id = _linked_issue(store)
    registry = {"github": _bare_adapter()}  # would raise if reached

    result = push(store, outbox, registry, _sync_prefs(True),
                  WS, task_id, task_is_done=False)

    assert result.eligible is False
    assert result.closed is False
    assert "done" in result.reason.lower()


# ── AC-5: the entry point is scoped to ONE task_id, never a sweep ──────

def test_push_entry_point_never_sweeps_pending_items(tmp_path):
    import inspect

    from prism_service.services import work_item_sync as wis

    sig = inspect.signature(wis.push_task_closure)
    params = list(sig.parameters)
    assert "task_id" in params
    assert not any(p in params for p in ("task_ids", "tasks", "task_id_list")), (
        "the push entry point must take exactly one task_id, never a "
        "collection — an unattended sweep is the dangerous misfire named on "
        "this task")

    source = inspect.getsource(wis.push_task_closure)
    assert "pending_items(" not in source, (
        "the push entry point must never drain the whole outbox in one call")
