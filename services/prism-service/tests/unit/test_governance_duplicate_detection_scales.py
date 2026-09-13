"""_detect_duplicates() must not lock up the whole daemon at real-world
scale -- the actual root cause of the /live-page hang misdiagnosed at first
as a transcript-I/O problem in api/work.py (task: livehang, round 3).

Timeline: rounds 1-2 bounded and deduped work_graph's own transcript
lookups (genuinely correct fixes, proven by their own tests). Round-2's
fix still deployed, GET /api/work/graph kept timing out on the live
instance. An in-process call to the identical work_graph() against the
identical real data completed in ~2.7s -- so the ROUTE'S OWN LOGIC was
never the remaining problem. The daemon's own watchdog (a plain GET /
self-probe with nothing to do with any specific route) had logged 24
stack-dump timeouts, which only makes sense if something was starving
the GIL for the WHOLE process periodically.

A SIGUSR1 stack dump mid-hang (services/main.py's preserved #38 dump
handler) caught it: prism-maintenance-clock's own thread was inside
governance.py's _detect_duplicates(), in difflib's SequenceMatcher
machinery -- NOT anywhere near work.py. Direct timing against the real
memory store (43 domains, 436 active entries, largest domain
"architecture" at 132) measured run_cycle() at 55.6s total, with
_detect_duplicates() alone responsible for 53.8s of it -- a naive O(n^2)
all-pairs SequenceMatcher.ratio() scan per domain, and ratio() is
expensive per call for real, paragraph-length memory text. Every other
governance sub-rule finished in under a second.

Because this runs on a periodic background thread (maintenance_clock)
that holds the GIL almost continuously while it computes, it starves
EVERY other thread in the process -- including whichever anyio worker
thread is trying to serve an HTTP request, any request, not just
/api/work/graph. This is why fixing api/work.py alone could never make
the live symptom go away: the actual blocker was never in that file.

Fix: `SequenceMatcher.quick_ratio()` is a stdlib-guaranteed UPPER BOUND on
`.ratio()` (cheap: a single pass building character-frequency multisets,
no real matching-block search) -- when quick_ratio() < DUPLICATE_THRESHOLD,
ratio() can never reach the threshold either, so the expensive comparison
is skipped with NO change in which pairs are flagged as duplicates. Also
reuses one SequenceMatcher instance per outer entry (`set_seq2` once,
`set_seq1` per inner entry) per difflib's own documented pattern for
comparing one sequence against many, instead of constructing a fresh
matcher (rebuilding the b2j index from scratch) on every single pair.

This test pins the MECHANISM directly (a real .ratio() call count,
deterministic and hardware-independent) rather than a wall-clock budget:
synthetic text that reliably reproduces difflib's true cost at scale
turned out to be its own rabbit hole (autojunk degenerates on
low-character-diversity text; a shared vocabulary defeats quick_ratio()
as a discriminator) -- the call-count assertion is what actually proves
the fix skips the expensive path instead of chasing a timing number that
would vary with the host and the corpus. A real-data re-measurement
(governance.run_cycle() against the live memory store, 43 domains / 436
entries) is reported in the version notes instead."""

from __future__ import annotations

import random
import string
from difflib import SequenceMatcher

from prism_service.models.memory import ExpertiseEntry
from prism_service.services.governance import GovernanceEngine
from prism_service.services.memory_service import MemoryService


def _mem(tmp_path) -> MemoryService:
    return MemoryService(str(tmp_path / "mulch"))


def _gov(mem) -> GovernanceEngine:
    return GovernanceEngine(mem, None, None)


