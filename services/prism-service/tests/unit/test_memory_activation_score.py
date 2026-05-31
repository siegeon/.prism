"""RED scaffold — Memory activation score + cheap selection prior (Tier 1).

Task 0bda3046. Pins the SERVICE-layer contract for the activation score:

  * compute_activation(entry, now=None) is a pure deterministic function
        activation = importance + effectiveness*W1 - decay(elapsed_days)*W2
    where decay() is a monotonically increasing (Ebbinghaus-style) function
    of (now - last_recalled) in days. W1 / W2 are module-level constants.
  * recall() ranks by activation (a freshly-recalled but low-importance
    entry outranks an old high-importance one once decay bites).
  * fading_entries(limit, threshold) returns entries ascending by
    activation, filtered below `threshold` (the prune/forget selection
    prior).

These FAIL today: compute_activation / fading_entries / W1 / W2 do not
exist on memory_service yet, and recall() still sorts on the old
(importance + effectiveness*2, recall_count) blend.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.models.memory import ExpertiseEntry
from prism_service.services import memory_service as ms
from prism_service.services.memory_service import MemoryService


_NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _entry(**kw) -> ExpertiseEntry:
    base = dict(
        id=kw.pop("id", "mx-aaaaaa"),
        domain=kw.pop("domain", "feedback"),
        name=kw.pop("name", "an-entry"),
        description=kw.pop("description", "desc"),
        type="convention",
        classification="tactical",
        status="active",
    )
    base.update(kw)
    return ExpertiseEntry(**base)


# ----------------------------------------------------------------------
# compute_activation — pure, deterministic, module-level weights
# ----------------------------------------------------------------------


def test_module_level_weight_constants_exist():
    # W1 (effectiveness weight) and W2 (decay weight) are module-level
    # constants — the formula's tunables must not be buried as magic
    # numbers inline.
    assert isinstance(ms.W1, (int, float)), "W1 constant missing on memory_service"
    assert isinstance(ms.W2, (int, float)), "W2 constant missing on memory_service"
    assert ms.W2 > 0, "decay weight W2 must be positive (decay subtracts)"


def test_compute_activation_is_pure_and_deterministic():
    e = _entry(importance=6, effectiveness=0.5,
               last_recalled=_iso(_NOW - timedelta(days=2)))
    a1 = MemoryService.compute_activation(e, now=_NOW)
    a2 = MemoryService.compute_activation(e, now=_NOW)
    assert a1 == a2, "compute_activation must be deterministic"
    assert isinstance(a1, float)


def test_compute_activation_formula_shape():
    # With elapsed=0 (recalled exactly now), decay term contributes its
    # minimum, so activation ≈ importance + effectiveness*W1.
    e = _entry(importance=8, effectiveness=1.0, last_recalled=_iso(_NOW))
    act = MemoryService.compute_activation(e, now=_NOW)
    floor = 8 + 1.0 * ms.W1
    # Decay at elapsed≈0 must be non-negative and small — activation must
    # not exceed the importance+effectiveness ceiling.
    assert act <= floor + 1e-9, "fresh entry must not exceed imp+eff*W1"
    assert act >= floor - ms.W2 - 1e-9


def test_decay_is_monotonic_increasing_in_elapsed_time():
    # Same entry, older last_recalled → strictly lower activation
    # (decay grows with elapsed days). This is the Ebbinghaus property.
    fresh = _entry(importance=7, effectiveness=0.2,
                   last_recalled=_iso(_NOW - timedelta(days=1)))
    stale = _entry(importance=7, effectiveness=0.2,
                   last_recalled=_iso(_NOW - timedelta(days=30)))
    older = _entry(importance=7, effectiveness=0.2,
                   last_recalled=_iso(_NOW - timedelta(days=120)))
    a_fresh = MemoryService.compute_activation(fresh, now=_NOW)
    a_stale = MemoryService.compute_activation(stale, now=_NOW)
    a_older = MemoryService.compute_activation(older, now=_NOW)
    assert a_fresh > a_stale > a_older, (
        f"decay must be monotonically increasing in elapsed days; "
        f"got {a_fresh} !> {a_stale} !> {a_older}"
    )


def test_recall_bump_resets_decay_clock():
    # Two entries with identical importance/effectiveness; the one with a
    # more recent last_recalled (staircase reset) must out-activate the
    # one whose clock has run long.
    just_recalled = _entry(id="mx-fresh", importance=5, effectiveness=0.0,
                           last_recalled=_iso(_NOW - timedelta(hours=1)))
    long_ago = _entry(id="mx-old", importance=5, effectiveness=0.0,
                      last_recalled=_iso(_NOW - timedelta(days=90)))
    assert (MemoryService.compute_activation(just_recalled, now=_NOW)
            > MemoryService.compute_activation(long_ago, now=_NOW))


# ----------------------------------------------------------------------
# recall() ranks by activation
# ----------------------------------------------------------------------


def _svc(tmp_path) -> MemoryService:
    return MemoryService(mulch_dir=str(tmp_path / "mulch"))


def test_recall_ranks_by_activation_not_old_blend(tmp_path):
    """A low-importance entry recalled moments ago must outrank a
    high-importance entry whose decay clock has run for months — the OLD
    (importance + effectiveness*2, recall_count) sort would put the
    high-importance one first; activation flips it."""
    svc = _svc(tmp_path)
    old_high = _entry(id="mx-high", name="old-high-importance",
                      description="ranking activation widget alpha",
                      importance=10, effectiveness=0.0,
                      last_recalled=_iso(_NOW - timedelta(days=200)))
    fresh_low = _entry(id="mx-low", name="fresh-low-importance",
                       description="ranking activation widget beta",
                       importance=3, effectiveness=0.0,
                       last_recalled=_iso(_NOW - timedelta(minutes=5)))
    svc._write_entries("feedback", [old_high, fresh_low])

    results = svc.recall("ranking activation widget", domain="feedback", limit=5)
    ids = [e.id for e in results]
    assert "mx-low" in ids and "mx-high" in ids, ids
    assert ids.index("mx-low") < ids.index("mx-high"), (
        "fresh low-importance entry must outrank stale high-importance one "
        f"once recall() ranks by activation; got order {ids}"
    )


# ----------------------------------------------------------------------
# fading_entries — the prune/forget selection prior
# ----------------------------------------------------------------------


def test_fading_entries_orders_ascending_below_threshold(tmp_path):
    svc = _svc(tmp_path)
    # Three entries spanning the activation range.
    hot = _entry(id="mx-hot", name="hot", importance=9, effectiveness=0.8,
                 last_recalled=_iso(_NOW - timedelta(hours=1)))
    warm = _entry(id="mx-warm", name="warm", importance=5, effectiveness=0.0,
                  last_recalled=_iso(_NOW - timedelta(days=40)))
    cold = _entry(id="mx-cold", name="cold", importance=2, effectiveness=-0.5,
                  last_recalled=_iso(_NOW - timedelta(days=200)))
    svc._write_entries("feedback", [hot, warm, cold])

    faders = svc.fading_entries(limit=10, threshold=ms.W1 * 10, now=_NOW)
    ids = [e.id for e in faders]
    # Coldest first (ascending by activation).
    assert ids and ids[0] == "mx-cold", f"coldest must lead, got {ids}"
    # Strictly ascending by activation.
    acts = [MemoryService.compute_activation(e, now=_NOW) for e in faders]
    assert acts == sorted(acts), f"fading must be ascending by activation: {acts}"
    # The hot entry is above any reasonable low threshold and must be
    # filtered out.
    assert "mx-hot" not in ids, "hot entry must not appear below the fade threshold"


def test_fading_entries_respects_limit(tmp_path):
    svc = _svc(tmp_path)
    entries = [
        _entry(id=f"mx-{i}", name=f"e{i}", importance=1, effectiveness=-1.0,
               last_recalled=_iso(_NOW - timedelta(days=300 + i)))
        for i in range(6)
    ]
    svc._write_entries("feedback", entries)
    faders = svc.fading_entries(limit=2, threshold=1000.0, now=_NOW)
    assert len(faders) == 2, f"limit must cap results; got {len(faders)}"
