"""UI contract tests for "Gate panel stops contradicting itself on evidence"
(task 5a6837a0).

The PRISM SPA has NO JS test runner, so UI-FIRST acceptance criteria are
pinned by asserting the ACTUAL web source (TSX) — the same convention as
tests/unit/test_conductor_page_animated_cleanup_ui.py:4-6.

THE DEFECT these pin: the gate panel renders TWO independent receipt
lookups side by side and neither names WHICH receipt it is.
  * DecisionPacket.tsx:115 "Oracle receipt" reads the packet's
    `receipt`, which is literally oracle_spec.latest_receipt() —
    the newest receipt on the task, whatever gate, whatever tree
    (services/decision_packet.py:86-98).
  * TaskDetailPage.tsx:1455-1475 "oracle receipt · <adapter>" reads
    GET /api/conductor/gate/readiness, which for a red_gate resolves a
    receipt pinned to the RED anchor commit + red spec hash
    (api/conductor.py:102-163).
On task 89e90d1a's red_gate the first says "passed" while the second says
"missing". Both are TRUE; the panel presents them as one contradictory
fact. Remedy (precedent mx-352a3d / v7.1.24): NAME the source — adapter,
short sha, and which gate it serves — and say in ONE line why they differ.
Deleting a row is the recorded likely_misfire, so deletion is pinned
against too.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parent.parent.parent  # services/prism-service
_SRC = _ROOT / "prism_service" / "web" / "src"
_TDP = _SRC / "pages" / "TaskDetailPage.tsx"
_PACKET = _SRC / "components" / "plan" / "DecisionPacket.tsx"
_PLANVIEW = _SRC / "components" / "plan" / "PlanView.tsx"
_READINESS = _ROOT / "prism_service" / "api" / "conductor.py"
_VERSION = _ROOT / "prism_service" / "__version__.py"


def _read(p: Path) -> str:
    assert p.is_file(), f"missing source file: {p}"
    return p.read_text(encoding="utf-8")


def _window(src: str, marker: str, before: int = 400, after: int = 2200) -> str:
    """The slice of source around `marker` — assertions stay LOCAL to the row
    under test, so an unrelated occurrence elsewhere can never fake a pass."""
    i = src.find(marker)
    assert i >= 0, f"marker not found in source: {marker!r}"
    return src[max(0, i - before): i + after]


def _short_sha_render(chunk: str) -> bool:
    """A short-sha is actually RENDERED here (…slice(0, 7) / (0, 12) …)."""
    return bool(re.search(r"\.slice\(\s*0\s*,\s*(7|8|10|12)\s*\)", chunk))


# ---------------------------------------------------------------------------
# 1 · Each row NAMES which receipt it reflects (adapter + sha + which gate)
# ---------------------------------------------------------------------------

def test_check_row_names_the_gate_and_anchor_it_reflects() -> None:
    """The CHECK row is the READINESS receipt: pinned to the gate under
    review and (for a red gate) to the red anchor commit. It must say so —
    'oracle receipt · <adapter>' alone is what makes it look like the same
    fact as the packet row."""
    chunk = _window(_read(_TDP), "oracle receipt · ", before=200, after=1800)
    assert "serves" in chunk, (
        "the check row must name WHICH gate its receipt serves "
        "(e.g. 'serves red_gate') — see api/conductor.py:102-163")
    assert ("workflow_step" in chunk or "stepLabel(" in chunk), (
        "the gate name must be interpolated from the live task, not hard-coded")
    assert _short_sha_render(chunk), (
        "the check row must render the SHORT SHA of the tree/anchor the "
        "readiness receipt was measured at (the mx-352a3d remedy: name the "
        "tree so the fact is checkable)")


def test_packet_receipt_row_names_which_receipt_it_is() -> None:
    """The DECISION PACKET row is `latest_receipt()` — the newest receipt on
    the task, NOT necessarily this gate's. It must scope itself and render
    the sha it was measured at."""
    chunk = _window(_read(_PACKET), 'title="Oracle receipt"', before=600, after=1600)
    assert "latest" in chunk.lower(), (
        "the packet row must scope itself as the LATEST receipt on the task "
        "(services/decision_packet.py:86-98), not an unqualified 'Oracle "
        "receipt' that reads as the gate's verdict")
    assert "gate" in chunk.lower(), (
        "the packet row must say which gate (if any) its receipt serves")
    assert _short_sha_render(chunk), (
        "the packet row must render the short sha of the tree its receipt "
        "was measured at")


def test_panel_feeds_the_packet_the_receipt_context_it_lacks() -> None:
    """The packet fetches its own receipt and knows nothing about the gate.
    The panel already holds both (`testReceipt` = latest receipt incl.
    tree_sha from /api/tasks/:id/tests, `gateReadiness` = the gate's) — it
    must hand that context down instead of leaving the row unlabelled."""
    src = _read(_TDP)
    m = re.search(r"<DecisionPacket[\s\S]{0,600}?/>", src)
    assert m, "the gate panel must still render <DecisionPacket …/>"
    assert "testReceipt" in m.group(0), (
        "pass the latest-receipt context (tree_sha/job_id) into DecisionPacket "
        "so its row can name the receipt it is showing")


# ---------------------------------------------------------------------------
# 2 · A divergence is EXPLAINED in one actionable line, never silent
# ---------------------------------------------------------------------------

def test_panel_states_why_the_two_rows_differ_in_one_line() -> None:
    src = _read(_TDP)
    assert re.search(r"diverg", src, re.I), (
        "the panel must COMPUTE whether the packet receipt and the gate "
        "readiness receipt are the same receipt (a named divergence flag)")
    assert re.search(r"different receipt", src, re.I), (
        "when they differ the panel must say so in one line the owner can "
        "act on — a silent contradiction is the defect")


def test_the_divergence_line_is_conditional_not_always_on() -> None:
    """Two rows agreeing must NOT be narrated as a divergence — the line is
    rendered off the computed flag."""
    src = _read(_TDP)
    m = re.search(r"(\w*[Dd]iverg\w*)\s*&&", src)
    assert m, ("the divergence copy must render behind the divergence flag "
               "(`{…diverg… && (…)}`), not unconditionally")


# ---------------------------------------------------------------------------
# 3 · The recorded likely_misfire: neither row may be deleted or hidden
# ---------------------------------------------------------------------------

def test_packet_oracle_receipt_row_survives() -> None:
    pkt = _read(_PACKET)
    assert 'title="Oracle receipt"' in pkt, (
        "deleting the packet's receipt row loses the REAL passing receipt — "
        "the recorded likely_misfire for this task")
    assert "pkt.receipt" in pkt, "the packet row must still read the receipt"


def test_check_row_keeps_the_honest_missing_signal() -> None:
    # after=3200 was stale: later UI additions (the design-packet / visual-
    # evidence blocks above this row) pushed the literal "missing" string to
    # 3835 chars past the anchor -- widened with margin, not shrunk to fit,
    # so the next addition doesn't silently reopen the same gap.
    chunk = _window(_read(_TDP), "the gate's evidence tooth", before=1400, after=4200)
    assert '"missing"' in chunk, (
        "hiding the honest 'missing' RED-receipt signal to stop the "
        "contradiction is the recorded likely_misfire")
    assert "gateReadiness?.receipt_ok" in chunk, (
        "the check row must still reflect the LIVE readiness tooth")


# ---------------------------------------------------------------------------
# 4 · Readiness copy: honest in BOTH directions (pinned, v7.1.25 behaviour)
# ---------------------------------------------------------------------------

def test_readiness_says_so_when_no_machine_seat_can_decide() -> None:
    api = _read(_READINESS)
    assert "NO machine sweep is coming" in api, (
        "a gate no machine seat is eligible to decide must say so "
        "(api/conductor.py:127-157)")
    assert "distinct actor's decision" in api
    assert "no owner action needed" not in _window(
        api, "NO machine sweep is coming", before=200, after=400), (
        "the no-seat branch must never promise 'no owner action needed'")


def test_readiness_still_promises_the_sweep_when_a_seat_is_eligible() -> None:
    """Inverse direction of the same likely_misfire: hard-coding 'owner
    action needed' everywhere lies for tasks a machine seat DOES take."""
    api = _read(_READINESS)
    assert "the machine seat will" in api and "no owner action needed" in api, (
        "the eligible-seat branch (api/conductor.py:158-163) must survive")


# ---------------------------------------------------------------------------
# 5 · The oracle + misfire are legible AT the gate, and render exactly ONCE
# ---------------------------------------------------------------------------

def test_likely_misfire_is_legible_where_the_gate_is_decided() -> None:
    """The gate panel already shows the oracle's lead clause; the risk that
    makes a pass wrong must sit with it, not only in the Tests tab."""
    src = _read(_TDP)
    i = src.find("what you're approving")
    j = src.find("the gate's evidence tooth")
    assert 0 <= i < j, "the gate panel's 'what you're approving' block moved"
    assert "likely_misfire" in src[i:j], (
        "task.likely_misfire must render inside the gate decision panel")


def test_oracle_does_not_render_twice_on_a_conductor_task() -> None:
    """stop_if #2: the gate panel renders the oracle (TaskDetailPage) AND
    PlanView renders it in the Tests tab — on a conductor task both are on
    screen. The PlanView copy must yield when the gate owns it."""
    src = _read(_TDP)
    assert "oracle={task.oracle}" not in src, (
        "PlanView is handed the oracle unconditionally — that is the second, "
        "competing oracle block")
    m = re.search(r"oracle=\{([^\n]*)\}", src)
    assert m and re.search(r"gate|Gate", m.group(1)), (
        "the oracle prop must be guarded by whether the gate panel owns it")
    assert "oracle — observable completion signal" in _read(_PLANVIEW), (
        "PlanView keeps its oracle block for the non-gate case — the fix is "
        "not deletion")


# ---------------------------------------------------------------------------
# 6 · The build the owner LOOKS at is identifiable
# ---------------------------------------------------------------------------

# The bump THIS slice shipped. A FLOOR, never an equality: the guard's intent
# is "a user-visible change ships a patch bump", which stays true at every
# later version. Pinning the literal made the test go red on the very next
# bump — exactly the behaviour the rule demands — so it punished the next
# person to obey it (task 90c32aa4; red on CI run 29893922722, PR #220).
_VERSION_FLOOR = (7, 1, 26)


def test_version_patch_bumped_for_the_reconciled_gate_panel() -> None:
    ver = _read(_VERSION)
    m = re.search(r'PRISM_VERSION = "(\d+)\.(\d+)\.(\d+)"', ver)
    assert m, "PRISM_VERSION must stay a readable semver literal"
    assert tuple(int(g) for g in m.groups()) >= _VERSION_FLOOR, (
        "user-visible change ⇒ patch-bump to at least "
        f"{'.'.join(str(n) for n in _VERSION_FLOOR)} in the same commit")
    assert "v7.1.26:" in ver, "add a PRISM_VERSION_NOTES line for 7.1.26"


# ---------------------------------------------------------------------------
# 7 · parseAcLines folds a nested oracle sub-bullet into its AC (task
#     3a3f90da, 2026-08-26) — the FRONTEND half of a fix whose backend half
#     (arc_governance.py's _ac_lines) landed the same day. A drive is
#     instructed (.claude/workflows/implement.js:540) to write each
#     criterion so it "ends with a line `- oracle: <observable check>`" — a
#     separate, more-indented CHILD bullet, not a same-line suffix. Before
#     this fix, parseAcLines only ever matched a raw line starting with
#     "AC-\d", so that canonical nested oracle line (which starts with
#     "oracle:") was silently dropped: a human on the Tests tab saw every
#     AC in a 9-AC story with NO oracle text at all, even though the
#     rubric-side bug (already fixed) meant the gate was about to pass
#     that very story. Fixing only the machine-facing parser and leaving
#     this one broken would have been a one-sided fix — the page a human
#     actually reads would still show a lie.
# ---------------------------------------------------------------------------

def test_parse_ac_lines_folds_a_nested_oracle_bullet_into_its_ac() -> None:
    src = _read(_PLANVIEW)
    fn_at = src.index("function parseAcLines(")
    next_fn_at = src.index("\nfunction ", fn_at + 1)
    body = src[fn_at:next_fn_at]

    # The fold must key off indentation, not merely "is this a bullet" —
    # that was the exact shape of the original bug: any bulleted line,
    # regardless of nesting, was eligible to become its own entry (and a
    # nested oracle sub-bullet doesn't even start with "AC-\d", so under
    # the OLD code it was not merely mis-grouped — it was dropped outright).
    assert re.search(r"const indent\s*=", body), (
        "parseAcLines must compute each line's indentation")
    assert re.search(r"acIndent\s*=\s*indent\b", body), (
        "an AC line must record its OWN indentation as the fold threshold "
        "for whatever follows it"
    )
    fold_m = re.search(
        r"if\s*\(\s*acIndent\s*!==\s*null[^)]*indent\s*>\s*acIndent\s*\)\s*\{"
        r"\s*out\[out\.length\s*-\s*1\]\s*\+=",
        body,
    )
    assert fold_m, (
        "a line MORE indented than its AC must be appended onto that AC's "
        "own entry (out[out.length - 1] +=), not pushed as a new, separate "
        "entry and not silently dropped — this is the exact fold the "
        "backend's _ac_lines fix (arc_governance.py, same day) also needed"
    )
    # A blank line resets the fold state — a paragraph break must not let
    # unrelated later content silently glue onto a stale AC.
    assert re.search(r"if\s*\(\s*!raw\.trim\(\)\s*\)\s*\{\s*acIndent\s*=\s*null",
                      body), (
        "a blank line must reset acIndent so unrelated content after a "
        "paragraph break never folds into a stale AC entry"
    )
