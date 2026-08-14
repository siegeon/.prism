"""Gate-scoped evidence: the Decision packet's Visual evidence Row re-scoped
per gate, not the same undifferentiated pile at every step.

Owner confusion recorded on task 61261201: "the old images were under the
old gate for the failing stuff" — at green_gate the panel showed the same
flat pile as at red_gate. This task re-scopes the EXISTING "Visual evidence"
Row (never adds a new one — the epic's own stop_if): red_gate leads with the
recorded BEFORE artifact expanded, green_gate leads with AFTER, and
everything else collapses under a labeled History disclosure, still one
click away. story_gate/plan_gate/no-step keep today's flat render. Absorbed
from sibling 6fe2fec3: a red_gate + proof_type=="demo" + a present before
artifact renders one explainer line that the red state IS the before
artifact, not a failing test suite.

AC-1/AC-2 pin the server-side classification + proof_type echo. AC-3..AC-7
pin the TSX render guard by parsing the ACTUAL SOURCE of DecisionPacket.tsx
(this SPA has no JS test runner, per CLAUDE.md and
test_conductor_page_animated_cleanup_ui.py:4-6) with comment lines stripped
first and the enclosing JS/JSX expression extracted by BRACE DEPTH, never a
fixed character window (convention verbatim from
test_decision_packet_gate_subject.py:97-121 /
test_gate_banner_honest_state_ui.py:51-67).
"""
from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve()
_SRC = _HERE.parent.parent.parent / "prism_service" / "web" / "src"
_DECISION_PACKET_TSX = _SRC / "components" / "plan" / "DecisionPacket.tsx"


# ---------------------------------------------------------------------------
# AC-1 - server-side filename -> phase classification, never drops a file
# ---------------------------------------------------------------------------

def _shots_from(tmp_path, monkeypatch, names):
    """Real files on disk under tmp_path, run through the real
    _screenshots() classifier (evidence_dir monkeypatched to tmp_path)."""
    from prism_service.services import decision_packet
    for n in names:
        (tmp_path / n).write_bytes(b"\x89PNG\r\n")
    monkeypatch.setattr(decision_packet, "evidence_dir", lambda tid: tmp_path)
    return decision_packet._screenshots("any-task-id")


@pytest.mark.parametrize("name,expected_phase", [
    ("task_page_before.png", "before"),
    ("task_page_after.png", "after"),
    ("livebar_ac8_before.png", "before"),
    ("livebar_ac8_after.png", "after"),
    ("02_connectors_before.png", "before"),
    ("trace_696cacf5_after.png", "after"),
])
def test_classifies_known_before_after_filenames(tmp_path, monkeypatch, name, expected_phase):
    shots = _shots_from(tmp_path, monkeypatch, [name])
    assert shots[0]["phase"] == expected_phase


def test_unrecognized_filename_defaults_to_other(tmp_path, monkeypatch):
    shots = _shots_from(tmp_path, monkeypatch, ["design_packet_card.png"])
    assert shots[0]["phase"] == "other"


def test_never_drops_a_file_it_cannot_cleanly_classify(tmp_path, monkeypatch):
    """The two real trap filenames named in this task's likely_misfire: the
    before/after TOKEN is present but describes a fix/approve action, not a
    capture phase. Whatever phase they land in, they must still come back —
    never silently dropped."""
    names = [
        "remote_landed_on_default_before_fix.png",
        "plan_gate_released_after_approve.png",
        "task_page_before.png",
        "task_page_after.png",
        "design_packet_card.png",
    ]
    shots = _shots_from(tmp_path, monkeypatch, names)
    assert len(shots) == len(names)
    returned_names = {s["name"] for s in shots}
    assert returned_names == set(names)
    for s in shots:
        assert s["phase"] in ("before", "after", "other")


# ---------------------------------------------------------------------------
# AC-2 - assemble_packet echoes task.proof_type additively
# ---------------------------------------------------------------------------

def _packet(task, monkeypatch):
    from prism_service.services import decision_packet, task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for", lambda tid: None)
    monkeypatch.setattr(decision_packet, "_receipt", lambda p, t: None)
    monkeypatch.setattr(decision_packet, "_screenshots", lambda t: [])
    return decision_packet.assemble_packet("prism", "72551ef0", task)


def _base_task(**overrides):
    fields = dict(workflow_step="red_gate", gate_state="pending",
                  status="in_progress", gate_reason="",
                  plan_doc="", plan_diagram="", proof_type="")
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_proof_type_echoed_for_demo_task(monkeypatch):
    task = _base_task(proof_type="demo")
    pkt = _packet(task, monkeypatch)
    assert pkt["proof_type"] == "demo"


def test_proof_type_empty_when_unset(monkeypatch):
    task = _base_task(proof_type="")
    pkt = _packet(task, monkeypatch)
    assert pkt["proof_type"] == ""


def test_existing_keys_still_present_additive_only(monkeypatch):
    """This slice must not rename/remove any of the pre-existing keys,
    including 31c345b7's additive 'subject' key."""
    task = _base_task()
    pkt = _packet(task, monkeypatch)
    for key in ("state", "diff_stat", "commits", "receipt", "screenshots",
                "subject", "proof_type"):
        assert key in pkt


# ---------------------------------------------------------------------------
# TSX source helpers (verbatim convention, see module docstring)
# ---------------------------------------------------------------------------

def _strip_comments(src: str) -> str:
    no_block = re.sub(r"/\*[\s\S]*?\*/", "", src)
    no_line = re.sub(r"//[^\n]*", "", no_block)
    return no_line


