"""RED scaffold — schema-validated slot-fill with bounded retry
(task 6d36ed99, C2 of the PI-orchestration build, parent 81b23574 FR-2).

Pins the slot-fill contract: every model inference on the drive path is
a SMALL slot (title / oracle / summary / fr_line / ac_line / verdict)
validated against a shape schema with BOUNDED retry; on exhaustion a
DETERMINISTIC fallback (or a typed failure when no fallback exists) —
never a bad value into a plan, never a hard stall (risk R1, mx-239c13).

Unit-testable with a stub model: no real inference anywhere below.

FAILS today: prism_service.inference.pi_slots does not exist.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _slots():
    from prism_service.inference import pi_slots
    return pi_slots


class StubModel:
    """Scripted model seam: returns the next canned output per call and
    counts invocations — the test's proof of the retry budget."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def __call__(self, prompt: str, system: str = "") -> str:
        self.calls += 1
        if not self.outputs:
            return ""
        return self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]


CONTEXT = {"feature_ask": "schema validated slot fill with bounded retry"}


# ── AC-1: garbage then valid → retried, valid slot returned ────────────

def test_garbage_then_valid_retries_then_succeeds():
    s = _slots()
    stub = StubModel(["%%% not a title %%%\n\nsecond line garbage",
                      '{"value": "Slot fill validated with bounded retry"}'])
    res = s.fill_slot("title", CONTEXT, model=stub, retries=2)
    assert res.ok is True, res
    assert res.value == "Slot fill validated with bounded retry"
    assert res.attempts == 2, res
    assert res.fallback_used is False
    assert stub.calls == 2


# ── AC-2: all-garbage → bounded attempts then deterministic fallback ───

def test_all_garbage_falls_back_deterministically():
    s = _slots()
    stub = StubModel([""])  # empty forever
    res = s.fill_slot("title", CONTEXT, model=stub, retries=2)
    assert stub.calls == 3, "retries=2 means at most 1+2 model calls"
    assert res.fallback_used is True, res
    assert res.ok is True, "fallback keeps the drive alive"
    assert res.error, "the exhaustion error must be preserved"
    # deterministic: derived from the feature ask, and schema-valid itself
    words = res.value.split()
    assert 4 <= len(words) <= 9, res.value
    again = s.fill_slot("title", CONTEXT, model=StubModel([""]), retries=2)
    assert again.value == res.value, "fallback must be deterministic"


# ── AC-3: per-slot schema shape is enforced ────────────────────────────

def test_schema_validation_per_slot():
    s = _slots()
    eleven = " ".join(["word"] * 11)
    res = s.fill_slot("title", CONTEXT,
                      model=StubModel([f'{{"value": "{eleven}"}}']),
                      retries=0)
    assert res.fallback_used or res.ok is False, (
        "an 11-word title must not validate")
    ok = s.fill_slot("title", CONTEXT,
                     model=StubModel(['{"value": "Six words make a valid title"}']),
                     retries=0)
    assert ok.ok is True and ok.fallback_used is False, ok
    multi = s.fill_slot(
        "oracle", CONTEXT,
        model=StubModel(['{"value": "line one\\nline two"}']), retries=0)
    assert multi.fallback_used or multi.ok is False, (
        "a multi-line oracle must not validate")


# ── AC-4: lenient extraction (fences / think-tags / prose-before-JSON) ─

def test_lenient_extraction():
    s = _slots()
    wrapped = ('<think>hmm let me think</think>Sure! Here is the JSON:\n'
               '```json\n{"value": "Bounded retry keeps drives alive"}\n```')
    res = s.fill_slot("title", CONTEXT, model=StubModel([wrapped]), retries=0)
    assert res.ok is True and res.fallback_used is False, res
    assert res.value == "Bounded retry keeps drives alive"
    plain = s.fill_slot("oracle", CONTEXT,
                        model=StubModel(["pytest -q passes on the new module"]),
                        retries=0)
    assert plain.ok is True and plain.value.startswith("pytest -q"), plain


# ── AC-5: retry budget configurable and honored exactly ────────────────

def test_retry_budget_configurable():
    s = _slots()
    stub = StubModel([""])
    s.fill_slot("title", CONTEXT, model=stub, retries=0)
    assert stub.calls == 1, "retries=0 means exactly one attempt"
    stub5 = StubModel([""])
    s.fill_slot("title", CONTEXT, model=stub5, retries=5)
    assert stub5.calls == 6, "retries=5 means exactly six attempts"


# ── AC-6: no fallback → typed failure, never a raise / bad value ───────

def test_no_fallback_yields_typed_failure():
    s = _slots()
    spec = s.SlotSpec(
        name="strict_custom",
        prompt="Return one word.",
        schema={"single_line": True, "max_chars": 20},
        fallback=None,
    )
    res = s.fill_slot(spec, {}, model=StubModel([""]), retries=1)
    assert res.ok is False, res
    assert res.error, "typed failure must carry the reason"
    assert res.value == "", "no bad value may be emitted"


# ── FR-1: the registry carries the drive-path slots ────────────────────

def test_registry_covers_drive_slots():
    s = _slots()
    for name in ("title", "oracle", "summary", "fr_line", "ac_line", "verdict"):
        assert name in s.SLOTS, f"missing slot spec {name!r}"
