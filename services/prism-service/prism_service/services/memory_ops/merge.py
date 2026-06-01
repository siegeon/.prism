"""MergeOperation — the first concrete MemoryOperation (Tier-2).

Folds a cluster of near-duplicate ``expertise`` memories into ONE canonical
higher-generation memory, archiving the members through the existing
``memory_service`` supersede path (history preserved, never deleted).

Pipeline (all three tiers, end-to-end):

  * ``select``        — Tier-1 activation prior (``fading_entries``) ranks the
    memory pool cheaply, then Brain similarity (``memory_svc.recall`` over
    ``domain='expertise'`` — the same hybrid surface ``brain_search`` uses)
    grows an N>=2 high-similarity cluster around each cold seed. Each cluster
    is enqueued as a ``consolidation_candidate`` whose ``scope_json`` carries
    the member ids, so the candidate id flows through the shared runner.
  * ``build_prompt``  — asks the model: same fact? synthesize ONE canonical
    memory, preserve any real distinctions.
  * ``apply_verdict`` — mints the synthesized memory at generation
    ``max(member generation) + 1`` and archives every member.

The verdict is logged to ``consolidation_runs`` (op_type='merge') by the
shared ``runner.run_one`` — no bespoke audit path here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from prism_service.services.memory_ops.base import MemoryOperation


# Cluster sizing: a merge needs at least 2 members; cap so one tick stays cheap.
_MIN_CLUSTER = 2
_MAX_CLUSTER = 5
# How many cold seeds the activation prior surfaces before clustering.
_SEED_LIMIT = 8
# fading_entries threshold: high enough that the prior ranks the whole live
# pool (coldest first) rather than only entries already below zero activation.
_FADE_THRESHOLD = 1e9


class MergeOperation(MemoryOperation):
    """Synthesize a cluster of duplicate/related memories into one canonical."""

    op_type = "merge"
    schema = {
        "same_fact": "bool",
        "narrative": "str",
        "confidence": "float",
        "new_memories": "list",
        "invalidate_memory_ids": "list",
    }
    #: Synthesis is a judgement call — default to the CLI default (Sonnet).
    model = ""
    provenance = {"source": "merge", "tier": 2}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ctx(project: str):
        from prism_service.project_context import get_project

        return get_project(project)

    @staticmethod
    def _scores_db(ctx) -> str:
        return str(ctx._data_dir / "scores.db")

    def _build_clusters(self, svc) -> list[list]:
        """Tier-1 prior (fading_entries) ranks the pool coldest-first; for each
        unclustered seed, Brain similarity (recall) grows an N>=2 cluster of
        high-similarity siblings. Returns lists of member ids (no overlap)."""
        seeds = svc.fading_entries(limit=_SEED_LIMIT, threshold=_FADE_THRESHOLD)
        clustered: set[str] = set()
        clusters: list[list] = []
        for seed in seeds:
            if seed.id in clustered:
                continue
            siblings = svc.recall(
                seed.name + " " + seed.description,
                domain=seed.domain or "expertise",
                limit=_MAX_CLUSTER,
            )
            ids: list[str] = []
            for e in [seed, *siblings]:
                if e.id in clustered or e.id in ids:
                    continue
                if e.status != "active" or e.invalid_at:
                    continue
                ids.append(e.id)
                if len(ids) >= _MAX_CLUSTER:
                    break
            if len(ids) >= _MIN_CLUSTER:
                clustered.update(ids)
                clusters.append(ids)
        return clusters

    # ------------------------------------------------------------------
    # MemoryOperation contract
    # ------------------------------------------------------------------

    def select(self, project: str) -> list:
        """Surface merge candidates: one ``consolidation_candidate`` per
        N>=2 near-duplicate cluster, its ``scope_json`` carrying the member
        ids. Returns the candidate ids (the items the shared runner drives)."""
        ctx = self._ctx(project)
        svc = ctx.memory_svc
        clusters = self._build_clusters(svc)
        if not clusters:
            return []

        from prism_service.services.janitor_service import JanitorService

        js = JanitorService(self._scores_db(ctx))
        items: list = []
        for member_ids in clusters:
            cid = js.enqueue(
                trigger="merge",
                scope={"op": "merge", "member_ids": member_ids},
            )
            items.append(cid)
        return items

    def _member_ids_for(self, ctx, item: Any) -> list:
        """Read the cluster member ids out of the candidate's scope_json."""
        import sqlite3

        conn = sqlite3.connect(self._scores_db(ctx))
        try:
            row = conn.execute(
                "SELECT scope_json FROM consolidation_candidates WHERE id=?",
                (item,),
            ).fetchone()
        finally:
            conn.close()
        if not row or not row[0]:
            return []
        try:
            return json.loads(row[0]).get("member_ids") or []
        except (json.JSONDecodeError, TypeError):
            return []

    def build_prompt(self, item: Any, project: str) -> str:
        """Ask the model to decide same-fact and synthesize ONE canonical
        memory from the cluster members, preserving real distinctions."""
        ctx = self._ctx(project)
        svc = ctx.memory_svc
        member_ids = self._member_ids_for(ctx, item)
        blocks = []
        for mid in member_ids:
            e = svc.get_entry(mid)
            if e is None:
                continue
            blocks.append(
                f"- id={e.id} gen={e.generation} name={e.name!r}\n"
                f"  {e.description}"
            )
        listing = "\n".join(blocks) or "(no members resolved)"
        return _PROMPT_TEMPLATE.format(
            count=len(blocks), members=listing,
            member_ids=json.dumps(member_ids),
        )

    def apply_verdict(self, item: Any, verdict: dict, project: str) -> None:
        """Mint the synthesized canonical memory at gen = max(member gen)+1,
        then archive (supersede) every member — history preserved."""
        ctx = self._ctx(project)
        svc = ctx.memory_svc

        # Members the verdict elected to fold (default: the whole cluster).
        member_ids = (
            verdict.get("invalidate_memory_ids")
            or self._member_ids_for(ctx, item)
        )
        members = [m for m in (svc.get_entry(i) for i in member_ids) if m]
        if not members:
            return
        # The model can decline: if it judged these NOT the same fact, leave
        # the cluster untouched (no merge, no supersede).
        if verdict.get("same_fact") is False:
            return

        max_gen = max((m.generation for m in members), default=1)
        spec = self._synth_spec(verdict, members)

        minted = svc.store(
            domain=spec["domain"], name=spec["name"],
            description=spec["description"], type=spec["type"],
            classification=spec["classification"],
            importance=spec["importance"], memory_type="semantic",
            evidence={"merged_from": [m.id for m in members],
                      "provenance": self.provenance},
        )
        # Force the canonical generation above the whole cluster (store() only
        # bumps past a single name/similarity match; a merge folds N members).
        if minted.generation <= max_gen:
            svc.update_entry(minted.id, generation=max_gen + 1)

        now = datetime.now(timezone.utc).isoformat()
        for m in members:
            if m.id == minted.id:
                continue
            svc.update_entry(m.id, status="archived", invalid_at=now)

    @staticmethod
    def _synth_spec(verdict: dict, members: list) -> dict:
        """Pull the synthesized memory out of the verdict, falling back to the
        highest-importance member when the model omits a field."""
        new = (verdict.get("new_memories") or [{}])[0] or {}
        anchor = max(members, key=lambda m: m.importance)
        return {
            "domain": new.get("domain") or anchor.domain or "expertise",
            "name": new.get("name") or anchor.name,
            "description": new.get("description") or anchor.description,
            "type": new.get("type") or anchor.type or "convention",
            "classification": new.get("classification")
            or anchor.classification,
            "importance": int(new.get("importance") or anchor.importance or 5),
        }


_PROMPT_TEMPLATE = """You are PRISM's memory-merge judge. Below are {count} \
expertise memories the activation prior + similarity search flagged as a \
near-duplicate cluster:

{members}

Decide: do these state the SAME underlying fact? If yes, synthesize ONE \
canonical memory that subsumes them — preserve every real distinction \
(don't drop a qualifier that only one member carries). If they are NOT the \
same fact, set "same_fact": false and synthesize nothing.

Return ONLY a JSON object (no prose, no markdown fence):
{{
  "same_fact": true | false,
  "narrative": "<one sentence: what you merged and why>",
  "confidence": <0.0-1.0>,
  "qualitative_score": <0.0-1.0>,
  "invalidate_memory_ids": {member_ids},
  "new_memories": [{{
    "name": "<canonical name>",
    "description": "<canonical description, distinctions preserved>",
    "domain": "expertise",
    "type": "convention",
    "classification": "<classification>",
    "importance": <1-10>
  }}]
}}"""
