"""Machine-written task text respects the ontology (task 938b0a2d).

Owner finding on task 8582921d: the reachability tooth wrote a
blocked_reason full of raw jargon and file paths, with no STE pass and
no ontology link -- "something in the system generated this content but
did not respect the ontology." Human-written task text already aligns
at write (services.ste + services.lexicon, wired through
TaskService._apply_ste) and links to the ontology on render
(entity_linker + LinkedText / spliceLinkedMarkdown). Machine-written
text -- blocked_reason (the reachability tooth, ship_worker, the
resume actuator) and gate_reason (every gate seat) -- must go through
the same two pipes.

Four things this file pins:

1. TaskService aligns blocked_reason and gate_reason the same way it
   aligns description: ste.normalize, then lexicon.align, with the
   ste_normalise history row carrying the pre-alignment text.
2. reachability_check's rewritten message is itself clean STE (flavored
   mode, zero findings) and lists one unreachable symbol per line.
3. GET /api/tasks/{id}/links returns spans for blocked_reason (task
   938b0a2d extends _LINK_FIELDS).
4. TaskDetailPage.tsx renders both fields through LinkedText, the same
   component likely_misfire already uses for plain (non-markdown) task
   text, so a linked entity is clickable wherever the field renders.

No model call anywhere in this file. Every assertion is a plain string,
list, or JSX-source comparison against a fixed input.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import ste  # noqa: E402

_TSX = _SERVICE_ROOT / "prism_service" / "web" / "src" / "pages" / "TaskDetailPage.tsx"


def _mk_service(tmp_path):
    from prism_service.services.task_service import TaskService

    return TaskService(str(tmp_path / "tasks.db"))


# ---------------------------------------------------------------------------
# (1) TaskService aligns blocked_reason and gate_reason like every other
# flavored field: normalize, then lexicon.align, before/after recorded.
# ---------------------------------------------------------------------------


def test_update_aligns_blocked_reason_and_records_before(tmp_path):
    svc = _mk_service(tmp_path)
    task = svc.create(title="Wiring check")

    text = "don't ship; the ticket is unwired"
    updated = svc.update(task.id, blocked_reason=text)

    # contraction (don't -> do not), semicolon (-> sentence break), then
    # the lexicon synonym "ticket" -> the canonical label "Task".
    assert updated.blocked_reason == "do not ship. The Task is unwired", (
        updated.blocked_reason)

    assert "blocked_reason" in svc.last_style["fixed"]
    assert set(svc.last_style["fixed"]["blocked_reason"]) == {
        "contraction", "semicolon", "lexicon"}
    assert {"field": "blocked_reason", "from": "ticket", "to": "Task"} \
        in svc.last_style["aligned"]

    rows = svc.history(task.id)
    normalise_rows = [h for h in rows if h.action == "ste_normalise"]
    assert normalise_rows, [h.action for h in rows]
    details = normalise_rows[-1].details
    assert "rules=" in details and "before=" in details
    before = json.loads(details.split("before=", 1)[1])
    assert before.get("blocked_reason") == text


def test_update_aligns_gate_reason_and_records_before(tmp_path):
    svc = _mk_service(tmp_path)
    task = svc.create(title="Wiring check")

    text = "don't ship; the ticket is unwired"
    updated = svc.update(task.id, gate_reason=text)

    assert updated.gate_reason == "do not ship. The Task is unwired", (
        updated.gate_reason)

    assert "gate_reason" in svc.last_style["fixed"]
    assert set(svc.last_style["fixed"]["gate_reason"]) == {
        "contraction", "semicolon", "lexicon"}
    assert {"field": "gate_reason", "from": "ticket", "to": "Task"} \
        in svc.last_style["aligned"]

    rows = svc.history(task.id)
    normalise_rows = [h for h in rows if h.action == "ste_normalise"]
    assert normalise_rows, [h.action for h in rows]
    details = normalise_rows[-1].details
    before = json.loads(details.split("before=", 1)[1])
    assert before.get("gate_reason") == text


def test_gate_reason_readers_keep_their_literal_tokens_through_alignment():
    """decision_packet.packet_state matches "rewound"/"rewind"/"recover"/
    "waiv"/"override" as lowercase substrings, conductor_service checks
    for the '⚠' marker, and api/conductor.py's settled-gate branch
    parses "tree=<sha>" with a regex straight off the stored gate_reason
    -- none of these live in ste's contraction/filler/nominalisation/
    phrasal-verb/marketing tables or in the ontology lexicon, so
    aligning gate_reason must never rewrite them."""
    from prism_service.services import lexicon

    # No semicolon here on purpose: the semicolon rule uppercases the
    # single letter right after it, and this test cares about the exact
    # lowercase substrings the readers check for, not that unrelated
    # rule.
    raw = ("manual override -- tree=abc123def -- rewound, then a "
           "recover, then waived, flagged with ⚠ by the owner")
    fixed, _rules = ste.normalize(raw, mode="flavored")
    aligned, _applied = lexicon.align(fixed)

    for token in ("override", "tree=abc123def", "rewound", "recover",
                  "waived", "⚠"):
        assert token in aligned, (token, aligned)


# ---------------------------------------------------------------------------
# (2) reachability_check's rewritten message is itself clean STE.
# ---------------------------------------------------------------------------


def _git(args, cwd):
    return subprocess.run(["git"] + args, cwd=str(cwd),
                          capture_output=True, text=True)


def _init_repo(tmp_path):
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(["add", "README.md"], tmp_path)
    _git(["commit", "-q", "-m", "baseline"], tmp_path)
    return _git(["rev-parse", "HEAD"], tmp_path).stdout.strip()


def _write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_reachability_message_is_ste_clean_with_two_violations(tmp_path):
    from prism_service.services.reachability_check import (
        unreachable_entry_point_reason_for_diff)

    baseline = _init_repo(tmp_path)
    _write(tmp_path / "pkg" / "a.py",
          "class OntologyStore:\n"
          "    def is_empty(self):\n"
          "        return True\n")
    _write(tmp_path / "pkg" / "b.py",
          "class OntologyStore:\n"
          "    def list_axioms(self):\n"
          "        return []\n")

    reason = unreachable_entry_point_reason_for_diff(tmp_path, baseline)
    assert reason, "two new unreachable entry points must be refused"
    assert reason.startswith("Unreachable entry points:\n"), reason

    bullets = [ln for ln in reason.splitlines() if ln.startswith("- ")]
    assert len(bullets) == 2, reason
    assert any("is_empty" in b and "a.py" in b for b in bullets), reason
    assert any("list_axioms" in b and "b.py" in b for b in bullets), reason

    findings = ste.check(reason, mode="flavored")
    assert findings == [], findings


# ---------------------------------------------------------------------------
# (3) GET /api/tasks/{id}/links returns spans for blocked_reason too.
# ---------------------------------------------------------------------------


TASK_TITLE = "Slack triage support flow"


@pytest.fixture
def linked_project():
    from prism_service.services.ontology_graph import OntologyGraph

    pid = f"machine-text-onto-{uuid.uuid4().hex[:8]}"
    rows = {
        "channels": [], "agents": [], "providers": [],
        "tasks": [{"id": "22222222-3333-4444-5555-666666666666",
                   "title": TASK_TITLE, "channel": "ui"}],
        "signals": [], "documents": [], "code_kinds": [], "memories": [],
    }
    OntologyGraph(pid).rebuild(rows=rows, agent_descriptions={}, signal_arrived_at={})
    return pid


def test_links_carries_spans_for_blocked_reason(linked_project):
    from prism_service.api import tasks as tasks_api
    from prism_service.project_context import get_project

    ctx = get_project(linked_project)
    t = ctx.task_svc.create(title="carrier", channel="ui")
    ctx.task_svc.update(
        t.id, blocked_reason=f"Blocked on {TASK_TITLE} finishing first.")

    out = tasks_api.get_task_links(t.id, project=linked_project)
    assert "blocked_reason" in out["fields"]
    texts = {s["text"] for s in out["fields"]["blocked_reason"]}
    assert TASK_TITLE in texts, texts


def test_links_carries_spans_for_gate_reason(linked_project):
    from prism_service.api import tasks as tasks_api
    from prism_service.project_context import get_project

    ctx = get_project(linked_project)
    t = ctx.task_svc.create(title="carrier", channel="ui")
    ctx.task_svc.update(
        t.id, gate_reason=f"Approve waits on {TASK_TITLE}.")

    out = tasks_api.get_task_links(t.id, project=linked_project)
    assert "gate_reason" in out["fields"]
    texts = {s["text"] for s in out["fields"]["gate_reason"]}
    assert TASK_TITLE in texts, texts


# ---------------------------------------------------------------------------
# (4) TaskDetailPage.tsx renders blocked_reason/gate_reason through
# LinkedText, the same component likely_misfire already uses for plain
# (non-markdown) task text.
# ---------------------------------------------------------------------------


def _strip_comments(src: str) -> str:
    """Drop /* */, {/* */} and // comments so a comment can never satisfy
    a source assertion (repo convention, tests/unit/test_cancelled_task_
    gate_card_inert.py:51-63)."""
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(?<!:)(?<!\\)//.*$", "", src)
    return src


def _read_tsx() -> str:
    assert _TSX.exists(), f"{_TSX} missing"
    return _strip_comments(_TSX.read_text(encoding="utf-8"))


def test_blocked_reason_renders_through_linked_text():
    src = _read_tsx()
    hits = src.count("<LinkedText text={task.blocked_reason}")
    assert hits >= 2, (
        "blocked_reason must render through LinkedText in every card it "
        f"appears in, found {hits} call site(s)")
    # No raw JSX-child interpolation left behind -- that would skip the
    # ontology cross-link this task adds.
    assert ">{task.blocked_reason}<" not in src
    assert not re.search(r"(?m)^\s*\{task\.blocked_reason\}\s*$", src)


def test_gate_reason_renders_through_linked_text():
    src = _read_tsx()
    hits = src.count("<LinkedText text={task.gate_reason}")
    assert hits >= 2, (
        "gate_reason must render through LinkedText in every card it "
        f"appears in, found {hits} call site(s)")
    assert ">{task.gate_reason}<" not in src
    assert not re.search(r"(?m)^\s*\{task\.gate_reason\}\s*$", src)


def test_linked_text_is_imported():
    src = _read_tsx()
    assert 'import LinkedText from "@/components/LinkedText";' in src
