"""GraphAnnotateService - the PULL inversion of graph_enrich.

graph_enrich PUSHES: PRISM shells ``claude -p`` for each scope and writes
{name, purpose} to graph_annotations. This service INVERTS that into a
JanitorService-style PULL loop: PRISM dispenses annotation BRIEFS and a
background Claude session does the inference, submitting {name, purpose}
back. The brief generation REUSES graph_enrich's scope enumeration
(hierarchy_scopes / community_scopes), render_prompt, and the _input_hash
escape-when-unchanged guard (a scope whose stored annotation already
matches the live input_hash is not enqueued). Submitted annotations are
schema-validated to {name, purpose} and persisted via
graph.upsert_annotation, exactly like the push path - provenance literal
``claude @ <date>`` so the narrative can't be mistaken for structure.

DURABLE (Option 1, #50): the queue lives in graph.db's ``graph_jobs``
table, NOT in process memory - a brief enqueued by one instance is
dispensable by any other instance over the same graph.db, surviving a
daemon bounce. (scope_kind, scope_id, input_hash) is UNIQUE so re-sweeps
are idempotent.

Mirrors JanitorService lifecycle: enqueue -> check (dispenses at most ONE
brief per call) -> submit (schema-validates) -> abandon (retry w/ backoff).
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

from prism_service.services import graph_enrich

_RESPONSE_SCHEMA: dict[str, str] = {
    "name": "string - short Title Case name, 2-4 words",
    "purpose": "string - one sentence, under 20 words",
}
_REQUIRED_FIELDS = ("name", "purpose")
_HARD_RETRY_LIMIT = 3


class GraphAnnotateService:
    """Durable queue + dispense-one + schema-validated submit + retry for
    the background-agent annotation PULL loop. Backed by graph.graph_jobs."""

    RESPONSE_SCHEMA = _RESPONSE_SCHEMA
    HARD_RETRY_LIMIT = _HARD_RETRY_LIMIT

    def __init__(self, graph) -> None:
        self._graph = graph
        # The scope payload is re-derivable from graph_enrich on demand,
        # but caching it per identity lets check() render the prompt and
        # carry input_hash without re-enumerating the whole project.
        self._scope_cache: dict[str, dict] = {}

    def enqueue(self, scope_kind: str, scopes: list[dict]) -> int:
        """Enqueue scopes of one kind whose stored input_hash no longer
        matches the live input_hash. Idempotent on (scope_kind, scope_id,
        input_hash) via the UNIQUE constraint. Returns count NEWLY queued."""
        n = 0
        for s in scopes:
            scope_id = s["scope_id"]
            input_hash = s.get("input_hash", "")
            existing = self._graph.get_annotation(scope_kind, scope_id, "name")
            if existing and existing.get("input_hash") == input_hash:
                continue  # escape - unchanged, zero work
            bid = uuid.uuid4().hex[:12]
            created = self._graph.insert_job(bid, scope_kind, scope_id,
                                             input_hash)
            self._scope_cache[self._key(scope_kind, scope_id, input_hash)] = s
            if created:
                n += 1
        return n

    def enqueue_project(self, project: str) -> int:
        """Project-level entry: enumerate BOTH kinds via graph_enrich and
        enqueue the changed scopes (reuses hierarchy_scopes /
        community_scopes - no re-derivation)."""
        total = 0
        total += self.enqueue("hierarchy", graph_enrich.hierarchy_scopes(project))
        total += self.enqueue("community", graph_enrich.community_scopes(project))
        return total

    @staticmethod
    def _key(scope_kind: str, scope_id: str, input_hash: str) -> str:
        return "\x1f".join((scope_kind, str(scope_id), input_hash or ""))

    def _scope_for(self, job: dict) -> dict:
        """Recover the live scope payload for a claimed job. Prefer the
        in-process cache; else reconstruct a minimal scope from the
        persisted row so a fresh instance can still render a prompt."""
        cached = self._scope_cache.get(
            self._key(job["scope_kind"], job["scope_id"], job["input_hash"]))
        if cached:
            return cached
        return {"scope_id": job["scope_id"],
                "level": 1 if job["scope_kind"] == "hierarchy" else None,
                "files": [job["scope_id"]], "symbols": [],
                "input_hash": job["input_hash"]}

    def check(self, session_id: str) -> dict:
        job = self._graph.claim_job(session_id)
        if job is None:
            return {"ready": False, "brief": None}
        scope = self._scope_for(job)
        brief = {
            "brief_id": job["brief_id"],
            "candidate_id": job["brief_id"],
            "scope_kind": job["scope_kind"],
            "scope_id": job["scope_id"],
            "prompt": graph_enrich.render_prompt(scope),
            "response_schema": dict(_RESPONSE_SCHEMA),
        }
        return {"ready": True, "brief": brief}

    def submit(self, brief_id: str, output: Optional[dict]) -> dict:
        job = self._graph.get_job(brief_id)
        if job is None:
            return {"accepted": False, "reason": "unknown brief"}
        out = output or {}
        for field in _REQUIRED_FIELDS:
            val = out.get(field)
            if not isinstance(val, str) or not val.strip():
                return {"accepted": False,
                        "reason": f"missing/invalid field: {field}"}
        prov = f"claude @ {time.strftime('%Y-%m-%d')}"
        ok = self._graph.upsert_annotation(
            job["scope_kind"], job["scope_id"], "name",
            out["name"].strip(), out["purpose"].strip(),
            job.get("input_hash", ""), prov)
        if not ok:
            return {"accepted": False, "reason": "persist failed"}
        self._graph.mark_job(brief_id, "completed")
        return {"accepted": True}

    def abandon(self, brief_id: str, reason: str = "") -> dict:
        job = self._graph.get_job(brief_id)
        if job is None:
            return {"accepted": False, "reason": "unknown brief"}
        res = self._graph.retry_job(brief_id, _HARD_RETRY_LIMIT)
        return {"accepted": True, "status": res["status"],
                "retries": res["retries"]}

    def status(self) -> dict:
        counts = self._graph.job_status_counts()
        return {
            "pending": counts.get("pending", 0),
            "dispensed": counts.get("dispensed", 0),
            "completed": counts.get("completed", 0),
            "abandoned": counts.get("abandoned", 0),
        }
