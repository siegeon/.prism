"""RED tests for the DISPOSABLE fixture task 31106380 — "FIXTURE gate panel
receipt rows".

The fixture produces no source change. Its outcome is an ARTEFACT: a
screenshot of the rendered gate panel, with BOTH reconciled receipt rows in
one frame, written into the PRISM evidence store of task 5a6837a0. So the
observation that is red here is the artefact, not a function.

WHY THE CAPTIONS THEMSELVES ARE NOT THE RED HALF: task 5a6837a0 already
shipped them. `DecisionPacket.tsx:147` renders "latest on this task · any
gate · <sha7>" and `TaskDetailPage.tsx:2294,2300` render "oracle receipt ·
<adapter>" / "serves … · anchor …". Asserting those strings is GREEN at this
tree, so they are pinned below as GUARDS (they say the frame has something
to show), never as the fix.

THIS FILE IS THROWAWAY. It rides the fixture's own branch and must never
merge to main: the evidence store lives under the machine's PRISM data dir,
not in the repo, so on any host that never took the screenshot these
assertions are correctly red. The fixture is cancelled and this file dies
with it (AC-5).
"""

from __future__ import annotations

import os
import struct
from pathlib import Path

# The task whose evidence store receives the capture (AC-4).
SUBJECT_TASK = "5a6837a0-a6cf-4bc3-9eed-0b2a43217d98"
CAPTURE_NAME = "gate-panel-receipt-rows-named.png"

# NOT `prism_service.data_dir.evidence_dir()`: tests/conftest.py:41-51 pins
# PRISM_DATA_DIR at a fresh `/tmp/prism-test-data-*` for the whole session, so
# the production helper resolves to a throwaway directory that the real
# capture can never land in — a red that could never turn green. The artefact
# is written into the LIVE store, so the assertion reads the live store.
_STORE = Path(os.environ.get("PRISM_REAL_DATA_DIR") or (Path.home() / ".prism"))

_HERE = Path(__file__).resolve()
_ROOT = _HERE.parent.parent.parent  # services/prism-service
_SRC = _ROOT / "prism_service" / "web" / "src"
_TDP = _SRC / "pages" / "TaskDetailPage.tsx"
_PACKET = _SRC / "components" / "plan" / "DecisionPacket.tsx"


def _capture() -> Path:
    return _STORE / "evidence" / SUBJECT_TASK / CAPTURE_NAME


def _png_size(raw: bytes) -> tuple[int, int]:
    """Width/height out of the IHDR chunk of a PNG."""
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG file"
    width, height = struct.unpack(">II", raw[16:24])
    return width, height


# ---------------------------------------------------------------------------
# RED · the fixture's own outcome — the capture exists in the evidence store
# ---------------------------------------------------------------------------


def test_the_gate_panel_capture_landed_in_the_evidence_store_of_5a6837a0():
    """AC-4. The one accepted destination is the PRISM evidence store of the
    subject task, served back through /api/tasks/<id>/evidence/<file>."""
    path = _capture()
    assert path.is_file(), (
        f"no gate-panel capture at {path} — the fixture has not been "
        "photographed yet, so task 5a6837a0's UI claim has no proof on file"
    )


def test_the_capture_is_a_real_png_not_an_empty_placeholder():
    """A zero-byte or non-PNG file would satisfy `is_file()` and prove
    nothing, so the magic bytes and a floor on size are pinned too."""
    raw = _capture().read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "capture is not a PNG"
    assert len(raw) > 10_000, f"capture is only {len(raw)} bytes — too small to be a screen"


def test_the_capture_is_wide_enough_to_hold_both_receipt_rows_in_one_frame():
    """AC-1 wants BOTH rows in ONE frame. A crop of a single row would still
    be a valid PNG, so the frame's own geometry is pinned: the gate panel
    column plus its two captions does not fit in a narrow strip."""
    width, height = _png_size(_capture().read_bytes())
    assert width >= 600, f"capture is {width}px wide — too narrow for the gate panel column"
    assert height >= 200, f"capture is {height}px tall — one row cannot sit above the other"


# ---------------------------------------------------------------------------
# GREEN GUARDS · the captions the frame must show still exist, and still sit
# inside the gate panel. These observe no fix; they stop a regression that
# would make the capture meaningless.
# ---------------------------------------------------------------------------


def test_the_decision_packet_row_still_carries_its_own_scope_caption():
    """AC-2. `DecisionPacket.tsx:147`."""
    src = _PACKET.read_text(encoding="utf-8")
    assert "latest on this task · any gate" in src
    i = src.find("latest on this task · any gate")
    assert "Oracle receipt" in src[max(0, i - 1200): i], (
        "the caption drifted away from the 'Oracle receipt' row it names"
    )


def test_the_check_row_still_names_the_gate_it_serves_and_its_anchor():
    """AC-3. `TaskDetailPage.tsx:2294` and `:2300`, both anchor branches."""
    src = _TDP.read_text(encoding="utf-8")
    i = src.find("oracle receipt · {gateReadiness?.receipt?.adapter")
    assert i >= 0, "the CHECK row's adapter caption is gone"
    chunk = src[i: i + 1200]
    assert "serves {stepLabel(" in chunk, "the CHECK row no longer names the gate it serves"
    assert "anchor ${gateAnchorSha.slice(0, 7)}" in chunk or "anchor not reported" in chunk, (
        "the CHECK row no longer names the tree it was measured at"
    )