def _extract_braced_expr(src: str, marker: str) -> str:
    """Return the `{...}` expression starting at the first `{` at/after
    `marker`, matched by BRACE DEPTH, never a fixed character window."""
    idx = src.index(marker)
    start = src.index("{", idx)
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces scanning forward from marker {marker!r}")


def _function_body(src: str, fn_marker: str) -> str:
    """Return a function's `{...}` body starting at the FIRST '{' after the
    function's own name marker line ends (skips any inline object-typed
    params/return-type braces by requiring the marker to already include the
    full signature up to the body-opening newline convention used here)."""
    idx = src.index(fn_marker)
    start = src.index("{", idx)
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces scanning forward from marker {fn_marker!r}")


@pytest.fixture(scope="module")
def tsx_src() -> str:
    return _strip_comments(_DECISION_PACKET_TSX.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# AC-3 / AC-4 - leadAndRest keys red_gate->before, green_gate->after
# ---------------------------------------------------------------------------

def test_lead_and_rest_maps_red_gate_to_before_and_green_gate_to_after(tsx_src):
    assert "function leadAndRest(" in tsx_src, (
        "expected a leadAndRest(shots, step) helper computing the lead "
        "artifact and the History remainder")
    body = _function_body(tsx_src, "function leadAndRest(")
    assert '"red_gate"' in body
    assert '"before"' in body
    assert '"green_gate"' in body
    assert '"after"' in body
    # AC-5: any other step must not get a lead (null default).
    assert re.search(r':\s*null', body), (
        "a step other than red_gate/green_gate must fall through to no lead")


# ---------------------------------------------------------------------------
# AC-3 / AC-4 / AC-5 - the Row's render: lead expanded, rest under History,
# unscoped flat fallback for any other step
# ---------------------------------------------------------------------------

def test_row_leads_with_lead_and_collapses_rest_under_history(tsx_src):
    expr = _extract_braced_expr(tsx_src, "{lead ? (")
    assert "<details" in expr, "the rest must collapse under a native disclosure"
    assert "History" in expr, "the disclosure must be labeled History"
    assert "rest.length > 0" in expr, (
        "History must only render when there is something to collapse")
    assert "rest.map" in expr, "the collapsed items must come from rest"
    assert "lead.url" in expr and "lead.name" in expr, (
        "the leading artifact must be rendered from the lead item")


def test_row_falls_back_to_flat_render_for_non_gate_steps(tsx_src):
    expr = _extract_braced_expr(tsx_src, "{lead ? (")
    # AC-5: the false branch is the pre-existing unscoped render, fed
    # straight from pkt.screenshots (never a locally-scoped alias).
    assert "pkt.screenshots.map" in expr
    assert "EvidenceGallery" in expr


def test_visual_evidence_row_still_exists_and_row_count_unchanged(tsx_src):
    """AC-7: no new Row is added — re-scoping happens entirely inside the
    existing Visual evidence Row. Baseline confirmed against the tree as of
    sibling 31c345b7's merge (subject + diff + commits + oracle receipt +
    visual evidence = 5)."""
    assert 'title="Visual evidence"' in tsx_src
    assert tsx_src.count("<Row") == 5


# ---------------------------------------------------------------------------
# AC-6 - demo-red explainer line: guarded by BOTH red_gate + proof_type
# demo AND a present lead (before) artifact
# ---------------------------------------------------------------------------

def test_demo_red_line_guard_requires_step_and_proof_type_and_lead(tsx_src):
    assert "showDemoRedLine" in tsx_src, (
        "expected a named guard so the demo-red condition is not buried "
        "inline and unreadable")
    m = re.search(r"const\s+showDemoRedLine\s*=\s*([^;]+);", tsx_src)
    assert m, "showDemoRedLine must be a plain const assignment"
    guard = m.group(1)
    assert '"red_gate"' in guard, "must be scoped to red_gate only"
    assert '"demo"' in guard, "must require proof_type === demo"
    assert "lead" in guard, "must require a present lead (before) artifact"


def test_demo_red_line_only_rendered_when_guard_is_true(tsx_src):
    expr = _extract_braced_expr(tsx_src, "{lead ? (")
    assert "showDemoRedLine &&" in expr, (
        "the explainer line must be gated on the named guard, not always "
        "rendered inside the lead branch")


def test_demo_red_line_absent_from_the_flat_fallback_branch(tsx_src):
    """The explainer only makes sense once a lead exists (red_gate); the
    non-scoped fallback branch (story_gate/plan_gate/other) must not carry
    it at all."""
    expr = _extract_braced_expr(tsx_src, "{lead ? (")
    # Split on the ternary's ' : ' at depth 1 is fragile; instead assert the
    # fallback's own render (pkt.screenshots.map) occurs strictly AFTER the
    # last mention of showDemoRedLine, i.e. the guard precedes and does not
    # re-occur alongside the flat render.
    demo_idx = expr.rindex("showDemoRedLine")
    fallback_idx = expr.rindex("pkt.screenshots.map")
    assert demo_idx < fallback_idx


# ---------------------------------------------------------------------------
# AC-2 (client type) - the Packet type carries phase + proof_type so the
# component can key on real data, not a client-side guess
# ---------------------------------------------------------------------------

def test_packet_type_carries_phase_and_proof_type(tsx_src):
    type_block = tsx_src[tsx_src.index("type Packet"):tsx_src.index("type Packet") + 1200]
    assert '"before"' in type_block and '"after"' in type_block and '"other"' in type_block
    assert "proof_type" in type_block
