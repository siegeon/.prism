"""_detect_duplicates() must cost ~0 on a settled store, and run_cycle()
must be able to skip it entirely for a project nobody is using right now
(task: livehang round 5).

Round 3 (7.13.333) made the per-pair comparison cheap (quick_ratio() +
matcher reuse + a length pre-filter): ~53.8s -> ~14s on the real live
memory store. Still a 14-second GIL-hogging tick every 5 minutes,
forever, even when NOTHING in the store has changed since the last run --
still a hang from a request's point of view (owner: "14s of GIL hogging
per maintenance tick is still a hang for the user"). This round adds:

(1) INCREMENTAL -- a persisted per-entry text hash
(GovernanceEngine._dup_hash_cache_path, next to this project's own
memory store). A domain where nothing changed since the last pass is
skipped ENTIRELY (zero ratio() calls, not merely fewer); a domain WITH
some real changes only compares the CHANGED entries against the rest.
(2) `run_cycle(scan_duplicates=False)` skips the scan altogether --
maintenance_clock._run_governance uses this for a project nobody is
using right now (project_activity.is_in_use), same shape as the drift
reindexer's active-project gate (round 4)."""

from __future__ import annotations

import random
from difflib import SequenceMatcher

from prism_service.services.governance import GovernanceEngine
from prism_service.services.memory_service import MemoryService

_WORDS = [
    "architecture", "decision", "conductor", "workflow", "pipeline",
    "gateway", "service", "database", "pattern", "strategy",
    "implementation", "function", "module", "component", "interface",
    "protocol", "session", "request", "response", "handler", "worker",
]


def _mem(tmp_path) -> MemoryService:
    return MemoryService(str(tmp_path / "mulch"))


def _gov(mem) -> GovernanceEngine:
    return GovernanceEngine(mem, None, None)


def _text(seed: int) -> str:
    return " ".join(random.Random(seed).choice(_WORDS) for _ in range(60))


def _count_real_ratio_calls(monkeypatch):
    calls = {"n": 0}
    real_ratio = SequenceMatcher.ratio

    def counting(self):
        calls["n"] += 1
        return real_ratio(self)

    monkeypatch.setattr(SequenceMatcher, "ratio", counting)
    return calls


def test_second_pass_with_no_changes_makes_zero_ratio_calls(tmp_path, monkeypatch):
    mem = _mem(tmp_path)
    domain = "architecture"
    for i in range(20):
        mem.store(domain, f"entry-{i}", _text(i), type="decision",
                  classification="tactical")
    gov = _gov(mem)

    # Force quick_ratio() to never filter anything (always "maybe a
    # duplicate") -- otherwise round 3's OWN quick_ratio pre-filter could
    # incidentally reach zero real ratio() calls on its own for
    # sufficiently-dissimilar random text, proving nothing about THIS
    # round's incremental hash-skip specifically. With quick_ratio()
    # forced open, the ONLY thing that can still make the second pass
    # cost zero is skipping the domain entirely because nothing changed.
    monkeypatch.setattr(SequenceMatcher, "quick_ratio", lambda self: 1.0)

    # First pass: a cold hash cache, so every entry is "changed" (never
    # seen before) -- this pass DOES compare, and persists the hashes.
    first_calls = _count_real_ratio_calls(monkeypatch)
    gov._detect_duplicates()
    assert first_calls["n"] > 0, (
        "sanity check failed: with quick_ratio() forced open, the first "
        "pass over a cold cache must call the real ratio() at least once "
        "(20 entries -> 190 pairs) -- got 0, so this test isn't actually "
        "exercising the comparison path at all")

    # Second pass: NOTHING in the store has changed. This must make ZERO
    # real ratio() calls -- not fewer, zero -- because every domain is
    # skipped outright once its entries' hashes all still match.
    second_calls = _count_real_ratio_calls(monkeypatch)
    gov._detect_duplicates()
    assert second_calls["n"] == 0, (
        f"a second duplicate scan over an UNCHANGED store called the real "
        f"SequenceMatcher.ratio() {second_calls['n']} times -- expected "
        f"EXACTLY zero. A settled store (no entry touched since the last "
        f"pass) must cost nothing: this is what turns a 14-second GIL-"
        f"hogging tick into an ongoing hang into a genuinely quiet one "
        f"once nothing is new."
    )


