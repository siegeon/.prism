"""A task PRISM creates names its own channel (task 02264017).

The ontology rule task-names-its-channel stays at 0 violations only if
every INTERNAL creation path stamps the channel a row arrived on. Three
creators omit it today: rule_decisions.py fix action, api/workflows.py
repair task, task_runner.py stall-split child.

Pins:
  AC-1/AC-2 test_a_fix_decision_task_names_the_ontology_channel --
       decide(action="fix") on a real ontology rule signal creates a
       task whose STORED row carries channel == "ontology" ==
       signal.channel next to the signal's own channel_ref.
  AC-3/AC-4 test_no_internal_creator_leaves_the_channel_blank -- an AST
       walk over prism_service/ finds every task_svc.create call site
       outside the task service itself and asserts each passes a
       channel keyword; api/workflows.py must pass the literal
       "daemon". The walk resolves the receiver from the AST, so a
       comment cannot satisfy it, and it names each blank site it finds.
"""

from __future__ import annotations

import ast
import sqlite3
import sys
import uuid
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_PKG_ROOT = _SERVICE_ROOT / "prism_service"

RULE = "no-artifacts-in-the-root"


def _project_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _seed_two_loose_docs(pid: str) -> None:
    """Same fixture posture as test_rule_decisions_exempt_keeps_reason:
    two loose root docs so no-artifacts-in-the-root fires."""
    from prism_service.config import project_data_dir
    from prism_service.project_context import get_project

    get_project(pid)
    brain_db = project_data_dir(pid) / "brain.db"
    conn = sqlite3.connect(str(brain_db))
    conn.execute("CREATE TABLE docs (id TEXT PRIMARY KEY, source_file TEXT)")
    conn.execute("INSERT INTO docs VALUES ('d1', 'README.md')")
    conn.execute("INSERT INTO docs VALUES ('d2', 'CLAUDE.md')")
    conn.commit()
    conn.close()


def _rule_signal(pid: str):
    from prism_service.services.signal_store import SignalStore

    return next(s for s in SignalStore(pid).list(limit=500)
                if s.channel == "ontology" and s.channel_ref == f"rule:{RULE}")


# ---------------------------------------------------------------------------
# AC-1 / AC-2
# ---------------------------------------------------------------------------

def test_a_fix_decision_task_names_the_ontology_channel():
    from prism_service.project_context import get_project
    from prism_service.services import ontology_prototype_projection as proj
    from prism_service.services import rule_decisions

    pid = _project_id("fix-channel")
    _seed_two_loose_docs(pid)
    proj.rebuild(pid)
    signal = _rule_signal(pid)
    assert signal.channel == "ontology"  # decide() precondition, and AC-2's bar

    result = rule_decisions.decide(pid, signal, "fix", "owner clicked Fix")
    assert result["action"] == "fix"

    task = get_project(pid).task_svc.get(result["task"]["id"])
    assert task is not None
    # AC-1: the fix task arrived on the ontology channel.
    assert task.channel == "ontology", (
        f"fix task {task.id} carries channel={task.channel!r}; the channel "
        "records HOW a row arrived, and this one arrived from the ontology queue"
    )
    # AC-2: channel and channel_ref both come from the signal, together.
    assert task.channel == signal.channel
    assert task.channel_ref == signal.channel_ref


# ---------------------------------------------------------------------------
# AC-3 / AC-4
# ---------------------------------------------------------------------------

_RECEIVERS = {"task_svc", "_task_svc", "task_service", "_task_service"}


def _receiver_name(func: ast.expr) -> str:
    """The attribute/name a .create() call is made on, e.g. 'task_svc' for
    get_project(p).task_svc.create(...) or self._task_svc.create(...)."""
    if not (isinstance(func, ast.Attribute) and func.attr == "create"):
        return ""
    inner = func.value
    if isinstance(inner, ast.Attribute):
        return inner.attr
    if isinstance(inner, ast.Name):
        return inner.id
    return ""


def _creation_sites() -> list[tuple[str, int, ast.Call]]:
    sites = []
    for path in sorted(_PKG_ROOT.rglob("*.py")):
        rel = path.relative_to(_PKG_ROOT).as_posix()
        if rel == "services/task_service.py":
            continue  # the service itself, not a caller
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and _receiver_name(node.func) in _RECEIVERS):
                sites.append((rel, node.lineno, node))
    return sites


def test_no_internal_creator_leaves_the_channel_blank():
    sites = _creation_sites()
    # The plan's survey found 8 call sites; fewer means the walk went blind.
    assert len(sites) >= 8, [f"{f}:{n}" for f, n, _ in sites]

    blank = [f"{f}:{n}" for f, n, call in sites
             if not any(kw.arg == "channel" for kw in call.keywords)]
    assert not blank, (
        "internal task creators that pass no channel= keyword "
        f"(the row arrives with a blank channel and the rule fires): {blank}"
    )

    # AC-3: the workflow-step repair task names the daemon channel, literally.
    wf = [call for f, _n, call in sites if f == "api/workflows.py"]
    assert wf, "no task_svc.create call found in api/workflows.py"
    values = [kw.value.value for call in wf for kw in call.keywords
              if kw.arg == "channel" and isinstance(kw.value, ast.Constant)]
    assert values == ["daemon"] * len(wf), (
        f"api/workflows.py repair task must pass channel='daemon', got {values}"
    )
