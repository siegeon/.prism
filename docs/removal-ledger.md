# Removal ledger

Task 292e8ea2 "Retire the code and Documents we no longer use". Delete-only
sweep. Each row names a candidate the ticket called out, the decision, and
the evidence behind it. `test_retired_surfaces_stay_retired.py` reads this
file and enforces that every REMOVED name has no remaining tracked
reference, and that every ticket candidate is classified.

| Decision | Path | Evidence |
| --- | --- | --- |
| REMOVED | `services/prism-service/docker-compose.v51.yml` | Version-pinned duplicate of `docker-compose.yml` for a "v5.2.0" preview (current version is 7.13.113). `git grep -l docker-compose.v51` finds only this file and the guard test — no compose profile, script, or doc invokes it. Superseded entirely by `docker-compose.yml`, which already carries the host-claude-auth bind mount and env vars this file predates. |
| KEEP | `services/prism-service/desktop/tauri-shell` | Tracked, 33 files (Cargo.lock, icons, src-tauri/, src/, package.json). `.github/workflows/release-tauri.yml` still builds from this tree, and `installer/pyinstaller.spec`, `auto_updater.py`, `graph_service.py`, `main.py`, `SettingsPage.tsx` reference Tauri as a live concept. CLAUDE.md's "never docker/pipx/Tauri for local dev" is a dev-workflow instruction, not a statement that Tauri is retired as a shipping target. Whether Tauri desktop shipping continues is a one-way business decision (icons/Cargo.lock/release workflow are not cheaply reversible once deleted) that cannot be answered from git history alone — per this task's own `stop_if`, kept as-is pending the owner's answer. Escalated; not deleted. |
| REMOVED | `docs/tasks/PLAT-0042-T3-smoke-pool-instrumentation.md` | Implementation task for the PLAT-0042 query-decomposition story, which is fully RETIRED (`benchmarks/EXPERIMENTS.md:43`: "PLAT-0042, RETIRED by task 19e4e7f7" — measured across three corpora, never won, code removed from `brain_engine.py:2899` with a "QUERY DECOMPOSITION WAS REMOVED HERE" marker). Its own story link (`docs/stories/PLAT-0042-retrieval-query-decomposition.story.md`) does not exist in this repo. `git grep -l -F PLAT-0042-T3-smoke-pool-instrumentation.md` finds only this ledger and the guard test. |
| REMOVED | `docs/tasks/PLAT-0042-T4-run-experiments.md` | Same PLAT-0042 workstream as T3, same retirement evidence (`benchmarks/EXPERIMENTS.md:43`, task 19e4e7f7). `git grep -l -F PLAT-0042-T4-run-experiments.md` finds only this ledger and the guard test. |
| REMOVED | `docs/tasks/PLAT-0042-T5-operator-doc.md` | Same PLAT-0042 workstream, same retirement evidence. Its own step 1 points at `services/prism-service/app/engines/brain_engine.py` (a path that does not exist in this repo — the real file is at `services/prism-service/prism_service/engines/brain_engine.py`), confirming it was never executed against the current tree. `git grep -l -F PLAT-0042-T5-operator-doc.md` finds only this ledger and the guard test. |
| KEEP | `docs/stories` | Not tracked: `git ls-files` returns no path under `docs/stories` in this repo — there is nothing to delete, and the ticket's own premise that CLAUDE.md's Structure block still lists it as "Story files" does not hold here (`grep -n "docs/stories\|Story files" CLAUDE.md` finds nothing). Classified KEEP as a no-op rather than REMOVED: the surface is already fully absent, and "stories"/"story" is a common word elsewhere in the tracked repo (docs/specs, `.gitignore`, plugin docs, benchmark tests referencing unrelated PLAT-0042 story links) unconnected to this directory, so a REMOVED classification would fail the reference-scan on those unrelated hits. |

## Notes for the remaining slices (untracked junk, docs-truth, dead-marker triage)

This slice (the guard-test-driving classification of the ticket's named
candidates) is scoped to the six rows above, matching
`_TICKET_CANDIDATES` in the guard test. The broader slices the ticket also
describes — untracked build junk in the owner's live checkout, the
docs/specs and docs/validation truth-reconciliation, and the ~94-hit
dead-code-marker triage across `prism_service/` — are out of scope for
this pinned guard test and are tracked separately rather than bulk-applied
here, per this task's own out-of-scope note ("Nothing gets left in the
ambiguous middle" applies to classification, not to expanding this single
step's diff to cover every marker in one pass). The untracked-junk items
named in the ticket (`services/prism-service/services/prism-service/data/`,
the `E:.prism.*` log, `dev-native.log`, `build/`) were not found in this
task's own worktree, matching this task's premise capture — that check has
to run against the owner's real working checkout, not a fresh conductor
worktree, and is left for a follow-up pass there.
