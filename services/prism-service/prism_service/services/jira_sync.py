"""Slice D — bidirectional task<->Jira sync.

Three pieces, all SAFE by default:

  * outbound_create()  — push a mapped PRISM task to Jira as a new issue and
    store the returned key on task.jira_issue_key. A no-op unless Jira is
    connected AND a project mapping resolves, so existing task flows are
    untouched when Jira is disconnected (the DEFAULT).
  * poll_once()        — pull issues changed since a cursor and reconcile them
    onto mapped tasks (upsert by jira_issue_key).
  * start_jira_sync()  — the background poller. OFF by default: returns
    immediately (None) when PRISM_JIRA_SYNC_INTERVAL<=0, so nothing runs and
    the daemon never blocks/crashes on Jira. The whole loop body is guarded
    so a Jira/network error can never take the daemon down.

Sync events land in an in-memory receipt ring ({direction, task_id,
jira_issue_key, ok, ts}) read back by the UI over /api/jira/sync/receipts.
No secrets are ever recorded or returned. Layering: this is a SERVICE — it
imports jira_client / jira_auth / task_service, never api/.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone

from prism_service.services import jira_auth, jira_client

_RECEIPTS: "deque[dict]" = deque(maxlen=200)
_RECEIPTS_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Receipts — a small in-memory ring; the UI reads it, nothing persists yet.
# --------------------------------------------------------------------------

def record_receipt(direction: str, task_id: str, jira_issue_key: str,
                   ok: bool, ts: str | None = None) -> dict:
    """Append a sync receipt and return it. `direction` is 'IN' or 'OUT'.
    No secrets — only ids/keys/status."""
    receipt = {
        "direction": direction,
        "task_id": task_id or "",
        "jira_issue_key": jira_issue_key or "",
        "ok": bool(ok),
        "ts": ts or datetime.now(timezone.utc).isoformat(),
    }
    with _RECEIPTS_LOCK:
        _RECEIPTS.append(receipt)
    return receipt


def recent_receipts(limit: int = 50) -> list[dict]:
    """The most-recent receipts, newest last (chronological)."""
    with _RECEIPTS_LOCK:
        rows = list(_RECEIPTS)
    return rows[-limit:] if limit and limit > 0 else rows


def reset_receipts() -> None:
    """Clear the ring (test hook)."""
    with _RECEIPTS_LOCK:
        _RECEIPTS.clear()


# --------------------------------------------------------------------------
# Mapping — for this slice, a single default project key from the env. The
# per-task / per-project mapping STORE is a follow-up (see the slice notes).
# --------------------------------------------------------------------------

def project_key_for_task(task) -> str:
    """Resolve the Jira project key a task maps to. Slice D: the single
    default from PRISM_JIRA_PROJECT_KEY ('' when unmapped => outbound no-ops)."""
    return (os.environ.get("PRISM_JIRA_PROJECT_KEY", "") or "").strip()


# --------------------------------------------------------------------------
# Outbound — push a mapped task to Jira. SAFE: no-op unless connected + mapped.
# --------------------------------------------------------------------------

def outbound_create(task_svc, task, project_key: str | None = None) -> str | None:
    """Create a Jira issue for `task` and store the key on it. Returns the new
    key, or None when skipped (Jira disconnected, no project mapping, or the
    task is already mapped). Never raises into the caller's task flow — a Jira
    failure records an unsuccessful OUT receipt and returns None."""
    if not jira_auth.is_authenticated():
        return None
    if getattr(task, "jira_issue_key", "") or "":
        return None  # already mapped — nothing to create (and no recursion)
    key = project_key if project_key is not None else project_key_for_task(task)
    if not key:
        return None
    try:
        issue_key = jira_client.create_issue(key, task.title)
    except Exception:
        record_receipt("OUT", getattr(task, "id", ""), "", False)
        return None
    if not issue_key:
        record_receipt("OUT", getattr(task, "id", ""), "", False)
        return None
    task_svc.update(task.id, jira_issue_key=issue_key)
    task.jira_issue_key = issue_key  # reflect on the caller's in-memory object
    record_receipt("OUT", task.id, issue_key, True)
    return issue_key


# --------------------------------------------------------------------------
# Inbound — reconcile changed Jira issues onto mapped tasks (upsert by key).
# --------------------------------------------------------------------------

def poll_once(task_svc, since_ts) -> int:
    """Pull issues updated since `since_ts` and reconcile each onto the task
    it maps to (matched by jira_issue_key). Returns the count reconciled.
    Guarded per-issue so one bad row can't abort the batch."""
    try:
        issues = jira_client.list_updated_since(since_ts)
    except Exception:
        return 0
    reconciled = 0
    for issue in issues or []:
        try:
            reconciled += _reconcile_issue(task_svc, issue)
        except Exception:
            key = (issue or {}).get("key", "") if isinstance(issue, dict) else ""
            record_receipt("IN", "", key, False)
    return reconciled


