"""The retrieval savings estimate must never flatter PRISM.

Every test here pins a case where a number WOULD be dishonest, and
asserts we stay silent instead. The arithmetic is the easy part; the
refusals are the point.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services.retrieval_savings import (  # noqa: E402
    baseline_for,
    savings_for,
    to_tokens,
)


def _write(root: Path, rel: str, size: int) -> str:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x" * size, encoding="utf-8")
    return rel


def test_baseline_sums_distinct_files_only(tmp_path):
    a = _write(tmp_path, "a.py", 400)
    b = _write(tmp_path, "b.py", 600)
    # a.py listed twice must be counted once.
    out = baseline_for([a, b, a], root=str(tmp_path))
    assert out == {"files": 2, "baseline_chars": 1000}


def test_unsizable_files_shrink_the_claim_never_break_it(tmp_path):
    b = _write(tmp_path, "b.py", 800)
    out = baseline_for(["missing.py", b], root=str(tmp_path))
    # The absent file contributes nothing rather than poisoning the sum.
    assert out == {"files": 1, "baseline_chars": 800}


def test_no_sizable_file_means_no_estimate_at_all(tmp_path):
    assert baseline_for(["nope.py", "gone.py"], root=str(tmp_path)) is None
    assert baseline_for([], root=str(tmp_path)) is None
    # And a missing baseline must silence the claim, not zero it.
    assert savings_for(100, None) is None


def test_reports_savings_when_the_payload_is_genuinely_smaller(tmp_path):
    a = _write(tmp_path, "a.py", 8000)
    base = baseline_for([a], root=str(tmp_path))
    out = savings_for(payload_chars=400, baseline=base)
    assert out is not None
    assert out["baseline_tokens"] == to_tokens(8000)
    assert out["payload_tokens"] == to_tokens(400)
    assert out["tokens_saved"] == to_tokens(8000) - to_tokens(400)
    assert out["percent"] == 95
    assert out["files_covered"] == 1
    assert "estimate" in out["basis"]


def test_silent_when_the_payload_is_not_actually_smaller(tmp_path):
    """A tiny file the pointers cost more than: saving nothing is not a
    small win to round up, it is no win, and we say nothing."""
    tiny = _write(tmp_path, "tiny.py", 40)
    base = baseline_for([tiny], root=str(tmp_path))
    assert savings_for(payload_chars=1000, baseline=base) is None
    # Exactly break-even is still not a saving.
    assert savings_for(payload_chars=40, baseline=base) is None


def test_silent_on_an_empty_baseline(tmp_path):
    empty = _write(tmp_path, "empty.py", 0)
    base = baseline_for([empty], root=str(tmp_path))
    # The file exists, so it is counted, but a 0-char baseline can never
    # support a savings claim.
    assert base == {"files": 1, "baseline_chars": 0}
    assert savings_for(payload_chars=10, baseline=base) is None


def test_no_instruction_to_advertise_leaks_into_the_payload(tmp_path):
    """Graft's footer ships a nudge telling the agent to report savings
    to the user every turn. PRISM must not: tool output is not an ad
    slot. Pin the absence so nobody reintroduces it."""
    a = _write(tmp_path, "a.py", 8000)
    out = savings_for(400, baseline_for([a], root=str(tmp_path)))
    blob = " ".join(str(v) for v in out.values()).lower()
    for banned in ("tell the user", "end of your reply", "report the",
                   "saved this turn"):
        assert banned not in blob