def test_duplicate_scan_skips_the_expensive_ratio_call_for_dissimilar_pairs(
        tmp_path, monkeypatch):
    mem = _mem(tmp_path)
    domain = "architecture"

    # 26 entries whose descriptions share NO characters with each other at
    # all (each one repeats a single, distinct letter of the alphabet) --
    # the most extreme "obviously not a duplicate" case, so quick_ratio()
    # must skip every cross pair among them. Real memory text is less
    # extreme than this but the mechanism being pinned -- quick_ratio() as
    # a guaranteed upper bound letting ratio() be skipped -- is exactly the
    # same one that cut a real 53.8s scan down on the live instance.
    letters = string.ascii_lowercase  # 26 distinct letters, no repeats --
    # each noise entry gets its OWN letter so none of them collide with
    # each other either; wrapping (i % 26) would give two entries the
    # exact same repeated-letter text and get THEM flagged as duplicates
    # too, which is not what this test is pinning.
    entries = [
        ExpertiseEntry(
            id=f"mx-noise{i:04d}", type="decision", name=f"decision-{i}",
            description=(letters[i] * 400), classification="tactical",
            recorded_at="2026-01-01T00:00:00+00:00", domain=domain,
            status="active",
        )
        for i in range(len(letters))
    ]
    # One genuine near-duplicate pair seeded alongside the noise: identical
    # content, trivial rewording -- must still be caught after the fix.
    # Written directly via _write_entries (bypassing MemoryService.store's
    # OWN write-time near-duplicate dedup, a separate mechanism from
    # governance's periodic sweep) so both can co-exist as active entries
    # for _detect_duplicates() to find.
    # A short REPEATED phrase (or single-character runs) triggers difflib's
    # autojunk heuristic even at moderate length: any element occurring in
    # more than 1% of a sequence over 200 characters is treated as "popular"
    # and excluded as a match anchor, and a short phrase repeated enough
    # times to reach paragraph length has EVERY one of its words cross that
    # bar -- verified directly: it collapses ratio() to ~0.004 between two
    # texts differing only by a trailing space, instead of the ~0.99 they
    # actually deserve. Varied real words drawn from a reasonably large
    # pool avoids this (verified: ratio()=0.988 for the equivalent
    # trivial-rewording pair below).
    _words = [
        "architecture", "decision", "conductor", "workflow", "pipeline",
        "gateway", "service", "database", "pattern", "strategy",
        "implementation", "function", "module", "component", "interface",
        "protocol", "session", "request", "response", "handler", "worker",
        "process", "thread", "schema", "migration", "cache", "budget",
        "deadline", "receipt", "ledger", "history", "audit", "signal",
        "trigger", "listener", "observer", "factory", "adapter", "bridge",
    ]
    dup_text = " ".join(
        random.Random(9999).choice(_words) for _ in range(80)) + "."
    entries.append(ExpertiseEntry(
        id="mx-duporig", type="decision", name="dup-original",
        description=dup_text, classification="tactical",
        recorded_at="2026-01-01T00:00:00+00:00", domain=domain,
        status="active",
    ))
    entries.append(ExpertiseEntry(
        id="mx-duprest", type="decision", name="dup-restated",
        description=dup_text + " ", classification="tactical",
        recorded_at="2026-01-02T00:00:00+00:00", domain=domain,
        status="active",
    ))
    mem._write_entries(domain, entries)

    calls = {"n": 0}
    real_ratio = SequenceMatcher.ratio

    def counting_ratio(self):
        calls["n"] += 1
        return real_ratio(self)

    monkeypatch.setattr(SequenceMatcher, "ratio", counting_ratio)

    gov = _gov(mem)
    archived = gov._detect_duplicates()

    n = len(letters) + 2  # noise entries + the seeded duplicate pair
    total_pairs = (n * (n - 1)) // 2
    assert calls["n"] < total_pairs, (
        f"the real (expensive) SequenceMatcher.ratio() was called "
        f"{calls['n']} times out of {total_pairs} total pairs -- expected "
        f"FEWER than the full pair count: quick_ratio() (a cheap, "
        f"stdlib-guaranteed upper bound on ratio()) must skip the "
        f"expensive call for pairs it can already tell apart, which is "
        f"what turned a real 53.8s scan on the live instance into "
        f"something that no longer starves the GIL for the whole process. "
        f"An unbounded call count here means every pair still pays the "
        f"expensive path regardless of how obviously dissimilar they are."
    )
    assert archived == 1, (
        f"the seeded near-duplicate pair (dup-original/dup-restated) must "
        f"still be detected and archived -- the speedup must not change "
        f"WHICH pairs are flagged, only how fast the scan runs; got "
        f"archived={archived!r}"
    )
    statuses = {e.name: e.status for e in mem.list_entries(domain, status_filter=None)}
    assert statuses["dup-restated"] == "archived", (
        f"the NEWER entry of the duplicate pair must be the one archived "
        f"(unchanged behavior from before the speedup); got {statuses!r}")
    assert statuses["dup-original"] == "active", f"got {statuses!r}"
