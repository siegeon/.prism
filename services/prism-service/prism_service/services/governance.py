"""Governance engine — deterministic rules enforced on a timer."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from prism_service.config import (
    DOMAIN_BUDGET_CAP,
    DOMAIN_SHELF_LIFE,
    DUPLICATE_THRESHOLD,
    TASK_STALE_HOURS,
    USAGE_ARCHIVE_DAYS,
    USAGE_DECAY_DAYS,
)
from prism_service.models.memory import HealthReport
from prism_service.services import system_activity

# Per-slice wall-clock budget for _detect_duplicates' comparison loop (task:
# livehang round 5) -- a route sharing the GIL with this worker never waits
# longer than one slice: the loop checks elapsed time against this and
# yields (time.sleep(0)) before continuing, so a slow scan degrades into
# many short slices instead of one long, request-starving one.
_DUP_SCAN_SLICE_S = 0.05


class GovernanceEngine:
    """Runs deterministic governance rules over expertise, tasks, and brain.

    Designed to be invoked periodically (e.g. every 5 minutes) and caches
    the most recent HealthReport for fast serving.
    """

    def __init__(
        self,
        memory_service: object,
        task_service: object,
        brain_service: object,
    ) -> None:
        from prism_service.services.memory_service import MemoryService
        from prism_service.services.task_service import TaskService
        from prism_service.services.brain_service import BrainService

        self._memory: MemoryService = memory_service  # type: ignore[assignment]
        self._tasks: TaskService = task_service  # type: ignore[assignment]
        self._brain: BrainService = brain_service  # type: ignore[assignment]
        self._cached_report: Optional[HealthReport] = None

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    def run_cycle(self, project: str = "", scan_duplicates: bool = True) -> HealthReport:
        """Execute all governance rules and return a health report.

        `project` (task: livehang round 5) names this project for the
        system_activity feed the duplicate scan records itself under --
        cosmetic when omitted (the scan just reports under an empty
        project label). `scan_duplicates=False` skips ONLY the duplicate
        scan (by far the most expensive rule here -- everything else in
        this method finishes in well under a second even on the real
        live memory store): the caller (maintenance_clock._run_governance)
        passes False for a project nobody is currently using, per the
        owner directive that background work should track actual use,
        not sweep every tracked project on a timer regardless."""
        report = HealthReport()
        report.last_governance_run = datetime.now(timezone.utc).isoformat()

        report.archived_this_cycle += self._enforce_ttl()
        report.archived_this_cycle += self._enforce_budget_caps()
        if scan_duplicates:
            report.archived_this_cycle += self._detect_duplicates(project=project)
        report.archived_this_cycle += self._decay_unused()
        report.stale_brain_docs = self._flag_stale_brain_docs()
        report.flagged_conflicts = self._detect_conflicts()
        report.stuck_tasks = self._flag_stuck_tasks()
        report.domains_near_cap = self._domains_near_cap()

        # Learning loop rules
        report.ineffective_flagged = self._decay_ineffective()
        report.effective_boosted = self._boost_effective()

        self._cached_report = report
        return report

    def get_health_report(self) -> HealthReport:
        """Return the most recently cached health report."""
        if self._cached_report is None:
            return HealthReport()
        return self._cached_report

    # ------------------------------------------------------------------
    # Rule: TTL enforcement
    # ------------------------------------------------------------------

    def _enforce_ttl(self) -> int:
        """Archive entries that have exceeded their domain shelf life."""
        archived = 0
        now = datetime.now(timezone.utc)

        for domain in self._memory.list_domains():
            shelf_days = DOMAIN_SHELF_LIFE.get(domain, DOMAIN_SHELF_LIFE["default"])
            cutoff = now - timedelta(days=shelf_days)

            entries = self._memory.list_entries(domain, status_filter="active")
            for entry in entries:
                if not entry.recorded_at:
                    continue
                try:
                    recorded = datetime.fromisoformat(entry.recorded_at)
                    if recorded.tzinfo is None:
                        recorded = recorded.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                if recorded < cutoff:
                    self._memory.update_entry(entry.id, status="archived")
                    archived += 1

        return archived

    # ------------------------------------------------------------------
    # Rule: budget caps
    # ------------------------------------------------------------------

    def _enforce_budget_caps(self) -> int:
        """Archive oldest entries when a domain exceeds the budget cap."""
        archived = 0

        for domain in self._memory.list_domains():
            entries = self._memory.list_entries(domain, status_filter="active")
            if len(entries) <= DOMAIN_BUDGET_CAP:
                continue

            # Sort by recorded_at ascending (oldest first)
            entries.sort(key=lambda e: e.recorded_at or "")
            excess = len(entries) - DOMAIN_BUDGET_CAP
            for entry in entries[:excess]:
                self._memory.update_entry(entry.id, status="archived")
                archived += 1

        return archived

    # ------------------------------------------------------------------
    # Rule: duplicate detection
    # ------------------------------------------------------------------

    def _dup_hash_cache_path(self) -> Path:
        """Where per-entry text hashes persist across runs (task: livehang
        round 5) -- a sibling of the memory store's own "expertise" dir,
        never inside it (so it's never mistaken for a real entry file)."""
        return Path(self._memory._dir).parent / "governance_dup_hashes.json"

    def _load_dup_hashes(self) -> dict[str, str]:
        try:
            return json.loads(self._dup_hash_cache_path().read_text())
        except Exception:
            return {}

    def _save_dup_hashes(self, hashes: dict[str, str]) -> None:
        try:
            self._dup_hash_cache_path().write_text(json.dumps(hashes))
        except Exception:
            pass

    def _detect_duplicates(self, project: str = "") -> int:
        """Auto-merge near-duplicate entries within each domain.

        Uses SequenceMatcher ratio on name+description. When a duplicate
        pair is found, the newer entry is archived.

        PERFORMANCE (task: live-page transcript-I/O hang, rounds 3 and 5):
        this runs periodically on maintenance_clock's background thread,
        which holds the GIL almost continuously while it computes and so
        starves EVERY other thread in the process -- any HTTP route, not
        just one. Measured live at 53.8s for one run_cycle() (43 domains /
        436 real entries, largest domain 132) -- a naive O(n^2) all-pairs
        scan where each pair built a FRESH SequenceMatcher and called the
        expensive real .ratio(). Round 3 (quick_ratio() pre-filter + one
        reused matcher + a plain length check, all provably
        behavior-preserving upper bounds on ratio()) cut that to ~14s --
        real, but still a 14-second GIL-hogging tick, still a hang from a
        request's point of view. Round 5 adds the two changes that
        actually fix that:

        INCREMENTAL (persisted per-entry hash): every entry's compare-text
        hash is persisted (_dup_hash_cache_path, a small JSON file next to
        this project's own memory store, never shared across projects).
        A domain where NO entry's hash has changed since the last run is
        skipped ENTIRELY -- zero length checks, zero quick_ratio() calls,
        zero real ratio() calls, not merely fewer of them. A domain WITH
        changes still only compares CHANGED entries against the rest (an
        unchanged-vs-unchanged pair was already resolved in a prior run
        and neither side has moved since, so re-comparing it can only
        repeat the same answer at the same cost). This is what makes a
        settled store cost ~0 on every tick after the first, instead of
        paying the full O(n^2) scan every 5 minutes forever.

        CHUNKED: the comparison loop checks elapsed wall-clock time every
        iteration and, once a slice (_DUP_SCAN_SLICE_S, default 50ms) is
        spent, calls time.sleep(0) to actually yield the GIL before
        continuing -- so even a large first-ever scan (or a domain with
        genuinely many real changes) never holds the GIL for one long
        continuous stretch; a request queued behind it gets a turn every
        slice instead of waiting for the whole scan to finish.

        Recorded via system_activity.pass_("governance", project,
        "detect_duplicates") so a scan in progress is visible on the Live
        page, same as the other standing workers.
        """
        stored_hashes = self._load_dup_hashes()
        new_hashes = dict(stored_hashes)
        archived = 0
        matcher = SequenceMatcher()
        slice_start = time.monotonic()

        with system_activity.pass_("governance", project, "detect_duplicates"):
            for domain in self._memory.list_domains():
                entries = self._memory.list_entries(domain, status_filter="active")

                texts: dict[str, str] = {}
                changed_ids: set[str] = set()
                for e in entries:
                    text = f"{e.name} {e.description}"
                    texts[e.id] = text
                    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
                    key = f"{domain}:{e.id}"
                    if stored_hashes.get(key) != h:
                        changed_ids.add(e.id)
                    new_hashes[key] = h
                # Prune hashes for entries that no longer exist/are no
                # longer active in this domain (archived/deleted since the
                # last run) -- otherwise the cache grows forever and a
                # reused id could false-match a stale hash.
                live_keys = {f"{domain}:{e.id}" for e in entries}
                for stale_key in [k for k in new_hashes
                                  if k.startswith(f"{domain}:") and k not in live_keys]:
                    new_hashes.pop(stale_key, None)

                if not changed_ids:
                    # Nothing in this domain has changed since the last
                    # pass -- every pairing here was already resolved then
                    # and neither side has moved, so there is NOTHING to
                    # (re)compare. Zero ratio() calls, zero quick_ratio()
                    # calls, zero length checks.
                    continue

                archived_ids: set[str] = set()
                for i in range(len(entries)):
                    if entries[i].id in archived_ids:
                        continue
                    text_i = texts[entries[i].id]
                    len_i = len(text_i)
                    matcher.set_seq2(text_i)
                    for j in range(i + 1, len(entries)):
                        if entries[j].id in archived_ids:
                            continue
                        if (entries[i].id not in changed_ids
                                and entries[j].id not in changed_ids):
                            # Neither side changed -- already resolved.
                            continue

                        if time.monotonic() - slice_start >= _DUP_SCAN_SLICE_S:
                            time.sleep(0)  # actually yield the GIL
                            slice_start = time.monotonic()

                        text_j = texts[entries[j].id]
                        len_j = len(text_j)
                        if (2 * min(len_i, len_j)) / (len_i + len_j) < DUPLICATE_THRESHOLD:
                            continue
                        matcher.set_seq1(text_j)
                        if matcher.quick_ratio() < DUPLICATE_THRESHOLD:
                            continue
                        if matcher.ratio() >= DUPLICATE_THRESHOLD:
                            # Archive the newer entry
                            self._memory.update_entry(entries[j].id, status="archived")
                            archived_ids.add(entries[j].id)
                            archived += 1

        self._save_dup_hashes(new_hashes)
        return archived

    # ------------------------------------------------------------------
    # Rule: usage decay
    # ------------------------------------------------------------------

    def _decay_unused(self) -> int:
        """Archive entries that haven't been recalled within the decay window."""
        archived = 0
        now = datetime.now(timezone.utc)
        archive_cutoff = now - timedelta(days=USAGE_ARCHIVE_DAYS)

        for domain in self._memory.list_domains():
            entries = self._memory.list_entries(domain, status_filter="active")
            for entry in entries:
                if entry.recall_count > 0 and entry.last_recalled:
                    try:
                        last = datetime.fromisoformat(entry.last_recalled)
                        if last.tzinfo is None:
                            last = last.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        continue
                    if last < archive_cutoff:
                        self._memory.update_entry(entry.id, status="archived")
                        archived += 1
                elif entry.recall_count == 0 and entry.recorded_at:
                    # Never recalled — check if old enough to archive
                    try:
                        recorded = datetime.fromisoformat(entry.recorded_at)
                        if recorded.tzinfo is None:
                            recorded = recorded.replace(tzinfo=timezone.utc)
                    except (ValueError, TypeError):
                        continue
                    if recorded < archive_cutoff:
                        self._memory.update_entry(entry.id, status="archived")
                        archived += 1

        return archived

    # ------------------------------------------------------------------
    # Rule: stale brain docs
    # ------------------------------------------------------------------

    def _flag_stale_brain_docs(self) -> int:
        """Count brain documents that may be stale.

        Uses the brain service status to get a rough count. A more
        sophisticated implementation would compare indexed_at to file
        mtime, but we keep it simple here.
        """
        status = self._brain.status()
        # A rough heuristic: if the brain has docs but hasn't reindexed
        # recently, flag them all
        if not status.get("last_reindex"):
            return status.get("doc_count", 0)
        return 0

    # ------------------------------------------------------------------
    # Rule: conflict detection
    # ------------------------------------------------------------------

    def _detect_conflicts(self) -> int:
        """Disabled — keyword overlap cannot detect semantic contradiction.

        v6.3.38 — the keyword heuristic (exactly one entry carries a negation
        word + shared tokens) produced ONLY false positives at scale. On
        PRISM's own store it flagged ~138 pairs per run (1499 candidate pairs
        over 206 active entries): dense technical memories about the same
        subsystem trivially share >=4 substantive terms, and 'never/avoid/not'
        is common in legitimate guidance ('never commit to main'). Worse, it
        auto-mutated status, burying ~390 valid memories in needs_review.
        Genuine contradiction handling already lives in same-name supersession
        (MemoryService.store) and the LLM-judged verify_staleness memory-op,
        so this rule now no-ops. Reintroduce only with an embedding/NLI-based
        detector that actually models meaning, not token overlap.
        """
        return 0

    # ------------------------------------------------------------------
    # Rule: stuck tasks
    # ------------------------------------------------------------------

    def _flag_stuck_tasks(self) -> int:
        """Flag tasks that have been in_progress beyond the stale threshold."""
        stuck = 0
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=TASK_STALE_HOURS)

        in_progress = self._tasks.list(status="in_progress")
        for task in in_progress:
            check_time = task.updated_at or task.created_at
            if not check_time:
                continue
            try:
                ts = datetime.fromisoformat(check_time)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if ts < cutoff:
                stuck += 1

        return stuck

    # ------------------------------------------------------------------
    # Rule: decay ineffective memories (learning loop)
    # ------------------------------------------------------------------

    def _decay_ineffective(self) -> int:
        """Flag or archive entries that correlate with task failures.

        Entries with effectiveness < -0.3 (recalled 3+ times, mostly during
        failed tasks) are flagged needs_review. Below -0.6, archived outright.
        More surgical than time-based decay — targets entries that actively hurt.
        """
        flagged = 0
        try:
            scores = self._memory.get_effectiveness_scores()
        except Exception:
            return 0

        for entry_id, data in scores.items():
            if data["total"] < 3:
                continue  # not enough signal yet
            score = data["score"]
            entry = self._memory.get_entry(entry_id)
            if entry is None or entry.status != "active":
                continue

            if score <= -0.6:
                self._memory.update_entry(entry_id, status="archived")
                flagged += 1
            elif score <= -0.3:
                self._memory.update_entry(entry_id, status="needs_review")
                flagged += 1

        return flagged

    # ------------------------------------------------------------------
    # Rule: boost effective memories (learning loop)
    # ------------------------------------------------------------------

    def _boost_effective(self) -> int:
        """Boost importance of entries that reliably correlate with success.

        Entries with effectiveness > 0.5 get importance bumped up (capped at 10).
        Proven-useful entries surface more prominently in future recalls.
        """
        boosted = 0
        try:
            scores = self._memory.get_effectiveness_scores()
        except Exception:
            return 0

        for entry_id, data in scores.items():
            if data["total"] < 3:
                continue
            score = data["score"]
            if score <= 0.5:
                continue

            entry = self._memory.get_entry(entry_id)
            if entry is None or entry.status != "active":
                continue

            # Bump importance: +1 for >0.5, +2 for >0.8, capped at 10
            bump = 2 if score > 0.8 else 1
            new_importance = min(10, entry.importance + bump)
            if new_importance != entry.importance:
                self._memory.update_entry(entry_id, importance=new_importance)
                boosted += 1

        return boosted

    # ------------------------------------------------------------------
    # Helper: domains near cap
    # ------------------------------------------------------------------

    def _domains_near_cap(self) -> list[str]:
        """Return domains at 80% or more of budget cap."""
        near: list[str] = []
        threshold = int(DOMAIN_BUDGET_CAP * 0.8)
        for domain in self._memory.list_domains():
            entries = self._memory.list_entries(domain, status_filter="active")
            if len(entries) >= threshold:
                near.append(domain)
        return near