def test_a_changed_entry_only_compares_against_the_rest_not_all_pairs(
        tmp_path, monkeypatch):
    mem = _mem(tmp_path)
    domain = "architecture"
    for i in range(15):
        mem.store(domain, f"entry-{i}", _text(i), type="decision",
                  classification="tactical")
    gov = _gov(mem)
    gov._detect_duplicates()  # settle the baseline

    # Touch exactly ONE entry (a real edit -- a new description).
    entries = mem.list_entries(domain, status_filter="active")
    touched = entries[0]
    mem.update_entry(touched.id, description=_text(9999))

    # Force quick_ratio() open (see the sibling test above for why) --
    # otherwise it alone could hold the real ratio() count low regardless
    # of whether the incremental skip is doing anything.
    monkeypatch.setattr(SequenceMatcher, "quick_ratio", lambda self: 1.0)
    calls = _count_real_ratio_calls(monkeypatch)
    archived = gov._detect_duplicates()
    assert archived == 0, f"no real duplicate was introduced; got {archived}"
    # A full unchanged-vs-unchanged re-scan of 15 entries is 15*14/2 = 105
    # pairs; with quick_ratio() forced open, the OLD (pre-incremental) code
    # would call the real ratio() on ALL 105. Only pairs touching the ONE
    # changed entry (at most 14) may legitimately reach ratio() here.
    assert calls["n"] <= 14, (
        f"changing ONE entry re-compared it against the other 14 (at "
        f"most) -- got {calls['n']} real ratio() calls, which is within "
        f"that bound; a value at or above the full O(n^2) pair count "
        f"would mean the incremental skip isn't actually skipping "
        f"unchanged-vs-unchanged pairs")


class _StubBrainService:
    """Minimal stand-in so run_cycle()'s OTHER rules (not under test here)
    don't crash on a bare None -- _flag_stale_brain_docs calls
    self._brain.status() unconditionally, with no try/except of its own."""

    def status(self):
        return {"last_reindex": "2026-01-01T00:00:00", "doc_count": 0}


class _StubTaskService:
    """Ditto for _flag_stuck_tasks, which calls self._tasks.list(...)
    unconditionally."""

    def list(self, **kw):
        return []


def test_run_cycle_can_skip_the_duplicate_scan_entirely(tmp_path, monkeypatch):
    mem = _mem(tmp_path)
    domain = "architecture"
    for i in range(10):
        mem.store(domain, f"entry-{i}", _text(i), type="decision",
                  classification="tactical")
    gov = GovernanceEngine(mem, _StubTaskService(), _StubBrainService())

    calls = _count_real_ratio_calls(monkeypatch)
    report = gov.run_cycle(project="idle-project", scan_duplicates=False)
    assert calls["n"] == 0, (
        f"run_cycle(scan_duplicates=False) must skip the duplicate scan "
        f"entirely (maintenance_clock uses this for a project nobody is "
        f"using right now) -- got {calls['n']} real ratio() calls")
    assert report.last_governance_run  # the rest of the cycle still ran


def test_maintenance_clock_gates_duplicate_scan_to_projects_in_use(monkeypatch):
    from prism_service.services import maintenance_clock, project_activity

    calls = []

    class _FakeGovernance:
        def run_cycle(self, project="", scan_duplicates=True):
            calls.append((project, scan_duplicates))

    class _FakeCtx:
        governance = _FakeGovernance()

    monkeypatch.setattr(
        "prism_service.project_context.get_project", lambda p: _FakeCtx())
    monkeypatch.setattr(project_activity, "is_in_use", lambda p: p == "busy")

    maintenance_clock._run_governance("busy")
    maintenance_clock._run_governance("idle")

    assert ("busy", True) in calls, f"got {calls!r}"
    assert ("idle", False) in calls, (
        f"a project project_activity.is_in_use() says is NOT in use must "
        f"still get its cheap governance rules (run_cycle still runs) but "
        f"with scan_duplicates=False; got {calls!r}")
