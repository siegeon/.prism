"""RED scaffold (Tier 3) — adaptive policy loop.

Pins the acceptance criteria for `services/adaptive_policy.py`: a small
service that reads the recall→outcome signal (effectiveness +
consolidation_runs) and tunes 3 memory knobs (forget_cutoff,
decay_weight, merge_similarity_threshold), persisting each tuning to a
new scores.db table `policy_knobs` with a timestamped history.

Real seams, not unit echoes:
  * the `policy_knobs` table is created by Brain() init (schema migration);
  * crafted outcome traces move the knobs the RIGHT direction;
  * `get_active_knobs` round-trips the latest row through a SEPARATE
    sqlite connection (durability, not in-memory echo);
  * the forget runner's select() reads the TUNED cutoff, not the constant;
  * `GET /api/learning/policy` returns knobs + history + op_accuracy.

Every test FAILS today: adaptive_policy.py does not exist, policy_knobs
has no table, /api/learning/policy is unrouted.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _make_scores_db(tmp_path: Path) -> str:
    """Real scores.db with the consolidation + policy schema (Brain() init)."""
    from prism_service.engines.brain_engine import Brain

    scores = tmp_path / "scores.db"
    Brain(
        brain_db=str(tmp_path / "brain.db"),
        graph_db=str(tmp_path / "graph.db"),
        scores_db=str(scores),
    )
    return str(scores)


def _insert_run(scores_db: str, op_type: str, confidence: float,
                decision: str = "archive") -> None:
    import json
    import uuid
    c = sqlite3.connect(scores_db)
    try:
        c.execute(
            "INSERT INTO consolidation_runs "
            "(id, candidate_id, output_json, subagent_type, confidence, "
            " schema_valid, op_type) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (str(uuid.uuid4()), str(uuid.uuid4()),
             json.dumps({"decision": decision, "confidence": confidence}),
             op_type, confidence, op_type),
        )
        c.commit()
    finally:
        c.close()


class _FakeMem:
    """Stand-in for MemoryService.get_effectiveness_scores()."""

    def __init__(self, scores: dict):
        self._scores = scores

    def get_effectiveness_scores(self) -> dict:
        return self._scores


# ---------------------------------------------------------------------------
# (a) schema: policy_knobs table is created by Brain() init
# ---------------------------------------------------------------------------

def test_policy_knobs_table_exists(tmp_path):
    scores_db = _make_scores_db(tmp_path)
    conn = sqlite3.connect(scores_db)
    try:
        cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(policy_knobs)"
        ).fetchall()}
    finally:
        conn.close()
    assert cols, "policy_knobs table was not created by Brain() init"
    for c in ("forget_cutoff", "decay_weight", "merge_similarity_threshold",
              "tuned_at"):
        assert c in cols, f"policy_knobs missing column {c}: {cols}"


# ---------------------------------------------------------------------------
# (b) defaults: get_active_knobs returns the documented defaults when empty
# ---------------------------------------------------------------------------

def test_get_active_knobs_defaults_when_empty(tmp_path):
    from prism_service.services.adaptive_policy import (
        DEFAULT_KNOBS, get_active_knobs,
    )

    scores_db = _make_scores_db(tmp_path)
    knobs = get_active_knobs(scores_db)
    assert knobs["forget_cutoff"] == DEFAULT_KNOBS["forget_cutoff"]
    assert knobs["decay_weight"] == DEFAULT_KNOBS["decay_weight"]
    assert knobs["merge_similarity_threshold"] == (
        DEFAULT_KNOBS["merge_similarity_threshold"]
    )


# ---------------------------------------------------------------------------
# (c) tune persists + round-trips through a SEPARATE connection
# ---------------------------------------------------------------------------

def test_tune_persists_and_round_trips(tmp_path):
    from prism_service.services.adaptive_policy import (
        AdaptivePolicyService, get_active_knobs,
    )

    scores_db = _make_scores_db(tmp_path)
    svc = AdaptivePolicyService(scores_db, mem=_FakeMem({}))
    out = svc.tune()
    assert set(out) >= {
        "forget_cutoff", "decay_weight", "merge_similarity_threshold",
    }

    # Read back the latest persisted knobs through a fresh service path.
    active = get_active_knobs(scores_db)
    assert abs(active["forget_cutoff"] - out["forget_cutoff"]) < 1e-9
    assert abs(active["decay_weight"] - out["decay_weight"]) < 1e-9

    # History row landed.
    conn = sqlite3.connect(scores_db)
    try:
        n = conn.execute("SELECT COUNT(*) FROM policy_knobs").fetchone()[0]
    finally:
        conn.close()
    assert n == 1, f"tune() must write exactly one policy_knobs row, got {n}"


# ---------------------------------------------------------------------------
# (d) DIRECTION: effective memories forgotten -> RAISE forget_cutoff
# ---------------------------------------------------------------------------

def test_effective_memories_forgotten_raises_forget_cutoff(tmp_path):
    from prism_service.services.adaptive_policy import (
        AdaptivePolicyService, DEFAULT_KNOBS,
    )

    scores_db = _make_scores_db(tmp_path)
    # Forget op archived a batch with HIGH confidence, yet effectiveness
    # scores show those memories were HELPING outcomes (positive score).
    for _ in range(5):
        _insert_run(scores_db, "forget", 0.9, decision="archive")
    mem = _FakeMem({
        "m1": {"positive": 4, "negative": 0, "total": 4, "score": 1.0},
        "m2": {"positive": 3, "negative": 0, "total": 3, "score": 1.0},
    })
    svc = AdaptivePolicyService(scores_db, mem=mem)
    out = svc.tune()
    assert out["forget_cutoff"] > DEFAULT_KNOBS["forget_cutoff"], (
        "forgetting effective memories must RAISE forget_cutoff (forget less)"
    )


# ---------------------------------------------------------------------------
# (e) DIRECTION: high-effectiveness decaying -> LOWER decay_weight
# ---------------------------------------------------------------------------

def test_high_effectiveness_decaying_lowers_decay_weight(tmp_path):
    from prism_service.services.adaptive_policy import (
        AdaptivePolicyService, DEFAULT_KNOBS,
    )

    scores_db = _make_scores_db(tmp_path)
    # Strongly-effective memories dominate the signal -> we are decaying
    # things that help, so decay should be GENTLER (weight down).
    mem = _FakeMem({
        f"m{i}": {"positive": 5, "negative": 0, "total": 5, "score": 1.0}
        for i in range(6)
    })
    svc = AdaptivePolicyService(scores_db, mem=mem)
    out = svc.tune()
    assert out["decay_weight"] < DEFAULT_KNOBS["decay_weight"], (
        "high-effectiveness population must LOWER decay_weight (decay gentler)"
    )


# ---------------------------------------------------------------------------
# (f) clamp: knobs never escape their documented bounds
# ---------------------------------------------------------------------------

def test_knobs_are_clamped(tmp_path):
    from prism_service.services.adaptive_policy import (
        AdaptivePolicyService, KNOB_BOUNDS,
    )

    scores_db = _make_scores_db(tmp_path)
    # Pile on an extreme signal many times; the knob must not run away.
    mem = _FakeMem({
        f"m{i}": {"positive": 9, "negative": 0, "total": 9, "score": 1.0}
        for i in range(40)
    })
    svc = AdaptivePolicyService(scores_db, mem=mem)
    out = svc.tune()
    for _ in range(20):
        out = svc.tune()
    for knob, (lo, hi) in KNOB_BOUNDS.items():
        assert lo <= out[knob] <= hi, f"{knob}={out[knob]} escaped [{lo},{hi}]"


# ---------------------------------------------------------------------------
# (g) op verdict accuracy: per-op counts + a confidence-proxy accuracy
# ---------------------------------------------------------------------------

def test_op_verdict_accuracy(tmp_path):
    from prism_service.services.adaptive_policy import AdaptivePolicyService

    scores_db = _make_scores_db(tmp_path)
    _insert_run(scores_db, "forget", 0.8)
    _insert_run(scores_db, "forget", 0.6)
    _insert_run(scores_db, "merge", 0.9)
    svc = AdaptivePolicyService(scores_db, mem=_FakeMem({}))
    acc = svc.op_verdict_accuracy()
    by_op = {r["op_type"]: r for r in acc}
    assert by_op["forget"]["n"] == 2
    assert by_op["merge"]["n"] == 1
    # accuracy proxy == mean confidence, in [0,1]
    assert abs(by_op["forget"]["accuracy"] - 0.7) < 1e-6
    assert 0.0 <= by_op["merge"]["accuracy"] <= 1.0


# ---------------------------------------------------------------------------
# (h) CONSUMER: forget.select() reads the TUNED cutoff, not the constant
# ---------------------------------------------------------------------------

def test_forget_select_reads_tuned_cutoff(tmp_path, monkeypatch):
    """The forget runner must read forget_cutoff from get_active_knobs so the
    adaptive loop is not dead code. We assert the select() threshold tracks
    the persisted knob, not the hardcoded FADING_THRESHOLD constant."""
    from prism_service.services.adaptive_policy import AdaptivePolicyService
    from prism_service.services.memory_ops import forget as forget_mod

    scores_db = _make_scores_db(tmp_path)

    # Persist a deliberately distinctive cutoff.
    svc = AdaptivePolicyService(scores_db, mem=_FakeMem({}))
    svc._persist({"forget_cutoff": 7.5, "decay_weight": 1.0,
                  "merge_similarity_threshold": 0.85})

    captured = {}

    class _Mem:
        def fading_entries(self, threshold):
            captured["threshold"] = threshold
            return []

    class _Ctx:
        _data_dir = Path(scores_db).parent
        memory_svc = _Mem()

    monkeypatch.setattr(forget_mod, "get_project", lambda p: _Ctx(),
                        raising=False)
    monkeypatch.setattr(
        "prism_service.project_context.get_project", lambda p: _Ctx()
    )

    op = forget_mod.ForgetOperation()
    op.select("p")
    assert captured.get("threshold") == 7.5, (
        f"forget.select must use the tuned cutoff (7.5), "
        f"got {captured.get('threshold')!r} (still the constant?)"
    )


# ---------------------------------------------------------------------------
# (i) API: GET /api/learning/policy returns knobs + history + op_accuracy
# ---------------------------------------------------------------------------

def test_api_learning_policy(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    scores_db = _make_scores_db(tmp_path)
    _insert_run(scores_db, "forget", 0.8)

    from prism_service.services.adaptive_policy import AdaptivePolicyService
    AdaptivePolicyService(scores_db, mem=_FakeMem({})).tune()

    class _Ctx:
        _data_dir = Path(scores_db).parent

    monkeypatch.setattr(
        "prism_service.api.learning.get_project", lambda p: _Ctx()
    )

    from prism_service.main import app
    client = TestClient(app)
    resp = client.get("/api/learning/policy?project=p")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "knobs" in body and "history" in body and "op_accuracy" in body
    assert body["knobs"]["forget_cutoff"] is not None
    assert isinstance(body["history"], list) and len(body["history"]) >= 1
    ops = {r["op_type"] for r in body["op_accuracy"]}
    assert "forget" in ops
