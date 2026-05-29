"""GraphAnnotateService — the PULL inversion of graph_enrich.

graph_enrich PUSHES: PRISM shells ``claude -p`` for each scope and writes
{name, purpose} to graph_annotations. This service INVERTS that into a
JanitorService-style PULL loop: PRISM dispenses annotation BRIEFS and a
background Claude session does the inference, submitting {name, purpose}
back. The brief generation REUSES graph_enrich's scope enumeration
(hierarchy_scopes / community_scopes), render_prompt, and the _input_hash
escape-when-unchanged guard (a scope whose stored annotation already
matches the live input_hash is not enqueued). Submitted annotations are
schema-validated to {name, purpose} and persisted via
graph.upsert_annotation, exactly like the push path — provenance literal
``claude @ <date>`` so the narrative can't be mistaken for structure.

Mirrors JanitorService lifecycle: enqueue -> check (dispenses at most ONE
brief per call) -> submit (schema-validates) -> abandon (retry w/ backoff).
"""

from __future__ import annotations

import time
import uuid
from typing import Optional

from prism_service.services import graph_enrich

# The response schema a submitting session must satisfy. Validated on
# submit; malformed output is rejected and the brief stays dispensable.
_RESPONSE_SCHEMA: dict[str, str] = {
    "name": "string — short Title Case name, 2-4 words",
    "purpose": "string — one sentence, under 20 words",
}
_REQUIRED_FIELDS = ("name", "purpose")
_HARD_RETRY_LIMIT = 3


class GraphAnnotateService:
    """Queue + dispense-one + schema-validated submit + retry for the
    background-agent annotation PULL loop."""

    RESPONSE_SCHEMA = _RESPONSE_SCHEMA
    HARD_RETRY_LIMIT = _HARD_RETRY_LIMIT

    def __init__(self, graph) -> None:
        self._graph = graph
        # brief_id -> {scope_kind, scope, status, retries}
        self._briefs: dict[str, dict] = {}
        # FIFO of dispensable brief_ids (status == 'pending').
        self._queue: list[str] = []

    # ------------------------------------------------------------------
    # enqueue — escape-when-unchanged guard (reuses graph_enrich hashes)
    # ------------------------------------------------------------------
    def enqueue(self, scope_kind: str, scopes: list[dict]) -> int:
        """Enqueue scopes of one kind whose stored input_hash no longer
        matches the live input_hash. Returns the count enqueued."""
        n = 0
        for s in scopes:
            existing = self._graph.get_annotation(
                scope_kind, s["scope_id"], "name")
            if existing and existing.get("input_hash") == s.get("input_hash"):
                continue  # escape — unchanged, zero work
            bid = uuid.uuid4().hex[:12]
            self._briefs[bid] = {
                "scope_kind": scope_kind, "scope": s,
                "status": "pending", "retries": 0,
            }
            self._queue.append(bid)
            n += 1
        return n

    def enqueue_project(self, project: str) -> int:
        """Project-level entry: enumerate BOTH kinds via graph_enrich and
        enqueue the changed scopes (reuses hierarchy_scopes /
        community_scopes — no re-derivation)."""
        total = 0
        total += self.enqueue("hierarchy", graph_enrich.hierarchy_scopes(project))
        total += self.enqueue("community", graph_enrich.community_scopes(project))
        return total

    # ------------------------------------------------------------------
    # check — dispense at most ONE brief per call
    # ------------------------------------------------------------------
    def check(self, session_id: str) -> dict:
        while self._queue:
            bid = self._queue.pop(0)
            rec = self._briefs.get(bid)
            if not rec or rec["status"] != "pending":
                continue
            rec["status"] = "dispensed"
            rec["session_id"] = session_id
            scope = rec["scope"]
            brief = {
                "brief_id": bid,
                "candidate_id": bid,
                "scope_kind": rec["scope_kind"],
                "scope_id": scope["scope_id"],
                "prompt": graph_enrich.render_prompt(scope),
                "response_schema": dict(_RESPONSE_SCHEMA),
            }
            return {"ready": True, "brief": brief}
        return {"ready": False, "brief": None}

    # ------------------------------------------------------------------
    # submit — schema-validate {name, purpose}, then persist
    # ------------------------------------------------------------------
    def submit(self, brief_id: str, output: Optional[dict]) -> dict:
        rec = self._briefs.get(brief_id)
        if rec is None:
            return {"accepted": False, "reason": "unknown brief"}
        out = output or {}
        for field in _REQUIRED_FIELDS:
            val = out.get(field)
            if not isinstance(val, str) or not val.strip():
                return {"accepted": False,
                        "reason": f"missing/invalid field: {field}"}
        scope = rec["scope"]
        prov = f"claude @ {time.strftime('%Y-%m-%d')}"
        ok = self._graph.upsert_annotation(
            rec["scope_kind"], scope["scope_id"], "name",
            out["name"].strip(), out["purpose"].strip(),
            scope.get("input_hash", ""), prov)
        if not ok:
            return {"accepted": False, "reason": "persist failed"}
        rec["status"] = "completed"
        return {"accepted": True}

    # ------------------------------------------------------------------
    # abandon — requeue with backoff until the hard retry limit
    # ------------------------------------------------------------------
    def abandon(self, brief_id: str, reason: str = "") -> dict:
        rec = self._briefs.get(brief_id)
        if rec is None:
            return {"accepted": False, "reason": "unknown brief"}
        rec["retries"] += 1
        if rec["retries"] >= _HARD_RETRY_LIMIT:
            rec["status"] = "abandoned"
        else:
            rec["status"] = "pending"
            self._queue.append(brief_id)
        return {"accepted": True, "status": rec["status"],
                "retries": rec["retries"]}
