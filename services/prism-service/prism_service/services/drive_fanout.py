"""Deterministic safe epic fan-out (task f6328e9e, C7 of the
PI-orchestration build, parent 81b23574 FR-6 / AC-6).

Given an epic whose children declare ``allowed_files``, drive them
through the C1 drive engine under the safe-fan-out invariant
(prism_guide 'orchestration'):

  * children with pairwise-DISJOINT allowed_files share a WAVE and run
    CONCURRENTLY (threads, bounded by ``max_parallel``);
  * a child whose allowed_files OVERLAP an already-placed child's is
    deferred to a later wave — the collision boundary: overlapping
    children are never in flight together (empty allowed_files is
    conservative: it collides with everything and rides a wave alone);
  * each child is gated on its OWN proof_type by its own engine walk;
    this module exposes NO override surface and never passes override
    to any seam ([[feedback_gate_enforcement_doctrine]]);
  * FAILURE ISOLATION: one child's ok:false (or raise) lands in the
    result with its reason and never aborts siblings or later waves;
  * the epic ROLL-UP verdict reuses the pure
    ``conductor_service.epic_rollup_verdict`` over the children's fresh
    task rows once all waves complete.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from prism_service.services.conductor_service import epic_rollup_verdict

DEFAULT_MAX_PARALLEL = 3


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Duck-typed field read: Task rows and plain dicts both work."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _allowed(child: Any) -> frozenset[str]:
    files = _attr(child, "allowed_files", None) or []
    return frozenset(str(f) for f in files if str(f).strip())


def partition_waves(children: list) -> list[list]:
    """Greedy first-fit wave partition by allowed_files overlap.

    DETERMINISTIC: stable child order in, stable waves out. A child joins
    the FIRST wave whose accumulated file set is disjoint from its own;
    otherwise it opens a new wave. A child with EMPTY allowed_files is
    treated as touching EVERYTHING (conservative), so it can never share
    a wave: it rides alone and nothing joins its wave after it.
    """
    waves: list[dict] = []  # {"files": set, "universal": bool, "members": []}
    for child in children:
        files = _allowed(child)
        universal = not files
        placed = False
        for wave in waves:
            if universal or wave["universal"]:
                continue
            if files.isdisjoint(wave["files"]):
                wave["files"] |= files
                wave["members"].append(child)
                placed = True
                break
        if not placed:
            waves.append({"files": set(files), "universal": universal,
                          "members": [child]})
    return [w["members"] for w in waves]


class _Meter:
    """Lock-guarded running-drive counter: the auditability signal that
    concurrency genuinely happened (FR-6) — peak simultaneous drives."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running = 0
        self.peak = 0

    def __enter__(self) -> "_Meter":
        with self._lock:
            self._running += 1
            self.peak = max(self.peak, self._running)
        return self

    def __exit__(self, *exc: object) -> None:
        with self._lock:
            self._running -= 1


def _drive_one(engine: Any, child_id: str, session_id: str,
               meter: _Meter) -> dict:
    """One child drive, exception-hardened (FR-4): a raise becomes an
    ok:false result carrying the reason — never a sibling abort. No
    override kwarg exists on this path (FR-3)."""
    with meter:
        try:
            res = engine.plan(child_id, session_id=session_id)
        except Exception as exc:  # noqa: BLE001 — isolation boundary
            return {"ok": False, "task_id": child_id,
                    "reason": f"engine raised: {exc}"}
    if not isinstance(res, dict):
        return {"ok": False, "task_id": child_id,
                "reason": f"engine returned non-dict {type(res).__name__}"}
    return res


def fan_out(epic_id: str, *, engine: Any, task_svc: Any,
            session_id: str = "",
            max_parallel: int = DEFAULT_MAX_PARALLEL) -> dict:
    """Drive an epic's children through the engine, safely in parallel.

    Returns ``{ok, epic_id, waves, children, rollup, stats}`` where
    ``waves`` is the id layout, ``children`` maps child id -> its engine
    result (or isolated failure), ``rollup`` is the
    ``epic_rollup_verdict`` over the FRESH child rows, and ``stats``
    carries ``max_concurrent`` (observed peak), wave/child counts, and
    the override tally summed from engine stats (this module adds none).
    ``ok`` is true only when every child drive succeeded AND the roll-up
    passed. No children -> not an epic: ok:false with the verdict's
    reason.
    """
    children = task_svc.list(parent_id=epic_id)
    waves = partition_waves(children)
    meter = _Meter()
    results: dict[str, dict] = {}

    for wave in waves:  # strictly sequential across waves (FR-2)
        workers = max(1, min(int(max_parallel or 1), len(wave)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                str(_attr(c, "id", "")): pool.submit(
                    _drive_one, engine, str(_attr(c, "id", "")),
                    session_id, meter)
                for c in wave
            }
            for cid, fut in futures.items():
                results[cid] = fut.result()  # _drive_one never raises

    # Epic roll-up over FRESH rows — the drives just mutated them (FR-5).
    fresh = task_svc.list(parent_id=epic_id)
    rollup_ok, rollup_reason = epic_rollup_verdict(fresh)

    overrides = sum(
        int((r.get("stats") or {}).get("overrides", 0) or 0)
        for r in results.values() if isinstance(r, dict))
    all_children_ok = bool(results) and all(
        bool(r.get("ok")) for r in results.values())

    return {
        "ok": bool(all_children_ok and rollup_ok),
        "epic_id": epic_id,
        "waves": [[str(_attr(c, "id", "")) for c in wave]
                  for wave in waves],
        "children": results,
        "rollup": {"ok": bool(rollup_ok), "reason": rollup_reason},
        "stats": {
            "max_concurrent": meter.peak,
            "waves": len(waves),
            "children": len(results),
            "overrides": overrides,
        },
    }
