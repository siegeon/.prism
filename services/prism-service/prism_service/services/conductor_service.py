"""Conductor service — wrapper over the Conductor engine with scores.db queries."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional


META_MIN_HOLDOUT_DELTA = 0.03
META_MAX_TOKEN_RATIO = 1.15
META_MAX_RETRY_DELTA = 0.0
META_MAX_FOLLOWUP_DELTA = 0.0
META_MAX_REVERT_DELTA = 0.0
META_MIN_SAMPLE_N = 5
META_REQUIRED_CONTEXTPACK_SCORE = 1.0
AUTO_MIN_OUTCOMES = 1

# Epsilon constants (mirror conductor_engine values)
EPSILON_START = 0.3
EPSILON_MIN = 0.05
EPSILON_DECAY = 0.05


def is_weak_proof(value: object) -> bool:
    """Ported from goalbuddy scripts/check-goal-state.mjs isWeakProof().

    A completion proof / oracle signal is "weak" when it is absent or a
    placeholder — i.e. it does not actually evidence the outcome. Used to
    flag (advisory) a green_gate close that carries no real proof.
    """
    if value is None:
        return True
    s = str(value).strip().lower()
    if s in ("", "unknown", "tbd", "todo", "none"):
        return True
    # placeholder tokens like "<fill me>" / "<observable signal>"
    return s.startswith("<") and s.endswith(">")


def green_gate_proof_note(files_modified: int, completion_proof: object) -> str:
    """Advisory note appended to a green_gate close (annotate, never block).

    Encodes the goalbuddy Judge doctrine "lots of files is not completion":
      * code changed (files_modified>0) but no real completion_proof
        -> BUSYWORK RISK: effort without demonstrated outcome.
      * nothing changed and no proof -> ORACLE: no completion signal at all.
      * a real completion_proof -> clean ('').
    """
    if not is_weak_proof(completion_proof):
        return ""
    if files_modified > 0:
        return (f"  ⚠ busywork risk: {files_modified} file-change(s) but no "
                f"completion_proof (effort ≠ outcome)")
    return "  ⚠ oracle: no completion_proof recorded"


def overlapping_allowed_files(file_lists: list) -> set:
    """Ported from goalbuddy scripts/parallel-plan.mjs: parallel workers are
    safe ONLY when their allowed_files sets are provably disjoint. Returns the
    set of files claimed by more than one worker (empty set == safe to run in
    parallel)."""
    seen: set = set()
    clash: set = set()
    for files in file_lists or []:
        cur = set(files or [])
        clash |= (cur & seen)
        seen |= cur
    return clash


def can_run_parallel(file_lists: list) -> bool:
    """True iff the given allowed_files sets are pairwise disjoint."""
    return not overlapping_allowed_files(file_lists)


class ConductorService:
    """Service layer for Conductor engine and scores.db queries.

    Provides orchestration methods and direct score database access
    for the UI and MCP layers.
    """

    def __init__(
        self,
        scores_db: str,
        enable_engine: bool = True,
        task_svc: Optional[Any] = None,
        verifier_svc: Optional[Any] = None,
    ) -> None:
        self._scores_db = scores_db
        self._conductor = None
        self._available = False
        # Conductor v2 (issue #79 [1/4]): optional TaskService reference
        # consumed by advance_task / gate_decide. Wired by ProjectContext
        # after both services exist; kept optional so legacy callers
        # (and the meta-conductor unit tests) can construct a bare
        # ConductorService without a TaskService.
        self._task_svc = task_svc
        # Conductor v2 (issue #79 [3/4]): optional VerifierService used by
        # gate_decide to convert a caller's 'approve' into a real
        # pass/fail decision. None = legacy behavior (trust the caller).
        self._verifier_svc = verifier_svc
        self._ensure_meta_schema()
        if not enable_engine:
            return
        try:
            from prism_service.engines.conductor_engine import Conductor

            self._conductor = Conductor()
            self._available = True
        except Exception as exc:
            print(
                f"ConductorService: Conductor unavailable ({exc})",
                file=sys.stderr,
            )

    # ------------------------------------------------------------------
    # Late binding for TaskService — ProjectContext wires this after
    # construction so the two services can stay laziness-friendly.
    # ------------------------------------------------------------------

    def attach_task_service(self, task_svc: Any) -> None:
        """Attach (or replace) the TaskService consumed by advance_task
        and gate_decide. No-op if already attached to the same instance.
        """
        self._task_svc = task_svc

    def attach_verifier_service(self, verifier_svc: Any) -> None:
        """Attach (or replace) the VerifierService consumed by gate_decide
        (issue #79 [3/4]). When None, gate_decide trusts the caller's
        action (legacy [1/4] behavior). When attached, 'approve' without
        override is verified against the prior step's validation kind.
        """
        self._verifier_svc = verifier_svc

    # ------------------------------------------------------------------
    # Delegated methods
    # ------------------------------------------------------------------

    def build_instruction(
        self,
        persona: str,
        step_id: str,
        difficulty: Optional[str] = None,
        story_context: Optional[str] = None,
    ) -> dict:
        """Build an agent instruction enriched with Brain context."""
        if not self._available or self._conductor is None:
            return {"instruction": "", "prompt_id": "", "available": False}
        try:
            result = self._conductor.build_agent_instruction(
                step_id=step_id,
                agent=persona,
                action=step_id,
                story_file=story_context or "",
            )
            return {
                "instruction": result,
                "prompt_id": self._conductor.last_prompt_id,
                "available": True,
            }
        except Exception as exc:
            return {"instruction": "", "prompt_id": "", "error": str(exc)}

    def record_outcome(
        self,
        prompt_id: str,
        persona: str,
        step_id: str,
        metrics: dict,
    ) -> None:
        """Record a step outcome for PSP scoring."""
        if not self._available or self._conductor is None:
            return
        self._conductor.record_outcome(prompt_id, persona, step_id, metrics)

    def reindex(self) -> int:
        """Trigger incremental reindex via Conductor."""
        if not self._available or self._conductor is None:
            return 0
        return self._conductor.incremental_reindex()

    # ------------------------------------------------------------------
    # Direct scores.db queries
    # ------------------------------------------------------------------

    def _scores_conn(self) -> sqlite3.Connection:
        """Open a read-only connection to scores.db."""
        conn = sqlite3.connect(self._scores_db, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def get_scores(
        self,
        persona: Optional[str] = None,
        step_id: Optional[str] = None,
    ) -> list[dict]:
        """Query score_aggregates from scores.db."""
        try:
            conn = self._scores_conn()
            clauses: list[str] = []
            params: list[str] = []
            if persona:
                clauses.append("persona = ?")
                params.append(persona)
            if step_id:
                clauses.append("step_id = ?")
                params.append(step_id)
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = conn.execute(
                f"SELECT * FROM score_aggregates{where} ORDER BY avg_score DESC",
                params,
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_variants(self, persona: Optional[str] = None) -> list[dict]:
        """Query prompt_variants from scores.db."""
        try:
            conn = self._scores_conn()
            if persona:
                rows = conn.execute(
                    "SELECT * FROM prompt_variants WHERE persona = ?",
                    (persona,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM prompt_variants").fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def get_retired(self) -> list[dict]:
        """Query retired_variants from scores.db."""
        try:
            conn = self._scores_conn()
            rows = conn.execute(
                "SELECT * FROM retired_variants ORDER BY retired_at DESC"
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Meta-Conductor: offline prompt-variant candidate loop
    # ------------------------------------------------------------------

    def _ensure_meta_schema(self) -> None:
        conn = self._scores_conn()
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS prompt_variants (
                prompt_id TEXT PRIMARY KEY,
                persona TEXT,
                content TEXT NOT NULL,
                source TEXT DEFAULT 'learned',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS prompt_scores (
                prompt_id TEXT,
                persona TEXT,
                step_id TEXT,
                score REAL,
                tokens_used INTEGER,
                context_tokens INTEGER,
                duration_s REAL,
                retries INTEGER,
                difficulty TEXT,
                tests_passed INTEGER,
                coverage_pct REAL,
                traceability_pct REAL,
                gate_passed INTEGER,
                probe_accuracy REAL,
                timestamp TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (prompt_id, persona, step_id, timestamp)
            );
            CREATE TABLE IF NOT EXISTS score_aggregates (
                prompt_id TEXT,
                persona TEXT,
                step_id TEXT,
                avg_score REAL DEFAULT 0.0,
                total_runs INTEGER DEFAULT 0,
                last_updated TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (prompt_id, persona, step_id)
            );
            CREATE TABLE IF NOT EXISTS meta_prompt_candidates (
                candidate_id TEXT PRIMARY KEY,
                prompt_id TEXT UNIQUE NOT NULL,
                persona TEXT NOT NULL,
                step_id TEXT NOT NULL,
                parent_prompt_id TEXT,
                content TEXT NOT NULL,
                rationale TEXT,
                generator TEXT,
                status TEXT DEFAULT 'proposed',
                created_at TEXT DEFAULT (datetime('now')),
                evaluated_at TEXT,
                promoted_at TEXT,
                decision_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_meta_prompt_candidates_status
                ON meta_prompt_candidates(status);
            CREATE INDEX IF NOT EXISTS idx_meta_prompt_candidates_persona_step
                ON meta_prompt_candidates(persona, step_id);
            CREATE TABLE IF NOT EXISTS meta_prompt_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT NOT NULL,
                baseline_score REAL,
                holdout_score REAL,
                train_score REAL,
                contextpack_score REAL,
                tests_passed INTEGER,
                retry_delta REAL,
                token_ratio REAL,
                followup_delta REAL,
                revert_delta REAL,
                sample_n INTEGER,
                score_delta REAL,
                passed INTEGER,
                reason TEXT,
                metrics_json TEXT,
                evaluated_at TEXT DEFAULT (datetime('now'))
            );
            """
        )
        conn.commit()
        conn.close()

    def _current_prompt_content(self, prompt_id: str) -> str:
        conn = self._scores_conn()
        row = conn.execute(
            "SELECT content FROM prompt_variants WHERE prompt_id = ?",
            (prompt_id,),
        ).fetchone()
        conn.close()
        if row:
            return str(row["content"])
        if "/" not in prompt_id:
            return ""
        persona, variant = prompt_id.split("/", 1)
        prompt_file = Path(__file__).parent.parent / "prompts" / persona / f"{variant}.md"
        try:
            return prompt_file.read_text(encoding="utf-8")
        except OSError:
            return ""

    def meta_brief(
        self,
        persona: str,
        step_id: str,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Return a deterministic brief for an external meta-agent.

        PRISM does not call an LLM here. The caller can use this packet to
        draft a prompt variant, then submit it back through propose/evaluate.
        """
        self._ensure_meta_schema()
        scores = self.get_scores(persona=persona, step_id=step_id)
        current = scores[0] if scores else {
            "prompt_id": f"{persona}/default",
            "avg_score": 0.0,
            "total_runs": 0,
        }
        conn = self._scores_conn()
        top = conn.execute(
            "SELECT prompt_id, score, tokens_used, duration_s, retries, timestamp "
            "FROM prompt_scores WHERE persona=? AND step_id=? "
            "ORDER BY score DESC LIMIT ?",
            (persona, step_id, int(limit)),
        ).fetchall()
        low = conn.execute(
            "SELECT prompt_id, score, tokens_used, duration_s, retries, timestamp "
            "FROM prompt_scores WHERE persona=? AND step_id=? "
            "ORDER BY score ASC LIMIT ?",
            (persona, step_id, int(limit)),
        ).fetchall()
        conn.close()
        prompt_id = str(current.get("prompt_id") or f"{persona}/default")
        return {
            "schema": "prism.meta_conductor.brief.v1",
            "persona": persona,
            "step_id": step_id,
            "current_best": current,
            "current_prompt": self._current_prompt_content(prompt_id),
            "top_outcomes": [dict(r) for r in top],
            "low_outcomes": [dict(r) for r in low],
            "rules": [
                "Submit prompt text only; PRISM owns storage and promotion.",
                "Do not change MCP tool names, context-pack schema, or install hooks.",
                "Optimize for holdout task quality, not live-score gaming.",
            ],
            "promotion_thresholds": self.meta_thresholds(),
        }

    def meta_thresholds(self) -> dict[str, Any]:
        return {
            "min_holdout_delta": META_MIN_HOLDOUT_DELTA,
            "max_token_ratio": META_MAX_TOKEN_RATIO,
            "max_retry_delta": META_MAX_RETRY_DELTA,
            "max_followup_delta": META_MAX_FOLLOWUP_DELTA,
            "max_revert_delta": META_MAX_REVERT_DELTA,
            "min_sample_n": META_MIN_SAMPLE_N,
            "required_contextpack_score": META_REQUIRED_CONTEXTPACK_SCORE,
            "tests_passed_required": True,
        }

    def auto_meta_candidate(
        self,
        *,
        persona: str,
        step_id: str,
        limit: int = 5,
        metrics: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Generate a deterministic prompt candidate from outcome traces.

        This is the no-LLM automatic path. PRISM mines existing scores and
        failure signals, writes a candidate through the same propose path, and
        optionally evaluates it if the caller supplies real benchmark metrics.
        """
        brief = self.meta_brief(persona=persona, step_id=step_id, limit=limit)
        stats = self._meta_outcome_stats(persona, step_id)
        if stats["sample_n"] < AUTO_MIN_OUTCOMES:
            return {
                "created": False,
                "reason": "no outcome traces for persona/step",
                "brief": brief,
                "stats": stats,
            }

        rules = self._auto_prompt_rules(stats)
        parent = str(brief["current_best"].get("prompt_id") or f"{persona}/default")
        content = self._render_auto_prompt(
            persona=persona,
            step_id=step_id,
            current_prompt=str(brief.get("current_prompt") or ""),
            rules=rules,
        )
        rationale = (
            "Deterministic Meta-Conductor candidate from PSP outcome traces: "
            + "; ".join(stats["signals"])
        )
        proposed = self.propose_meta_candidate(
            persona=persona,
            step_id=step_id,
            content=content,
            parent_prompt_id=parent,
            rationale=rationale,
            generator="prism-rule-meta-conductor",
        )
        result: dict[str, Any] = {
            "created": True,
            "candidate": proposed["candidate"],
            "rules_applied": rules,
            "stats": stats,
            "promotion_thresholds": proposed["promotion_thresholds"],
        }
        if metrics is not None:
            result["evaluation"] = self.evaluate_meta_candidate(
                proposed["candidate"]["candidate_id"],
                metrics,
            )
        return result

    def _meta_outcome_stats(self, persona: str, step_id: str) -> dict[str, Any]:
        conn = self._scores_conn()
        rows = conn.execute(
            "SELECT score, tokens_used, duration_s, retries, tests_passed, "
            "gate_passed, coverage_pct, traceability_pct, probe_accuracy "
            "FROM prompt_scores WHERE persona=? AND step_id=?",
            (persona, step_id),
        ).fetchall()
        conn.close()
        sample_n = len(rows)
        if not rows:
            return {
                "sample_n": 0,
                "avg_score": 0.0,
                "avg_tokens": 0.0,
                "avg_retries": 0.0,
                "test_fail_rate": 0.0,
                "gate_fail_rate": 0.0,
                "low_traceability_rate": 0.0,
                "signals": [],
            }

        def present(name: str) -> list[float]:
            vals: list[float] = []
            for row in rows:
                value = row[name]
                if value is not None:
                    vals.append(float(value))
            return vals

        scores = present("score")
        tokens = present("tokens_used")
        retries = present("retries")
        tests = present("tests_passed")
        gates = present("gate_passed")
        traceability = present("traceability_pct")
        coverage = present("coverage_pct")

        def avg(vals: list[float]) -> float:
            return sum(vals) / len(vals) if vals else 0.0

        test_fail_rate = (
            sum(1 for v in tests if v <= 0.0) / len(tests) if tests else 0.0
        )
        gate_fail_rate = (
            sum(1 for v in gates if v <= 0.0) / len(gates) if gates else 0.0
        )
        low_traceability_rate = (
            sum(1 for v in traceability if v < 0.8) / len(traceability)
            if traceability else 0.0
        )
        low_coverage_rate = (
            sum(1 for v in coverage if v < 0.7) / len(coverage)
            if coverage else 0.0
        )
        stats = {
            "sample_n": sample_n,
            "avg_score": round(avg(scores), 4),
            "avg_tokens": round(avg(tokens), 2),
            "avg_retries": round(avg(retries), 2),
            "test_fail_rate": round(test_fail_rate, 4),
            "gate_fail_rate": round(gate_fail_rate, 4),
            "low_traceability_rate": round(low_traceability_rate, 4),
            "low_coverage_rate": round(low_coverage_rate, 4),
            "signals": [],
        }
        signals: list[str] = []
        if stats["avg_retries"] > 0:
            signals.append(f"avg_retries={stats['avg_retries']}")
        if test_fail_rate > 0:
            signals.append(f"test_fail_rate={test_fail_rate:.2f}")
        if gate_fail_rate > 0:
            signals.append(f"gate_fail_rate={gate_fail_rate:.2f}")
        if low_traceability_rate > 0:
            signals.append(f"low_traceability_rate={low_traceability_rate:.2f}")
        if low_coverage_rate > 0:
            signals.append(f"low_coverage_rate={low_coverage_rate:.2f}")
        if stats["avg_tokens"] > 6000:
            signals.append(f"avg_tokens={stats['avg_tokens']}")
        if stats["avg_score"] < 0.7:
            signals.append(f"avg_score={stats['avg_score']}")
        if not signals:
            signals.append("stable_outcomes")
        stats["signals"] = signals
        return stats

    def _auto_prompt_rules(self, stats: dict[str, Any]) -> list[str]:
        rules = [
            "Start from the PRISM context pack and preserve MCP tool contracts.",
        ]
        if stats["avg_retries"] > 0 or stats["gate_fail_rate"] > 0:
            rules.append(
                "Before editing, identify the smallest behavior change and inspect the directly affected files."
            )
        if stats["test_fail_rate"] > 0 or stats["gate_fail_rate"] > 0:
            rules.append(
                "Before completion, run the narrowest relevant verification command and report the exact result."
            )
        if stats["low_traceability_rate"] > 0:
            rules.append(
                "Map each requirement to the files or tests that prove it before declaring the task done."
            )
        if stats["low_coverage_rate"] > 0:
            rules.append(
                "Prefer adding or updating focused regression tests when behavior changes."
            )
        if stats["avg_tokens"] > 6000:
            rules.append(
                "Keep context compact: cite only source files and PRISM memories that directly affect the change."
            )
        if stats["avg_score"] < 0.7:
            rules.append(
                "Call out residual risk explicitly and avoid broad refactors unless required by the task."
            )
        if len(rules) == 1:
            rules.append(
                "Keep the existing working pattern, but make verification and residual risk explicit."
            )
        return rules

    def _render_auto_prompt(
        self,
        *,
        persona: str,
        step_id: str,
        current_prompt: str,
        rules: list[str],
    ) -> str:
        base = current_prompt.strip()
        if not base:
            base = (
                f"# {persona} {step_id}\n"
                "Use PRISM MCP context, task state, memory, and Brain results "
                "before acting."
            )
        bullets = "\n".join(f"- {rule}" for rule in rules)
        return (
            f"{base}\n\n"
            "## Meta-Conductor adjustments\n"
            "These deterministic adjustments were generated from PRISM outcome "
            "signals, not by an LLM.\n"
            f"{bullets}"
        )

    def propose_meta_candidate(
        self,
        *,
        persona: str,
        step_id: str,
        content: str,
        parent_prompt_id: str = "",
        rationale: str = "",
        generator: str = "",
    ) -> dict[str, Any]:
        self._ensure_meta_schema()
        normalized = content.strip()
        if not normalized:
            raise ValueError("candidate content must not be empty")
        parent = parent_prompt_id or f"{persona}/default"
        digest = hashlib.sha256(
            f"{persona}\0{step_id}\0{parent}\0{normalized}".encode("utf-8")
        ).hexdigest()[:12]
        candidate_id = f"mc-{digest}"
        prompt_id = f"{persona}/meta-{digest}"
        conn = self._scores_conn()
        conn.execute(
            "INSERT OR REPLACE INTO meta_prompt_candidates "
            "(candidate_id, prompt_id, persona, step_id, parent_prompt_id, "
            " content, rationale, generator, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
            " COALESCE((SELECT status FROM meta_prompt_candidates WHERE candidate_id=?), 'proposed'))",
            (
                candidate_id,
                prompt_id,
                persona,
                step_id,
                parent,
                normalized,
                rationale,
                generator,
                candidate_id,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM meta_prompt_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        conn.close()
        return {
            "candidate": dict(row),
            "promotion_thresholds": self.meta_thresholds(),
        }

    def evaluate_meta_candidate(
        self,
        candidate_id: str,
        metrics: dict[str, Any],
    ) -> dict[str, Any]:
        self._ensure_meta_schema()
        conn = self._scores_conn()
        cand = conn.execute(
            "SELECT * FROM meta_prompt_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        if cand is None:
            conn.close()
            raise ValueError(f"unknown candidate_id: {candidate_id}")

        decision = self._meta_decision(metrics)
        now_expr = "datetime('now')"
        conn.execute(
            "INSERT INTO meta_prompt_evaluations "
            "(candidate_id, baseline_score, holdout_score, train_score, "
            " contextpack_score, tests_passed, retry_delta, token_ratio, "
            " followup_delta, revert_delta, sample_n, score_delta, passed, "
            " reason, metrics_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate_id,
                decision["baseline_score"],
                decision["holdout_score"],
                decision["train_score"],
                decision["contextpack_score"],
                1 if decision["tests_passed"] else 0,
                decision["retry_delta"],
                decision["token_ratio"],
                decision["followup_delta"],
                decision["revert_delta"],
                decision["sample_n"],
                decision["score_delta"],
                1 if decision["passed"] else 0,
                decision["reason"],
                json.dumps(metrics, sort_keys=True, default=str),
            ),
        )
        if decision["passed"]:
            conn.execute(
                "INSERT OR REPLACE INTO prompt_variants "
                "(prompt_id, persona, content, source) VALUES (?, ?, ?, 'meta-conductor')",
                (cand["prompt_id"], cand["persona"], cand["content"]),
            )
            conn.execute(
                f"UPDATE meta_prompt_candidates SET status='promoted', "
                f"evaluated_at={now_expr}, promoted_at={now_expr}, decision_json=? "
                "WHERE candidate_id=?",
                (json.dumps(decision, sort_keys=True), candidate_id),
            )
        else:
            conn.execute(
                f"UPDATE meta_prompt_candidates SET status='rejected', "
                f"evaluated_at={now_expr}, decision_json=? WHERE candidate_id=?",
                (json.dumps(decision, sort_keys=True), candidate_id),
            )
        conn.commit()
        updated = conn.execute(
            "SELECT * FROM meta_prompt_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()
        conn.close()
        return {
            "candidate": dict(updated),
            "decision": decision,
            "promoted": bool(decision["passed"]),
        }

    def _meta_decision(self, metrics: dict[str, Any]) -> dict[str, Any]:
        def f(name: str, default: float = 0.0) -> float:
            value = metrics.get(name, default)
            return float(value if value is not None else default)

        baseline = f("baseline_score")
        holdout = f("holdout_score")
        train = f("train_score")
        contextpack = f("contextpack_score")
        token_ratio = f("token_ratio", 999.0)
        retry_delta = f("retry_delta", 999.0)
        followup_delta = f("followup_delta", 999.0)
        revert_delta = f("revert_delta", 999.0)
        sample_n = int(metrics.get("sample_n") or 0)
        tests_passed = bool(metrics.get("tests_passed"))
        score_delta = holdout - baseline

        failures: list[str] = []
        if sample_n < META_MIN_SAMPLE_N:
            failures.append(f"sample_n {sample_n} < {META_MIN_SAMPLE_N}")
        if score_delta < META_MIN_HOLDOUT_DELTA:
            failures.append(
                f"holdout_delta {score_delta:.3f} < {META_MIN_HOLDOUT_DELTA:.3f}"
            )
        if contextpack < META_REQUIRED_CONTEXTPACK_SCORE:
            failures.append(
                f"contextpack_score {contextpack:.3f} < "
                f"{META_REQUIRED_CONTEXTPACK_SCORE:.3f}"
            )
        if not tests_passed:
            failures.append("tests_passed is false")
        if token_ratio > META_MAX_TOKEN_RATIO:
            failures.append(f"token_ratio {token_ratio:.3f} > {META_MAX_TOKEN_RATIO:.3f}")
        if retry_delta > META_MAX_RETRY_DELTA:
            failures.append(f"retry_delta {retry_delta:.3f} > {META_MAX_RETRY_DELTA:.3f}")
        if followup_delta > META_MAX_FOLLOWUP_DELTA:
            failures.append(
                f"followup_delta {followup_delta:.3f} > {META_MAX_FOLLOWUP_DELTA:.3f}"
            )
        if revert_delta > META_MAX_REVERT_DELTA:
            failures.append(f"revert_delta {revert_delta:.3f} > {META_MAX_REVERT_DELTA:.3f}")

        return {
            "passed": not failures,
            "reason": "passed" if not failures else "; ".join(failures),
            "baseline_score": baseline,
            "holdout_score": holdout,
            "train_score": train,
            "contextpack_score": contextpack,
            "tests_passed": tests_passed,
            "retry_delta": retry_delta,
            "token_ratio": token_ratio,
            "followup_delta": followup_delta,
            "revert_delta": revert_delta,
            "sample_n": sample_n,
            "score_delta": score_delta,
        }

    # ------------------------------------------------------------------
    # Conductor v2 — per-task workflow state machine (issue #79 [1/4])
    # ------------------------------------------------------------------
    #
    # advance_task / gate_decide are the only sanctioned entry points
    # for moving a task across WORKFLOW_STEPS. They consult and write
    # Task.workflow_step / .gate_state / .gate_reason via TaskService,
    # and append explicit task_history rows for every transition so the
    # audit log captures who moved the task and why.
    #
    # Out of scope for [1/4]:
    #   * No MCP surface (deliverable [2/4]).
    #   * No verifier_service consultation (deliverable [3/4]).
    #   * No per-task override of WORKFLOW_STEPS — every task walks the
    #     default sequence from models.workflow.

    @staticmethod
    def _workflow_steps() -> list[dict]:
        """Local import avoids a circular dep with models.workflow."""
        from prism_service.models.workflow import WORKFLOW_STEPS

        return WORKFLOW_STEPS

    @classmethod
    def _step_index(cls, step_id: str) -> int:
        """Return the position of step_id in WORKFLOW_STEPS, or -1.

        An empty step_id means the task has not entered the workflow,
        which is equivalent to index -1 (the next step is index 0).
        """
        if not step_id:
            return -1
        for i, step in enumerate(cls._workflow_steps()):
            if step["id"] == step_id:
                return i
        return -1

    @classmethod
    def _step_by_id(cls, step_id: str) -> Optional[dict]:
        if not step_id:
            return None
        for step in cls._workflow_steps():
            if step["id"] == step_id:
                return step
        return None

    def advance_task(
        self,
        task_id: str,
        validation: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> dict:
        """Move a task to the next entry in WORKFLOW_STEPS.

        Rules:
          * Task with workflow_step='' enters the workflow at step 0.
          * If the *current* step is a gate and gate_state='pending', the
            transition is refused — the gate must be decided first.
          * After moving, if the *new* step is a gate, gate_state is set
            to 'pending' (caller must use gate_decide to release it).
          * Every transition appends a task_history row.

        Returns a dict shaped:
          {'ok': bool, 'task_id', 'from_step', 'to_step',
           'gate_state', 'reason' (on refusal)}
        """
        if self._task_svc is None:
            return {"ok": False, "task_id": task_id,
                    "reason": "no TaskService attached"}
        task = self._task_svc.get(task_id)
        if task is None:
            return {"ok": False, "task_id": task_id,
                    "reason": "unknown task"}

        steps = self._workflow_steps()
        if not steps:
            return {"ok": False, "task_id": task_id,
                    "reason": "WORKFLOW_STEPS is empty"}

        current_id = task.workflow_step or ""
        current_step = self._step_by_id(current_id)

        # Refuse if we're sitting on a gate that hasn't been decided.
        if (current_step is not None
                and current_step["type"] == "gate"
                and task.gate_state == "pending"):
            return {
                "ok": False,
                "task_id": task_id,
                "from_step": current_id,
                "to_step": current_id,
                "gate_state": task.gate_state,
                "reason": (
                    f"gate '{current_id}' is pending; "
                    "call gate_decide before advancing"
                ),
            }

        current_index = self._step_index(current_id)
        next_index = current_index + 1
        if next_index >= len(steps):
            return {
                "ok": False,
                "task_id": task_id,
                "from_step": current_id,
                "to_step": current_id,
                "gate_state": task.gate_state,
                "reason": "task is already at the final workflow step",
            }

        next_step = steps[next_index]
        next_id = next_step["id"]
        new_gate_state = (
            "pending" if next_step["type"] == "gate" else "none"
        )
        # Clear stale gate_reason whenever we leave a gate.
        new_gate_reason = task.gate_reason if new_gate_state == "pending" else ""

        self._task_svc.update(
            task_id,
            workflow_step=next_id,
            gate_state=new_gate_state,
            gate_reason=new_gate_reason,
        )

        detail_bits = [f"from={current_id or '<start>'}", f"to={next_id}"]
        if validation:
            detail_bits.append(f"validation={validation}")
        if new_gate_state == "pending":
            detail_bits.append("gate=pending")
        self._task_svc.record_history(
            task_id,
            action="advance_task",
            details="; ".join(detail_bits),
            actor="conductor",
        )

        # Conductor-path auto-writer: stamp/refresh the task_sessions row
        # from the carried task_id + session so the association is
        # captured even if the session never reaches the Stop hook.
        self._stamp_session(task_id, session_id)

        return {
            "ok": True,
            "task_id": task_id,
            "from_step": current_id,
            "to_step": next_id,
            "gate_state": new_gate_state,
        }

    def _stamp_session(
        self, task_id: str, session_id: Optional[str],
    ) -> None:
        """Best-effort upsert of a task_sessions row via the single
        TaskService writer. No-op when no session is carried or the
        writer is unavailable — must never break a transition."""
        if not session_id:
            return
        link = getattr(self._task_svc, "link_session", None)
        if not callable(link):
            return
        try:
            link(task_id, session_id)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Gate verification helpers (issue #79 [3/4])
    # ------------------------------------------------------------------

    @classmethod
    def _validation_for_gate(cls, gate_step_id: str) -> Optional[str]:
        """Return the validation kind the verifier should check at this
        gate. By convention, a gate inherits its expectation from the
        immediately preceding step's ``validation`` field
        (e.g. ``red_gate`` follows ``write_failing_tests`` whose
        validation is ``red_with_trace``)."""
        steps = cls._workflow_steps()
        idx = cls._step_index(gate_step_id)
        if idx <= 0:
            return None
        for prev in reversed(steps[:idx]):
            if prev.get("validation"):
                return str(prev["validation"])
        return None

    # Mapping: validation kind -> (allowed verifier statuses,
    # allowed tier0 statuses, human-readable expectation). Manual
    # kinds (story_complete, plan_coverage) have value None — we can't
    # verify them mechanically and a human must use override.
    _VERIFIER_RULES: dict[str, Optional[dict]] = {
        "red_with_trace": {
            "expect_status": ("fail",),
            "expect_tier0": ("fail",),
            "expectation": (
                "red_with_trace expects verifier.status=fail with "
                "tier0=fail (failing test scaffold landed)"
            ),
        },
        "green": {
            "expect_status": ("pass",),
            "expect_tier0": ("pass", "not-run"),
            "expectation": "green expects verifier.status=pass with tier0=pass",
        },
        "green_full": {
            "expect_status": ("pass",),
            "expect_tier0": ("pass", "not-run"),
            "expectation": "green_full expects verifier.status=pass with tier0=pass",
        },
        "story_complete": None,
        "plan_coverage": None,
    }

    def _verify_gate(self, task, gate_step_id: str) -> dict:
        """Consult the attached VerifierService for the gate's expected
        validation. Returns a dict shaped:
          {'verified': bool|None, 'reason': str,
           'verifier': <raw verifier dict or None>,
           'validation': <kind or None>}
        'verified' is True/False after a verifier run, or None when no
        verifier is attached or the validation is a manual kind."""
        validation = self._validation_for_gate(gate_step_id)
        if validation is None:
            return {"verified": None, "reason": "gate has no validation kind",
                    "verifier": None, "validation": None}
        rule = self._VERIFIER_RULES.get(validation)
        if rule is None:
            return {
                "verified": None,
                "reason": (
                    f"validation {validation!r} requires manual review; "
                    "re-call gate_decide with override=True"
                ),
                "verifier": None,
                "validation": validation,
            }
        # gate_decide short-circuits when self._verifier_svc is None
        # (legacy trust-caller path). _verify_gate is only called when
        # a verifier is attached, so we don't need a None-check here.
        try:
            v = self._verifier_svc.run(
                task_id=task.id,
                # story_file gives a useful audit anchor even though
                # VerifierService.run treats workspace as primary scope.
            )
        except Exception as exc:
            return {
                "verified": False,
                "reason": f"verifier raised {type(exc).__name__}: {exc}",
                "verifier": None,
                "validation": validation,
            }
        status = str(v.get("status") or "")
        tier0 = str(v.get("tier0") or "")
        ok_status = status in rule["expect_status"]
        ok_tier0 = tier0 in rule["expect_tier0"]
        if ok_status and ok_tier0:
            return {
                "verified": True,
                "reason": (
                    f"verifier passed: status={status} tier0={tier0} "
                    f"({rule['expectation']})"
                ),
                "verifier": v,
                "validation": validation,
            }
        summary = v.get("summary") or f"status={status} tier0={tier0}"
        return {
            "verified": False,
            "reason": (
                f"verifier rejected: {summary}; "
                f"{rule['expectation']}"
            ),
            "verifier": v,
            "validation": validation,
        }

    def gate_decide(
        self,
        task_id: str,
        action: str,
        reason: str = "",
        override: bool = False,
        session_id: Optional[str] = None,
    ) -> dict:
        """Resolve a pending gate on a task.

        action='approve' flips gate_state to 'passed' and auto-advances
        past the gate to the next non-gate step. action='reject' flips
        gate_state to 'failed' and stores reason in task.gate_reason;
        reject does NOT auto-advance.

        When ``override`` is False (the default) and a VerifierService is
        attached, action='approve' first calls verifier_service.run() and
        only releases the gate if the verifier confirms the prior step's
        validation kind. Manual-only validation kinds (story_complete,
        plan_coverage) require ``override=True``. When ``override`` is
        True, the verifier is bypassed and the audit row carries
        actor='manual-override' plus the supplied reason.

        Returns a dict shaped:
          {'ok': bool, 'task_id', 'gate_step',
           'gate_state', 'to_step' (on approve), 'reason' (on refusal),
           'verifier' (when a verifier run informed the decision)}
        """
        if action not in ("approve", "reject"):
            return {"ok": False, "task_id": task_id,
                    "reason": f"unknown action {action!r}; "
                              "expected 'approve' or 'reject'"}
        if self._task_svc is None:
            return {"ok": False, "task_id": task_id,
                    "reason": "no TaskService attached"}
        task = self._task_svc.get(task_id)
        if task is None:
            return {"ok": False, "task_id": task_id,
                    "reason": "unknown task"}

        # Conductor-path auto-writer: stamp/refresh the task_sessions row
        # from the carried task_id + session on every gate decision.
        self._stamp_session(task_id, session_id)

        current_step = self._step_by_id(task.workflow_step)
        if current_step is None or current_step["type"] != "gate":
            return {
                "ok": False,
                "task_id": task_id,
                "gate_step": task.workflow_step,
                "gate_state": task.gate_state,
                "reason": "task is not currently on a gate step",
            }
        # Conductor v2 follow-up (#79): allow manual recovery on failed
        # gates. An explicit override=True on action='approve' supersedes
        # the verifier's earlier ruling; the audit row tags actor=
        # 'manual-override' so the recovery stays visible in task_history.
        # 'reject' on a failed gate is still pointless (already failed).
        if task.gate_state == "failed":
            if not (action == "approve" and override):
                return {
                    "ok": False,
                    "task_id": task_id,
                    "gate_step": task.workflow_step,
                    "gate_state": task.gate_state,
                    "reason": (
                        "gate_state is 'failed'; recovery requires "
                        "action='approve' with override=True"
                    ),
                }
        elif task.gate_state != "pending":
            return {
                "ok": False,
                "task_id": task_id,
                "gate_step": task.workflow_step,
                "gate_state": task.gate_state,
                "reason": (
                    f"gate_state is {task.gate_state!r}; "
                    "gate_decide only acts on 'pending' (or 'failed' with override)"
                ),
            }

        gate_step_id = task.workflow_step

        if action == "reject":
            self._task_svc.update(
                task_id,
                gate_state="failed",
                gate_reason=reason,
            )
            self._task_svc.record_history(
                task_id,
                action="gate_decide",
                details=f"gate={gate_step_id}; action=reject; reason={reason}",
                actor="conductor",
            )
            return {
                "ok": True,
                "task_id": task_id,
                "gate_step": gate_step_id,
                "gate_state": "failed",
            }

        # action == 'approve' - validation evidence is REQUIRED.
        # Every approve must describe what was used to satisfy the gate
        # (test run, screenshot, manual review, etc.). The reason text
        # is the validation; without it the gate decision is opaque.
        # This rule applies even when a verifier is consulted - the human
        # narrative augments the machine check.
        if not (reason and reason.strip()):
            return {
                "ok": False,
                "task_id": task_id,
                "gate_step": gate_step_id,
                "gate_state": task.gate_state,
                "reason": (
                    "approve requires reason describing the validation "
                    "used (test run, screenshot, manual review, etc.)"
                ),
            }
        verifier_payload: Optional[dict] = None
        verifier_validation: Optional[str] = None
        verifier_reason = ""

        if override:
            # Manual override path — bypass the verifier entirely but
            # tag the audit row so the override is auditable.
            actor = "manual-override"
            detail_bits = [
                f"gate={gate_step_id}",
                "action=approve",
                "override=True",
            ]
            if reason:
                detail_bits.append(f"reason={reason}")
        elif self._verifier_svc is None:
            # Legacy [1/4] behavior — no verifier wired (bare
            # ConductorService used by unit tests and meta-only
            # callers). Trust the caller's approve. ProjectContext
            # always wires a verifier, so this path only fires for
            # explicit no-verifier construction.
            actor = "conductor"
            detail_bits = [f"gate={gate_step_id}", "action=approve"]
            if reason:
                detail_bits.append(f"reason={reason}")
        else:
            # Verifier-driven path. Look up the prior step's validation
            # kind and consult VerifierService. If the verifier rejects
            # or no verifier is attached, fail the gate (do NOT advance)
            # with the verifier's reason recorded on the task.
            outcome = self._verify_gate(task, gate_step_id)
            verifier_payload = outcome.get("verifier")
            verifier_validation = outcome.get("validation")
            verifier_reason = outcome.get("reason", "")
            if outcome["verified"] is not True:
                self._task_svc.update(
                    task_id,
                    gate_state="failed",
                    gate_reason=verifier_reason,
                )
                self._task_svc.record_history(
                    task_id,
                    action="gate_decide",
                    details=(
                        f"gate={gate_step_id}; action=approve; "
                        f"verifier=fail; validation="
                        f"{verifier_validation or 'none'}; "
                        f"reason={verifier_reason}"
                    ),
                    actor="conductor",
                )
                refusal = {
                    "ok": False,
                    "task_id": task_id,
                    "gate_step": gate_step_id,
                    "gate_state": "failed",
                    "reason": verifier_reason,
                    "validation": verifier_validation,
                }
                if verifier_payload is not None:
                    refusal["verifier"] = verifier_payload
                return refusal
            actor = "conductor"
            detail_bits = [
                f"gate={gate_step_id}",
                "action=approve",
                f"verifier=pass; validation={verifier_validation}",
            ]
            if reason:
                detail_bits.append(f"reason={reason}")

        # Persist validation evidence to the row so it surfaces wherever
        # gate_reason is rendered (TaskDetailPage, swimlane tooltips).
        # reason is required upstream, so it's always present here.
        if actor == "manual-override":
            passed_gate_reason = f"manual override: {reason}"
        elif verifier_validation:
            passed_gate_reason = f"verified ({verifier_validation}): {reason}"
        else:
            passed_gate_reason = reason
        # Oracle + anti-busywork (goalbuddy): at the terminal green_gate, flag a
        # close with no real completion_proof — and escalate to BUSYWORK RISK
        # when code churned without an outcome ("lots of files is not
        # completion"). Advisory per the hooks doctrine — annotate the reason,
        # never block — so it surfaces without breaking override-driven closes.
        if gate_step_id == "green_gate":
            _proof = getattr(self._task_svc.get(task_id), "completion_proof", "")
            try:
                _churn = sum(int(s.get("files_modified", 0) or 0)
                             for s in self._task_svc.sessions_for_task(task_id))
            except Exception:
                _churn = 0
            passed_gate_reason += green_gate_proof_note(_churn, _proof)
        self._task_svc.update(
            task_id,
            gate_state="passed",
            gate_reason=passed_gate_reason,
        )
        self._task_svc.record_history(
            task_id,
            action="gate_decide",
            details="; ".join(detail_bits),
            actor=actor,
        )

        advance_result = self.advance_task(task_id)
        response: dict = {
            "ok": True,
            "task_id": task_id,
            "gate_step": gate_step_id,
            "gate_state": "passed",
            "to_step": advance_result.get("to_step", gate_step_id),
            "auto_advanced": bool(advance_result.get("ok")),
        }
        if verifier_payload is not None:
            response["verifier"] = verifier_payload
        if verifier_validation is not None:
            response["validation"] = verifier_validation
        if override:
            response["override"] = True
        return response

    # Session-id prefixes used by smoke tests, dogfood probes, and the
    # bench harness. Rows with these ids carry near-zero token counts
    # and aren't real sessions — including them in averages drags the
    # mean toward zero and makes real work look like inflation. Filter
    # them out by default; pass include_smoke=True to see everything.
    _SMOKE_SESSION_PREFIXES: tuple[str, ...] = (
        "test-",
        "manual-",
        "sse-smoke-",
        "bridge-",
        "dogfood-",
        "hook-migration-",
        "diagnose-",
        "smoke-",
        "probe-",
    )

    @classmethod
    def _is_smoke_session(cls, row: dict) -> bool:
        """True if this row is a smoke/probe test, not a real session."""
        sid = (row.get("session_id") or "").lower()
        if any(sid.startswith(p) for p in cls._SMOKE_SESSION_PREFIXES):
            return True
        # Rows with zero tokens are incomplete/aborted records — the Stop
        # hook fired but never read the transcript. They aren't useful
        # signal, just noise on the mean.
        if (row.get("tokens_used") or 0) == 0:
            return True
        return False

    def get_session_outcomes(
        self, limit: int = 50, include_smoke: bool = False,
    ) -> list[dict]:
        """Query recent session outcomes from scores.db.

        Reads the ``session_outcomes`` table populated by
        ``record_session_outcome`` (served by the MCP and written by the
        Stop hook that prism_install ships). Maps DB columns onto the
        keys the /sessions UI expects (id, session_id, duration,
        tokens, files_modified, recorded_at).

        When ``include_smoke`` is False (default) rows whose session_id
        matches a known smoke/probe prefix or whose tokens_used is zero
        are dropped — those rows don't represent real sessions and skew
        the mean toward zero.
        """
        try:
            conn = self._scores_conn()
            # Pull a wider window when filtering so the post-filter list
            # still has up to ``limit`` real sessions. 4x is enough given
            # the observed smoke-row ratio in dogfood.
            db_limit = limit if include_smoke else limit * 4
            rows = conn.execute(
                "SELECT session_id, duration_s, tokens_used, files_read, "
                "files_modified, skills_invoked, timestamp "
                "FROM session_outcomes ORDER BY timestamp DESC LIMIT ?",
                (db_limit,),
            ).fetchall()
            conn.close()
        except Exception:
            return []
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            if not include_smoke and self._is_smoke_session(d):
                continue
            # Normalise keys to what sessions_page.py expects.
            d["id"] = d["session_id"]
            d["duration"] = d.get("duration_s")
            d["tokens"] = d.get("tokens_used")
            d["recorded_at"] = d.get("timestamp")
            # Honest per-work-unit metric. Tokens per session are
            # dominated by session scope; tokens per file edited
            # normalises by output and is a better proxy for retrieval
            # efficiency. None when files_modified is 0/missing so the
            # caller can show a dash instead of dividing.
            files_m = d.get("files_modified") or 0
            d["tokens_per_file"] = (
                int((d.get("tokens_used") or 0) / files_m)
                if files_m > 0 else None
            )
            out.append(d)
            if len(out) >= limit:
                break
        return out

    def get_skill_usage(self, session_id: Optional[str] = None) -> list[dict]:
        """Query skill_usage from scores.db."""
        try:
            conn = self._scores_conn()
            if session_id:
                rows = conn.execute(
                    "SELECT * FROM skill_usage WHERE session_id = ?",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM skill_usage").fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception:
            return []

    def exploration_rate(self) -> float:
        """Compute the current epsilon for exploration.

        Uses total outcome count to determine how much the system
        should explore vs exploit prompt variants.
        """
        try:
            conn = self._scores_conn()
            row = conn.execute(
                "SELECT COUNT(*) FROM prompt_scores"
            ).fetchone()
            conn.close()
            total = row[0] if row else 0
            return max(EPSILON_MIN, EPSILON_START * math.exp(-EPSILON_DECAY * total))
        except Exception:
            return EPSILON_START

    # ------------------------------------------------------------------
    # Conductor v2 visual surface (#79 follow-up):
    # SPA /conductor page reads these to render "what tasks is the
    # conductor driving and where are they in the SDLC?"
    # ------------------------------------------------------------------

    def managed_tasks(self) -> list[dict]:
        """List tasks where conductor is engaged.

        A task is "managed" when workflow_step is non-empty OR gate_state
        is not 'none'. Tasks worked raw (status flips only) are not
        included — they don't appear on the /conductor page.

        v6.1.3: filter out status=done. Conductor swimlanes had been
        accumulating every task that ever reached green_gate, polluting
        the active view (14 of 15 visible tiles were done shipped work).
        Done means done — it stays in the audit trail (workflow_step is
        not cleared) but doesn't show as currently-managed work.
        """
        if self._task_svc is None:
            return []
        try:
            tasks = self._task_svc.list()
        except Exception:
            return []
        out: list[dict] = []
        for t in tasks:
            step = getattr(t, "workflow_step", "") or ""
            gate = getattr(t, "gate_state", "none") or "none"
            status = getattr(t, "status", "") or ""
            if step == "" and gate == "none":
                continue
            if status == "done":
                continue
            out.append({
                "id": t.id,
                "title": t.title,
                "status": t.status,
                "workflow_step": step,
                "gate_state": gate,
                "gate_reason": getattr(t, "gate_reason", "") or "",
                # v6.0.43: extra fields for the /conductor tile redesign so
                # the SPA can render status/priority/age/owner/tags without
                # an N+1 fetch on each tile.
                "priority": getattr(t, "priority", 0) or 0,
                "assigned_agent": getattr(t, "assigned_agent", "") or "",
                "created_at": getattr(t, "created_at", "") or "",
                "updated_at": getattr(t, "updated_at", "") or "",
                "tags": list(getattr(t, "tags", []) or []),
                # Animated SDLC progress bar (a5e0d9f5): blended current-step
                # fill so the tile bar tweens between polls.
                "phase_progress": self.phase_progress(t.id),
            })
        return out

    def step_buckets(self) -> dict[str, int]:
        """Count of conductor-managed tasks per workflow_step.

        Used by the /conductor stepper to show "12 tasks at implement_tasks,
        3 at red_gate" at a glance. v6.1.3: status=done excluded — same
        rationale as managed_tasks (counters were inflated by historical
        shipped work).
        """
        if self._task_svc is None:
            return {}
        try:
            tasks = self._task_svc.list()
        except Exception:
            return {}
        from collections import Counter
        counter: Counter[str] = Counter()
        for t in tasks:
            step = getattr(t, "workflow_step", "") or ""
            status = getattr(t, "status", "") or ""
            if step and status != "done":
                counter[step] += 1
        return dict(counter)

    # ------------------------------------------------------------------
    # phase_progress — blended current-step fill for the SDLC bar
    # ------------------------------------------------------------------
    #   pct = min(0.95, in_step_s / typical_s)  (time baseline)
    #   OVERRIDDEN by children_done / children_total when child tasks exist.
    # Drives the animated current-segment fill on the conductor tiles +
    # TaskDetailPage header so the bar tweens between 5s polls instead of
    # snapping at each advance.
    _TYPICAL_S_FALLBACK = 900.0  # 15 min — positive default when no history

    @staticmethod
    def _parse_iso(ts: str) -> Optional[float]:
        """ISO-8601 timestamp -> epoch seconds; None if unparseable."""
        if not ts:
            return None
        try:
            from datetime import datetime
            return datetime.fromisoformat(ts).timestamp()
        except Exception:
            return None

    def _median_step_s(self) -> float:
        """Median gap between consecutive advance_task rows across all task
        history — the empirical 'typical' time a task dwells in one step.
        Falls back to a positive constant when there is no history yet."""
        if self._task_svc is None:
            return self._TYPICAL_S_FALLBACK
        try:
            tasks = self._task_svc.list()
        except Exception:
            return self._TYPICAL_S_FALLBACK
        gaps: list[float] = []
        for t in tasks:
            try:
                rows = self._task_svc.history(t.id)
            except Exception:
                continue
            advs = [self._parse_iso(getattr(r, "timestamp", "") or "")
                    for r in rows
                    if getattr(r, "action", "") == "advance_task"]
            advs = [a for a in advs if a is not None]
            for a, b in zip(advs, advs[1:]):
                if b > a:
                    gaps.append(b - a)
        if not gaps:
            return self._TYPICAL_S_FALLBACK
        gaps.sort()
        n = len(gaps)
        mid = n // 2
        med = gaps[mid] if n % 2 else (gaps[mid - 1] + gaps[mid]) / 2.0
        return med if med > 0 else self._TYPICAL_S_FALLBACK

    def _in_step_s(self, task_id: str) -> float:
        """Seconds since the most recent advance_task row for this task —
        how long it has dwelt in the current step. 0.0 when unknown."""
        if self._task_svc is None:
            return 0.0
        try:
            rows = self._task_svc.history(task_id)
        except Exception:
            return 0.0
        last: Optional[float] = None
        for r in rows:
            if getattr(r, "action", "") == "advance_task":
                ts = self._parse_iso(getattr(r, "timestamp", "") or "")
                if ts is not None:
                    last = ts
        if last is None:
            return 0.0
        from datetime import datetime, timezone
        elapsed = datetime.now(timezone.utc).timestamp() - last
        return elapsed if elapsed > 0 else 0.0

    def phase_progress(self, task_id: str) -> dict:
        """Blended estimate of how far through the CURRENT workflow step a
        task is. Shape:
          {pct, basis, in_step_s, typical_s,
           children_done, children_total, tokens_since_step}
        - baseline (basis='time'): min(0.95, in_step_s / typical_s) from the
          median step history; the 0.95 ceiling means it never reads 'done'
          before the actual advance.
        - override (basis='children'): when child tasks exist (parent_id ==
          task_id), pct is the exact children_done/children_total ratio.
        """
        typical_s = self._median_step_s()
        in_step_s = self._in_step_s(task_id)

        children_done = 0
        children_total = 0
        if self._task_svc is not None:
            try:
                for t in self._task_svc.list():
                    if (getattr(t, "parent_id", "") or "") == task_id:
                        children_total += 1
                        if (getattr(t, "status", "") or "") == "done":
                            children_done += 1
            except Exception:
                children_total = 0

        if children_total > 0:
            pct = children_done / children_total
            basis = "children"
        else:
            ratio = in_step_s / typical_s if typical_s > 0 else 0.0
            pct = min(0.95, ratio)
            basis = "time"

        # Real token effort: sum tokens across the sessions linked to this task
        # (the importer populates sessions.tokens_used). Per-message timestamps
        # aren't imported, so this is task-TOTAL spend, not a per-step slice —
        # but it's a real, growing number the bar surfaces as "N tok".
        tokens = 0
        if self._task_svc is not None:
            try:
                for s in self._task_svc.sessions_for_task(task_id):
                    used = s.get("tokens_used") if isinstance(s, dict) else getattr(s, "tokens_used", 0)
                    tokens += int(used or 0)
            except Exception:
                tokens = 0

        return {
            "pct": round(max(0.0, min(1.0, pct)), 6),
            "basis": basis,
            "in_step_s": round(in_step_s, 3),
            "typical_s": round(typical_s, 3),
            "children_done": children_done,
            "children_total": children_total,
            # Task-total linked-session tokens (see note above).
            "tokens_since_step": tokens,
        }
