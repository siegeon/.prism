"""UI contract tests for "Machine gate decisions are visibly machine-made"
(task 934af569-bfc5-4184-a951-1cc267688436, child of epic fcf6b70b).

The server already resolves and attaches `actor_identity` to every history
row (services/prism-service/prism_service/api/tasks.py:712-724, shape
{id, kind, display_name, user_id, external_ref} with kind in
machine|human|agent|unknown - actor_service.py:37-50). Only the RENDER is
missing: TaskDetailPage.tsx's `HistoryRow` type does not declare the field,
and `TimelineRow`'s always-rendered header (~412-435) prints the raw
`row.actor` string as plain opaque monospace text with no visual
distinction between a machine seat and a human. The rubric/receipt gist for
a gate_decide row (`turnSummary()`) is computed but only ever surfaced
inside the `open`/expand branch (~446-456), which is NOT glance level.

The PRISM SPA has NO JS test runner, so UI acceptance criteria are pinned by
asserting the ACTUAL TSX source (tests/unit/test_conductor_page_animated_
cleanup_ui.py:4-6). Per repo convention (CLAUDE.md Lessons, e139295d /
mx many): comments are stripped before every assertion, and every branch is
located by brace-balancing from an unambiguous marker, never a fixed
character window, so a comment or a stray literal cannot satisfy an
assertion the real guard is supposed to own. Patterns reused from
tests/unit/test_cancelled_task_gate_card_inert.py:52-77 (_strip_comments)
and tests/unit/test_gate_banner_honest_state_ui.py:51-67 (brace-balanced
extraction from a marker).

ALL of these FAIL against the current source (baseline
0b2e36e153dfc3f64994ab35075be26de3f64440): HistoryRow has no
`actor_identity` field, TimelineRow's header has no kind-branching chip,
and the gate_decide gist is not shown at header level.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_TSX = _SERVICE_ROOT / "prism_service" / "web" / "src" / "pages" / "TaskDetailPage.tsx"

_HEADER_MARKER = '<div className="flex items-baseline justify-between gap-3 flex-wrap">'
_EXPAND_MARKER = "{summary && ("
_TIMELINE_ROW_MARKER = "isFirst: boolean }) {"
_HISTORY_ROW_MARKER = "type HistoryRow = {"


def _read() -> str:
    assert _TSX.exists(), f"{_TSX} missing"
    return _TSX.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Drop /* */, {/* */} and // comments so a comment can never satisfy
    a source assertion (repeated failure mode, see CLAUDE.md Lessons). The
    trailing negative lookbehind guards escaped '//' inside regex literals
    (e.g. `/^https?:\\/\\//`) from being swallowed as a comment start."""
    src = re.sub(r"\{\s*/\*.*?\*/\s*\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)(?<!:)(?<!\\)//.*$", "", src)
    return src


def _extract_braced(src: str, marker: str) -> str:
    """Return the `{...}` block whose opening brace is the first `{` found
    AT OR AFTER `marker`'s end, matched by brace depth (not a fixed slice)."""
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


def _timeline_row_body(src: str) -> str:
    return _extract_braced(src, _TIMELINE_ROW_MARKER)


def _header_slice(body: str) -> str:
    """The always-rendered header portion of TimelineRow's JSX — strictly
    BEFORE the `{summary && (` expand-button marker. Glance level lives
    here; the expand branch (446-456 today) does not count."""
    start = body.index(_HEADER_MARKER)
    end = body.index(_EXPAND_MARKER, start)
    return body[start:end]


def _extract_function_body(src: str, start_idx: int) -> str:
    """Brace-balance a function BODY starting at a `function Name(...) {`
    declaration whose `idx` points at the `function` keyword. Skips past
    the parameter list (which may itself contain `{...}` destructuring /
    type-annotation braces) by locating the FIRST `") {"` after `idx` —
    the point where the params close and the body opens — rather than
    naively taking the first `{`, which would wrongly match a destructured
    parameter's brace instead of the function body."""
    body_open = src.index(") {", start_idx)
    start = body_open + 2  # index of the body's opening '{'
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    raise AssertionError(f"unbalanced braces scanning function body from {start_idx}")


def _actor_chip_body(src: str) -> str:
    """Locate whatever function renders the actor identity chip by finding
    the first `function <Name>(...)` whose body branches on `.kind`, so
    this test does not hardcode a component name. Falls back to the
    TimelineRow body itself if the branch logic is written inline there."""
    for m in re.finditer(r"function\s+\w+\s*\(", src):
        try:
            body = _extract_function_body(src, m.start())
        except (ValueError, AssertionError):
            continue
        if re.search(r'kind\s*===\s*["\']machine["\']', body):
            return body
    # Inline in TimelineRow itself.
    return _timeline_row_body(src)


# ---------------------------------------------------------------------------
# Self-check: the comment-stripper actually strips a planted trap comment
# that names every string this suite otherwise searches for (AC-6 guard —
# a comment-only edit must never satisfy these assertions).
# ---------------------------------------------------------------------------

def test_strip_comments_removes_a_trap_comment():
    trap = (
        "const x = 1; // actor_identity.kind === \"machine\" conductor-adjudicator\n"
        "/* actor_identity kind machine human */\n"
        "{/* actor_identity kind machine */}\n"
        "const y = 2;"
    )
    stripped = _strip_comments(trap)
    assert "actor_identity" not in stripped
    assert "conductor-adjudicator" not in stripped
    assert "const x = 1" in stripped and "const y = 2" in stripped


