"""The daemon seat that runs the align-language workflow (task f07c9cea,
epic df0eed4a).

Owner rule mx-f49a5c: an agent-shaped behaviour is a TOP-LEVEL WORKFLOW
the SYSTEM controls, like the conductor -- registered in WORKFLOWS, on
the Workflows page catalog, driven by its own versioned per-project
behaviour file, run by a daemon worker that creates a VISIBLE run task
per pass, with the API/UI button only ever triggering that same run.
This module is that worker.

Off by default. ``PRISM_LANGUAGE_ALIGNMENT_WORKER=on`` enables the
seat; ``PRISM_LANGUAGE_ALIGNMENT_WORKER_INTERVAL`` sets the tick
interval in seconds (minimum 60, default 900); pin the sweep to one
project with ``PRISM_LANGUAGE_ALIGNMENT_WORKER_PROJECT`` -- omitted,
every project ``config.list_projects()`` returns is swept. Mirrors the
env-var shape ``services/memory_ops_worker.py`` uses for its own
per-op ``PRISM_<OP>_WORKER``/``_INTERVAL``/``_PROJECT`` gates.

Each pass that finds real work creates exactly ONE visible run task
(``workflow="align_language"``) and drives it, step by step, through
the SAME server-side conductor report path
``services/task_runner.py`` drives a task through --
``api/conductor_flow.flow_start`` / ``flow_report`` -- under its own
seat identity, ``SEAT_ID``. ``POST /api/tasks/align-language`` and the
``task_align_language`` MCP tool both call ``run_once_for`` directly,
so a person's button click and the daemon's own tick run the exact
same code path -- the API/UI trigger never re-implements the drive.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Optional

from prism_service.services import system_activity

SEAT_ID = "prism-language-alignment-worker"
_ENV_PREFIX = "PRISM_LANGUAGE_ALIGNMENT_WORKER"
_RULE_NAMES = ("text-is-plain", "text-uses-canonical-terms")


def _log(msg: str) -> None:
    print(f"[language-alignment-worker] {msg}", file=sys.stderr, flush=True)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "on", "true", "yes")


def is_enabled() -> bool:
    """True when this environment opted the seat in."""
    return _env_truthy(_ENV_PREFIX)


def _interval_s() -> int:
    try:
        return max(60, int(os.environ.get(f"{_ENV_PREFIX}_INTERVAL", "900")))
    except ValueError:
        return 900


def _projects_in_scope() -> list[str]:
    pinned = os.environ.get(f"{_ENV_PREFIX}_PROJECT", "").strip()
    if pinned:
        return [pinned]
    from prism_service.config import list_projects
    try:
        return list_projects()
    except Exception:
        return []


def _load_behavior(project: str) -> dict:
    from prism_service.api.workflows import align_language_behavior_document
    return align_language_behavior_document(project)


def _rule_counts(project: str) -> dict:
    """{"text-is-plain": n, ...} -- a rule that does not exist yet in this
    tree's shapes.ttl (e.g. text-uses-canonical-terms) is simply absent
    from the dict, never a KeyError. Best-effort: an evaluate() failure
    (e.g. no OntologyGraph yet) reads as an empty dict."""
    from prism_service.services import ontology_rules

    out: dict[str, int] = {}
    try:
        rows = ontology_rules.evaluate(project)
    except Exception:
        return out
    for row in rows:
        name = row.get("name")
        if name in _RULE_NAMES:
            out[name] = row.get("violations", 0)
    return out


#: project -> the (count, max_updated_at) TaskService.text_fingerprint()
#: returned the last time run_once_for actually looked. Tick-cost pass
#: (owner brief, 2026-09-13): in-memory and per-process, like the other
#: standing workers' own backoff/fetch caches -- a restart re-scans once,
#: which is the correct bias.
_LAST_FINGERPRINT: dict[str, tuple[int, str]] = {}


def reset_fingerprint_cache() -> None:
    """Test-only: forces the next run_once_for call for every project to
    treat its fingerprint as unseen."""
    _LAST_FINGERPRINT.clear()


def run_once_for(project: str, force: bool = False) -> dict:
    """One align-language pass for ``project``.

    Returns ``{"skipped": <reason>}`` when there is nothing to do (the
    behaviour is disabled and ``force`` is False, a project's task text
    has not moved since the last look, or a dry run finds zero
    candidates), ``{"ok": False, ...}`` if the drive itself could not
    start, or ``{"run_task_id", "report"}`` once a run task has been
    created and driven all the way to ``done``. Never raises -- every
    failure path returns a result dict so a tick loop stays alive.
    """
    from prism_service.services import language_alignment

    behavior = _load_behavior(project)
    if not behavior.get("enabled", True) and not force:
        return {"skipped": "disabled"}

    if not force:
        # COST (owner brief, 2026-09-13): the dry run below scans every
        # task's free text just to answer "would_change" -- a project
        # nothing has touched since the last pass cannot have a different
        # answer, so check a cheap (count, max updated_at) signature
        # FIRST and skip the scan entirely when it has not moved.
        try:
            from prism_service.project_context import get_project
            fp = get_project(project).task_svc.text_fingerprint()
        except Exception:
            fp = None
        if fp is not None:
            if _LAST_FINGERPRINT.get(project) == fp:
                return {"skipped": "unchanged since last pass"}
            _LAST_FINGERPRINT[project] = fp

    fields = behavior.get("fields") or language_alignment.DEFAULT_FIELDS
    batch_size = behavior.get("batch_size") or language_alignment.DEFAULT_BATCH_SIZE
    mode = behavior.get("mode") or "apply"

    dry = language_alignment.align_language(
        project, apply=False, fields=fields, batch_size=batch_size)
    n = dry.get("would_change", 0)
    if n == 0:
        return {"skipped": "nothing to align"}

    before_rules = _rule_counts(project)

    from prism_service.project_context import get_project
    task_svc = get_project(project).task_svc
    run_task = task_svc.create(
        title=f"Align language in {n} loose tasks",
        description=(
            f"Bring the free text of {n} tasks into plain Simplified "
            f"Technical English. The mode is {mode}. The align step "
            "writes through TaskService.update, so every fix carries "
            "its own audit row."
        ),
        channel="daemon", tags=["align-language", "daemon"],
        parent_id="", workflow="align_language",
    )

    from prism_service.api import conductor_flow as flow

    started = flow.flow_start(
        flow.Ident(task_id=run_task.id, session_id=SEAT_ID), project=project)
    if not started.get("ok"):
        return {"ok": False, "run_task_id": run_task.id,
                "reason": started.get("error") or "flow_start refused"}

    def _report(step_id: str, proof: dict) -> bool:
        task_svc.update(run_task.id,
                        completion_proof=json.dumps(proof, indent=2))
        res = flow.flow_report(flow.Ident(
            task_id=run_task.id, session_id=SEAT_ID, outcome="pass",
            expected_step=step_id), project=project)
        return bool(res.get("ok"))

    _report("collect", dry)

    if mode == "apply":
        align_report = language_alignment.align_language(
            project, apply=True, fields=fields, batch_size=batch_size)
    else:
        align_report = dry
    _report("align", align_report)

    after_rules = _rule_counts(project)
    confirm = language_alignment.align_language(
        project, apply=False, fields=fields, batch_size=batch_size)
    verify_report = {
        "before": before_rules, "after": after_rules,
        "second_dry_run_would_change": confirm.get("would_change", 0),
    }
    _report("verify", verify_report)

    # "done" (models/workflow.py ALIGN_LANGUAGE_STEPS) is not a gate --
    # nothing else closes the task once the flow reaches it, so this
    # worker finishes the job itself, the same way a green_gate approve
    # finishes an implement-workflow task (conductor_service.gate_decide).
    task_svc.update(run_task.id, status="done", full_outcome_complete=True)
    task_svc.record_history(
        run_task.id, action="language_alignment_worker_done",
        details=(f"mode={mode}; touched="
                 f"{align_report.get('changed', align_report.get('would_change', 0))}"),
        actor=SEAT_ID)

    return {"run_task_id": run_task.id, "report": align_report}


def _loop(interval_s: int, stop_event: Optional[threading.Event] = None) -> None:
    from prism_service.services import wakeups

    _log(f"started; interval={interval_s}s (event-driven; falls back to "
         f"{interval_s}s when nothing changed)")
    wakeups.lower_thread_priority()
    if stop_event is None:  # never delay a test-driven loop
        wakeups.wait_out_startup_warmup()
    while stop_event is None or not stop_event.is_set():
        # Captured fresh, right before this iteration's own sweep -- NOT
        # carried over from the timestamp the PREVIOUS iteration's wait()
        # already consumed. Using a stale prior baseline here double-fires:
        # the signal that woke the previous wait() is still the newest
        # entry in wakeups._LAST, so a since= older than it (e.g. the
        # previous sweep's own start time) sees it as "new" all over again
        # and triggers an extra, unwanted pass immediately after this one.
        sweep_started = time.time()
        for project in _projects_in_scope():
            try:
                # Serialized with the other pure-maintenance sweeps (see
                # dispatch_guard._loop's comment) -- never task_runner/
                # resume_actuator/ship_worker/gate_adjudicator.
                with wakeups.serial_slot():
                    with system_activity.pass_(
                            "language_alignment", project, "run_once_for") as info:
                        res = run_once_for(project)
                        info["active"] = "skipped" not in res
                if "skipped" not in res:
                    _log(f"{project}: {res}")
            except Exception as exc:
                _log(f"{project}: run_once_for raised: {exc}")
        if stop_event is not None:
            # Test-only plumbing (never passed by
            # start_language_alignment_worker): keep the plain interval
            # wait here so a stop_event-driven test still ends the loop
            # the way it always has.
            if stop_event.wait(timeout=interval_s):
                break
        else:
            # Owner 2026-09-13: a reactive suite has no clock of its own --
            # a task edit (task_changed) is what makes text worth
            # realigning, so this worker wakes on that signal instead of
            # ticking `interval_s` whether or not anything changed. The
            # interval survives only as the fallback ceiling. `since=
            # sweep_started` (this iteration's own pre-sweep timestamp,
            # not a value left over from a previous iteration) is what
            # keeps one signal to exactly one extra pass.
            wakeups.wait(["task_changed"], timeout=interval_s, since=sweep_started)


def start_language_alignment_worker() -> Optional[threading.Thread]:
    """Spawn the align-language daemon thread, unless disabled (default).
    Mirrors start_task_runner/start_gate_adjudicator: same shape, same
    off-by-default posture."""
    if not is_enabled():
        _log("disabled (default OFF; set PRISM_LANGUAGE_ALIGNMENT_WORKER=on "
             "to opt this environment in)")
        return None
    t = threading.Thread(
        target=_loop, args=(_interval_s(),),
        name="prism-language-alignment-worker", daemon=True,
    )
    t.start()
    return t
