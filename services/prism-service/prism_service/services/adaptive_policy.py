"""Tier-3 adaptive policy loop — self-tune memory knobs from recall→outcome.

PRISM's moat: the memory subsystem's knobs are NOT frozen constants. This
service reads the REAL task-outcome effectiveness signal
(``MemoryService.get_effectiveness_scores`` over recall_log) plus the
op-verdict audit (``consolidation_runs``) and nudges three knobs:

  * ``forget_cutoff``               — the fading-activation threshold the
    forget runner selects under. RAISE it (forget less) when the forget op
    has been archiving memories that turned out to HELP outcomes.
  * ``decay_weight``                — global decay aggressiveness. LOWER it
    (decay gentler) when the effective-memory population is large, i.e. we
    are at risk of decaying things that help.
  * ``merge_similarity_threshold``  — how similar two memories must be to
    merge; nudged off merge-op confidence.

Each tuning is appended to ``scores.db`` table ``policy_knobs`` with a
timestamp + rationale, so the newest row is the active policy and the older
rows are the history the /learning panel charts (and a future bandit replays).

DEFERRED (designed-toward, not built here): rewriting Tier-2 op PROMPTS from
the bad-outcome training signal, and a learned/bandit policy over the
persisted history. This slice is deterministic knob nudges + accuracy
surfacing — a clean, testable first version.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

# Frozen-constant baselines the loop nudges away from. forget_cutoff mirrors
# memory_ops.forget.FADING_THRESHOLD (the value it replaces as a consumer).
DEFAULT_KNOBS: dict[str, float] = {
    "forget_cutoff": 2.0,
    "decay_weight": 1.0,
    "merge_similarity_threshold": 0.85,
}

# Hard clamps — a runaway nudge must never push a knob out of a sane range.
KNOB_BOUNDS: dict[str, tuple[float, float]] = {
    "forget_cutoff": (1.0, 6.0),
    "decay_weight": (0.5, 1.5),
    "merge_similarity_threshold": (0.70, 0.95),
}

# Per-tick nudge step (small — the loop is meant to drift, not jump).
_STEP = 0.25
_DECAY_STEP = 0.05
_MERGE_STEP = 0.02


def _clamp(knob: str, value: float) -> float:
    lo, hi = KNOB_BOUNDS[knob]
    return max(lo, min(hi, float(value)))


def get_active_knobs(scores_db: str) -> dict[str, float]:
    """Return the currently-active knobs: the newest ``policy_knobs`` row,
    falling back to ``DEFAULT_KNOBS`` when the loop has never run.

    Reads a fresh connection every call (durability, not a cached echo) so a
    consumer always sees the latest persisted tuning.
    """
    knobs = dict(DEFAULT_KNOBS)
    if not Path(scores_db).exists():
        return knobs
    conn = sqlite3.connect(scores_db)
    try:
        row = conn.execute(
            "SELECT forget_cutoff, decay_weight, merge_similarity_threshold "
            "FROM policy_knobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return knobs
    finally:
        conn.close()
    if not row:
        return knobs
    for key, val in zip(
        ("forget_cutoff", "decay_weight", "merge_similarity_threshold"), row
    ):
        if val is not None:
            knobs[key] = float(val)
    return knobs


class AdaptivePolicyService:
    """Compute + persist tuned memory knobs from the recall→outcome signal.

    ``scores_db`` carries ``consolidation_runs`` + ``policy_knobs``; ``mem``
    is a ``MemoryService`` (or any object exposing
    ``get_effectiveness_scores()``) supplying the recall_log effectiveness
    population. ``mem`` may be None — the loop then tunes purely off the
    op-verdict audit.
    """

    def __init__(self, scores_db: str, mem: Optional[Any] = None):
        self._scores_db = scores_db
        self._mem = mem

    # -- signal reads --------------------------------------------------

    def _effectiveness(self) -> dict[str, dict]:
        if self._mem is None:
            return {}
        try:
            return self._mem.get_effectiveness_scores() or {}
        except Exception:
            return {}

    def op_verdict_accuracy(self) -> list[dict]:
        """Per-op_type verdict stats: row count + a simple accuracy proxy.

        Accuracy proxy = mean(confidence) over that op's consolidation_runs —
        a runner that keeps emitting low-confidence verdicts is, on this
        proxy, less accurate. Also surfaces the archive/keep split for forget.
        """
        if not Path(self._scores_db).exists():
            return []
        conn = sqlite3.connect(self._scores_db)
        try:
            rows = conn.execute(
                "SELECT COALESCE(op_type, 'reflection') AS op_type, "
                "       COUNT(*) AS n, AVG(COALESCE(confidence, 0.0)) AS acc, "
                "       output_json "
                "FROM consolidation_runs "
                "GROUP BY COALESCE(op_type, 'reflection')"
            ).fetchall()
            # Per-op decision split needs the raw rows; cheap second pass.
            decisions = conn.execute(
                "SELECT COALESCE(op_type, 'reflection') AS op_type, output_json "
                "FROM consolidation_runs"
            ).fetchall()
        finally:
            conn.close()
        split: dict[str, dict] = {}
        for op, oj in decisions:
            d = (json.loads(oj or "{}") or {}).get("decision") if oj else None
            bucket = split.setdefault(op, {})
            if d:
                bucket[d] = bucket.get(d, 0) + 1
        out = []
        for op, n, acc, _oj in rows:
            out.append({
                "op_type": op,
                "n": int(n),
                "accuracy": round(float(acc or 0.0), 3),
                "decisions": split.get(op, {}),
            })
        out.sort(key=lambda r: (-r["n"], r["op_type"]))
        return out

    # -- the rules -----------------------------------------------------

    def tune(self, project: Optional[str] = None) -> dict:
        """Compute the next knob set from the signal, persist it, return it.

        Starts from the currently-active knobs (so nudges compound across
        ticks toward a stable point) and applies small, clamped, defensible
        nudges. Every tune appends one ``policy_knobs`` row.
        """
        knobs = get_active_knobs(self._scores_db)
        eff = self._effectiveness()
        acc = self.op_verdict_accuracy()
        by_op = {r["op_type"]: r for r in acc}

        scores = [d.get("score", 0.0) for d in eff.values()]
        n_positive = sum(1 for s in scores if s > 0.25)
        n_total = len(scores)
        reasons: list[str] = []

        # Rule 1 — forget op archived while effective memories abound:
        # we are at risk of forgetting things that HELP -> forget LESS.
        forget_archives = (by_op.get("forget", {}).get("decisions", {})
                           .get("archive", 0))
        if forget_archives > 0 and n_positive >= 2:
            knobs["forget_cutoff"] = _clamp(
                "forget_cutoff", knobs["forget_cutoff"] + _STEP
            )
            reasons.append(
                f"raise forget_cutoff: {forget_archives} archives vs "
                f"{n_positive} effective memories"
            )

        # Rule 2 — the effective-memory population is large: decaying is
        # likely costing us helpful memories -> decay GENTLER.
        if n_total > 0 and n_positive / max(n_total, 1) >= 0.5 and n_positive >= 3:
            knobs["decay_weight"] = _clamp(
                "decay_weight", knobs["decay_weight"] - _DECAY_STEP
            )
            reasons.append(
                f"lower decay_weight: {n_positive}/{n_total} memories effective"
            )

        # Rule 3 — low-confidence merge verdicts -> demand MORE similarity
        # before merging (tighten the threshold up toward the ceiling).
        merge = by_op.get("merge")
        if merge and merge["n"] >= 3 and merge["accuracy"] < 0.5:
            knobs["merge_similarity_threshold"] = _clamp(
                "merge_similarity_threshold",
                knobs["merge_similarity_threshold"] + _MERGE_STEP,
            )
            reasons.append(
                f"tighten merge: mean confidence {merge['accuracy']} < 0.5"
            )

        if not reasons:
            reasons.append("no signal moved the knobs (steady state)")
        self._persist(knobs, rationale="; ".join(reasons))
        return knobs

    # -- persistence ---------------------------------------------------

    def _persist(self, knobs: dict, rationale: str = "") -> None:
        """Append one timestamped policy_knobs row."""
        if not Path(self._scores_db).exists():
            return
        conn = sqlite3.connect(self._scores_db)
        try:
            conn.execute(
                "INSERT INTO policy_knobs "
                "(forget_cutoff, decay_weight, merge_similarity_threshold, "
                " rationale) VALUES (?, ?, ?, ?)",
                (
                    float(knobs["forget_cutoff"]),
                    float(knobs["decay_weight"]),
                    float(knobs["merge_similarity_threshold"]),
                    rationale,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def history(self, limit: int = 50) -> list[dict]:
        """Newest-first policy_knobs rows for the /learning chart."""
        if not Path(self._scores_db).exists():
            return []
        conn = sqlite3.connect(self._scores_db)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, forget_cutoff, decay_weight, "
                "       merge_similarity_threshold, rationale, tuned_at "
                "FROM policy_knobs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