# ---------------------------------------------------------------------------
# AC-1 - HistoryRow declares actor_identity
# ---------------------------------------------------------------------------

def test_history_row_declares_actor_identity():
    src = _strip_comments(_read())
    block = _extract_braced(src, _HISTORY_ROW_MARKER)
    assert "actor_identity" in block, (
        "HistoryRow must declare actor_identity (server already sends it, "
        "api/tasks.py:712-724) so the client type matches the wire shape"
    )


# ---------------------------------------------------------------------------
# AC-2 - the chip lives in TimelineRow's ALWAYS-RENDERED header, not the
# expand branch.
# ---------------------------------------------------------------------------

def test_actor_identity_is_consumed_at_header_glance_level():
    src = _strip_comments(_read())
    body = _timeline_row_body(src)
    header = _header_slice(body)
    assert "actor_identity" in header, (
        "the header row (before the {summary && ( expand marker) must "
        "reference row.actor_identity - a chip added only to the expand "
        "branch is not glance level"
    )


def test_actor_identity_reference_is_not_confined_to_the_expand_branch():
    src = _strip_comments(_read())
    body = _timeline_row_body(src)
    header_start = body.index(_HEADER_MARKER)
    expand_start = body.index(_EXPAND_MARKER, header_start)
    first_ref = body.index("actor_identity")
    assert first_ref < expand_start, (
        "the FIRST reference to actor_identity in TimelineRow must occur "
        "before the expand marker - proves it is not expand-only"
    )


# ---------------------------------------------------------------------------
# AC-3 - machine vs. human branches on actor_identity.kind, never on a
# literal actor string (the FOURTH likely_misfire: re-deriving machine-ness
# by string-matching "conductor-" desyncs from MACHINE_SEATS the moment a
# new seat is added).
# ---------------------------------------------------------------------------

def test_machine_branch_keys_on_kind_not_on_a_literal_actor_string():
    src = _strip_comments(_read())
    chip_body = _actor_chip_body(src)
    assert re.search(r'kind\s*===\s*["\']machine["\']', chip_body), (
        "expected a branch comparing actor_identity.kind to the literal "
        "\"machine\" (ActorKind.MACHINE value, models/actor.py:21)"
    )


def test_no_literal_actor_string_match_stands_in_for_kind():
    src = _strip_comments(_read())
    # Forbidden: re-deriving "is this a machine seat" from the raw actor
    # string instead of the server-resolved kind.
    assert not re.search(r'actor\s*===\s*["\']conductor-adjudicator["\']', src)
    assert not re.search(r'actor\??\.includes\(\s*["\']conductor-', src)
    assert not re.search(r'\.actor\s*===\s*["\']conductor-autoclear["\']', src)


# ---------------------------------------------------------------------------
# AC-4 - the visual treatment actually DIFFERS between kinds (a chip that
# renders identically for every kind distinguishes nothing).
# ---------------------------------------------------------------------------

def test_machine_and_human_and_fallback_render_distinct_tones():
    src = _strip_comments(_read())
    chip_body = _actor_chip_body(src)

    machine_idx = chip_body.index('kind === "machine"')
    human_idx = chip_body.index('kind === "human"')
    assert human_idx > machine_idx, (
        "expected the machine branch to be checked before the human/"
        "fallback branches"
    )

    machine_region = chip_body[machine_idx:human_idx]
    # Whatever token names the machine tone, capture it so the human
    # region can be checked for NOT reusing it.
    tone_match = re.search(r"accent-(\w+)", machine_region)
    assert tone_match, (
        "the machine branch must use one of the existing --accent-* tone "
        "tokens (matching TonePill/StateChip's existing palette) so it "
        "reads as a distinct, on-brand tone, not an ad-hoc color"
    )
    machine_tone = tone_match.group(1)

    fallback_start = human_idx
    fallback_region = chip_body[fallback_start:]
    human_region = fallback_region[:fallback_region.index("return", 40)] if "return" in fallback_region[40:] else fallback_region

    assert f"accent-{machine_tone}" not in human_region, (
        "the human branch must not reuse the machine branch's accent tone "
        "- otherwise a human-approved row can be mistaken for machine"
    )

    # A third, distinct kind (agent/unknown) must not silently collapse
    # into the machine tone either.
    assert chip_body.count('kind === "machine"') == 1, (
        "expected exactly one machine-kind check driving the distinct tone"
    )


# ---------------------------------------------------------------------------
# AC-5 - the rubric/receipt gist for a gate_decide row is visible at header
# (glance) level, reusing the existing turnSummary()/grabKV extraction.
# ---------------------------------------------------------------------------

def test_gate_decide_gist_is_visible_at_header_level():
    src = _strip_comments(_read())
    body = _timeline_row_body(src)
    header = _header_slice(body)
    assert "gate_decide" in header, (
        "the header must special-case gate_decide rows to show the "
        "rubric/receipt gist, not just the actor"
    )
    assert "summary" in header, (
        "the header's gate_decide gist must reuse the existing `summary` "
        "(turnSummary()/grabKV) computation already used by the expand "
        "branch, not a second parser"
    )