def _reconcile_issue(task_svc, issue: dict) -> int:
    """Upsert the task mapped to `issue` from its fields. Updates the mapped
    task's title from the issue summary; records an IN receipt. Returns 1 when
    a task was touched, else 0."""
    key = (issue.get("key") or "").strip()
    if not key:
        return 0
    fields = issue.get("fields") or {}
    summary = (fields.get("summary") or "").strip()
    matches = task_svc.list(id=None)
    matches = [t for t in matches if getattr(t, "jira_issue_key", "") == key]
    if not matches:
        return 0
    task = matches[0]
    if summary and summary != task.title:
        task_svc.update(task.id, title=summary)
    record_receipt("IN", task.id, key, True)
    return 1


# --------------------------------------------------------------------------
# Poller — OFF by default. start_jira_sync returns None unless an interval is
# explicitly set > 0, so the daemon boots with sync disabled.
# --------------------------------------------------------------------------

def _resolve_interval() -> int:
    """Seconds between poll passes, from PRISM_JIRA_SYNC_INTERVAL (config's
    JIRA_SYNC_INTERVAL_SECONDS is the default). <=0 disables — the DEFAULT."""
    raw = os.environ.get("PRISM_JIRA_SYNC_INTERVAL")
    if raw is None:
        try:
            from prism_service import config
            return int(getattr(config, "JIRA_SYNC_INTERVAL_SECONDS", 0) or 0)
        except Exception:
            return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _sync_loop(interval: int) -> None:
    """Poll every `interval`s, reconciling each project's tasks. The whole
    body is guarded so a Jira/network/store error only skips a pass — it can
    NEVER crash the daemon."""
    print(f"[jira_sync] poller running every {interval}s", file=sys.stderr, flush=True)
    cursors: dict[str, float] = {}
    while True:
        try:
            if jira_auth.is_authenticated():
                from prism_service.project_context import get_all_projects, get_project
                now = time.time()
                for pid in get_all_projects():
                    since = cursors.get(pid, now - interval)
                    try:
                        poll_once(get_project(pid).task_svc, since)
                        cursors[pid] = now
                    except Exception as e:
                        print(f"[jira_sync] {pid}: {type(e).__name__}: {e}",
                              file=sys.stderr, flush=True)
        except Exception as e:
            print(f"[jira_sync] loop error: {e}", file=sys.stderr, flush=True)
        time.sleep(interval)


def start_jira_sync(*_args, **_kwargs) -> threading.Thread | None:
    """Spawn the background poller thread, or return None when disabled
    (interval<=0 — the DEFAULT). Never loops or raises in the disabled path."""
    interval = _resolve_interval()
    if interval <= 0:
        print("[jira_sync] poller disabled (PRISM_JIRA_SYNC_INTERVAL<=0)",
              file=sys.stderr, flush=True)
        return None
    thread = threading.Thread(
        target=_sync_loop, args=(interval,), daemon=True, name="jira-sync")
    thread.start()
    return thread
