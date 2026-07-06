---
name: ship
description: Deliver a PRISM release end-to-end — local gates → checkin → push → CI → tag → publish → production verify → local dev refresh. Use when the user says "ship", "ship it", "release", "publish v6.0.X", "cut a release", "push and release", or has finished a chunk of patch-bumped work and wants it out the door. Refuses to advance at any failed gate. Ends with BOTH the release surface (:7778) AND the local dev surface (:8888) reporting the new PRISM_VERSION.
version: 1.2.0
---

# /ship — deliver PRISM release end-to-end

## When to use

- "ship" / "ship it" / "let's ship" / "ship the build"
- "release" / "cut a release" / "publish v6.0.X" / "push and release"
- User has bumped `PRISM_VERSION`, the dev daemon shows the new version + DEV pill, and they want it published to the release surface.

**Do NOT use for:** WIP commits without release intent (use plain git), branch syncing (use [[sync-main]]), starting the dev daemon (use [[prism-dev]]).

## Topology (do not violate)

- **WSL pipx prism on 7777/7778** = the release surface. The whole point of /ship is to flip its `/api/version` to the new value.
- **Windows-native source-run on 8887/8888** = dev. Pre-flight checks run here AND it must end the ship on the new version too (per [[dev-must-stay-current]]).
- **Repo**: `E:\.prism`. Origin = `siegeon/.prism`. Never push to upstream `resolve-io/.prism` from this skill.

## Pre-flight (refuse to ship if any fail)

1. **Version bumped + changelog entry**: `PRISM_VERSION` in `services/prism-service/prism_service/__version__.py` must not match any existing `v*` git tag (avoid double-publish). Per [[patch-bump-per-iteration]], every user-visible change bumps the patch — if this skill fires and the version is unchanged, refuse and tell the user to bump first. ALSO confirm `PRISM_VERSION_NOTES` has a leading entry for this exact version (it is the canonical changelog — no separate `CHANGELOG.md`). A bumped number with no notes entry ships a blank changelog; refuse until both are present.
2. **No merge markers anywhere**: `git grep -nE '^<<<<<<< |^=======$|^>>>>>>> '` returns nothing. Pre-merged conflicts have broken /api/version on this project before — never ship with them present.
3. **Branch is not protected**: `git branch --show-current` ≠ `main`/`master`/`staging`/`develop`. Ship from a feature/release branch only.
4. **Clean working tree** (or only the intended diff): `git status --short` reviewed, all untracked files explained.

## Local gates (refuse to push if any fail)

5. **TypeScript clean**: `cd services/prism-service/prism_service/web && npx tsc -b` exits 0.
6. **SPA build clean**: `npm run build` in the same dir produces a fresh `web_dist/` with no errors.
7. **Pytest green**: `cd services/prism-service && pytest tests/unit -q` exits 0 with ZERO failures. There is no "acceptable pre-existing failure" set — every red test is a blocker. If a test is broken, fix the test or fix the code; do not ship around it. **Common case (v6.2.x):** a feature that adds an MCP tool or a startup worker thread breaks the *count* assertions in `test_mcp_tool_profiles.py` (interactive tool count) and `test_lifespan_lock_recovery.py` (daemon thread count). Those are intentional drift — update the asserted number + its comment to the new value; that IS fixing the test, not shipping around it.

> **Local gates first, PR CI second — both must be green.** As of v6.3.18 this repo HAS a PR-triggered CI gate: `.github/workflows/pr-checks.yml` re-runs `tsc -b` + SPA build + `pytest tests/unit` on every `pull_request` into main. Run the local gates (steps 5–9) to fail fast BEFORE pushing, then (step 15) the SAME checks run again on the PR and **must pass before merge**. `release.yml`/`release-tauri.yml` still only fire on the tag push (post-merge) — they are publish steps, not the quality gate.
8. **Dev daemon healthy**: `curl http://localhost:8888/api/version` returns the bumped `PRISM_VERSION`. If not, restart per [[prism-dev]] before ship continues.
9. **/verify**: invoke the `verify` skill against the working diff. FAIL or BLOCKED stops the ship; PASS proceeds.

## Checkin

10. Stage specific files only — never `git add -A` or `git add .` (per CLAUDE.md, accidental .env/credential inclusion risk).
11. Commit message format: `<type>(v<VERSION>): <one-line summary>` matching existing convention. Types: `feat`, `fix`, `chore`, `docs`, `refactor`.
12. Trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` per project convention (use the running model's name/version).

## Push + PR

13. `git push -u origin <branch>` to siegeon.
14. `gh pr create --repo siegeon/.prism --title "<type>(v<VERSION>): <summary>" --body "..."`. If the work is linked to a GH issue, include `closes #N` in the body.

## CI gate

15. **Wait for the PR CI to pass — this is a hard gate, never merge a red or pending PR.** `pr-checks.yml` (v6.3.18+) triggers on the `pull_request` (and on each push to the PR branch as `synchronize`), so a freshly-pushed PR has a run within seconds. Watch it to completion:
    - Find the run: `gh run list --branch <branch> --event pull_request --limit 1 --json databaseId,status,conclusion`.
    - Block on it: `gh run watch <run-id> --repo siegeon/.prism --exit-status` (exit 0 = pass; non-zero = fail).
    - On failure: `gh run view <run-id> --log-failed` to see which gate broke, fix it, push the fix (which re-triggers CI), and re-watch. Do NOT merge.
    - Only when the run concludes **success** AND `gh pr view <PR> --json mergeable,mergeStateStatus` reads `MERGEABLE`/`CLEAN` do you proceed to merge.
    - Edge: if for some reason NO run appears within ~30s (workflow file not yet on the PR head), confirm `pr-checks.yml` is present on the branch head and re-push; the local gates (5–9) are the fallback only if PR CI is genuinely unavailable.

## Release (only when shipping to main)

> **Worktree trap (common — ship often runs from `.claude/worktrees/...`).** `git checkout main` fails or is wrong inside a worktree (main is checked out elsewhere). For ALL main-checkout git ops below, target the real checkout explicitly with `git -C E:\.prism …` rather than `cd`/`checkout` in the current dir. Merging is via `gh` (API, cwd-independent) so it's safe anywhere.

16. Merge: `gh pr merge <PR> --repo siegeon/.prism --merge` (project convention; never squash without confirmation).
17. Fast-forward the real main checkout: `git -C E:\.prism pull --ff-only origin main`. Confirm `__version__.py` on main now reads `<VERSION>`.
18. Tag from main: `git -C E:\.prism tag v<VERSION> && git -C E:\.prism push origin v<VERSION>`. This is the trigger — `release.yml` builds the wheel; `release-tauri.yml` builds signed macOS/Windows/Linux bundles + `latest.json` updater manifest.
19. Watch the wheel run to completion (it's the one the pipx release surface needs): `gh run watch <release.yml run-id> --repo siegeon/.prism --exit-status`. The Tauri run can finish independently — "shipped" for pipx depends on the wheel, not the bundles. On failure, `gh run view --log` and STOP.
19a. **Verify the release is published as `latest` with an installable wheel** — this is what the production auto-updater consumes, so it's the real "production will update" gate (you usually can't reach `:7778` from the dev box; see Handoff). Check:
    `gh api repos/siegeon/.prism/releases/latest -q '{tag:.tag_name,draft:.draft,prerelease:.prerelease,wheel:([.assets[].name]|map(select(endswith(".whl")))[0])}'`
    Must show `tag: v<VERSION>`, `draft: false`, `prerelease: false`, and a PEP-427 wheel name (`prism_service-<VERSION>-py3-none-any.whl`). If `latest` points at an older tag or the wheel is missing/misnamed, production will NOT auto-update — investigate before declaring success.
19b. **Wheel-install smoke (catches "green build, broken wheel" — the v6.0.34 class):** green CI ≠ installable. In a throwaway venv, `pip install <published-wheel-url>` and run `python -c "import prism_service; print(prism_service.__version__.PRISM_VERSION)"`. If it fails (PEP-427 name, missing dep, pip-less pipx venv), the auto-update will fail too — fix and re-cut.

## Handoff verify — release surface

> **The auto-updater IS the flip mechanism — by design.** Production (`:7778`) polls `releases/latest` every `PRISM_AUTO_UPDATE_INTERVAL` (default 30 min) and self-updates when a newer non-draft release with an installable wheel exists. So if step 19a passed, production WILL flip on its own — there is no manual push required and no "gap." Do NOT frame the auto-update as a shortcoming; the only job here is to (a) confirm the release was published correctly and (b) confirm/observe the flip.

20. **Confirm the flip — preferred order:**
    - If `:7778` is reachable from this session: `curl http://localhost:7778/api/version`. If still old, either wait for the poll, or `curl -X POST http://localhost:7778/api/update/check` then `…/api/update/apply` to apply now, then re-curl until it reads `<VERSION>`.
    - **If `:7778` is NOT reachable** (common — it's the WSL-bound host and Windows port-forwarding may not expose it to this session): that is NOT a failure. Step 19a (`releases/latest` = v<VERSION>, installable wheel) is the binding proof that production will auto-update. State clearly: "published correctly; :7778 auto-updates on its next poll" — do not claim to have *observed* the flip if you couldn't reach it.
21. Confirm the user-visible change is actually reachable — **on a FRESH load, never a cached view** (see the cache gotcha below). Pop/refresh the release Edge `--app` at `http://localhost:7778/?project=prism` and verify the feature renders.

## Local dev refresh — non-negotiable (per [[feedback_dev_must_stay_current]])

The release surface is one of two daemons the user runs. The Windows-native dev daemon on 8887/8888 must also end up on `v<VERSION>` — otherwise the user opens dev tomorrow, sees the old version in the SPA footer, and reasonably concludes "ship was a lie." A successful ship that leaves local dev stale fails the standing mandate.

21c. **Clean up local + switch back to mainline (explicit user expectation).** After the merge: pass `--delete-branch` to `gh pr merge` (or `gh api -X DELETE repos/siegeon/.prism/git/refs/heads/<branch>`) to drop the remote feature branch, then on the local checkout switch back to main and pull latest — `git -C E:\.prism checkout main && git -C E:\.prism pull --ff-only origin main` — and delete the merged local branch `git -C E:\.prism branch -d <branch>`. End state: the working checkout is on `main` at `v<VERSION>`, not left stranded on the merged feature branch. (sync-main in step 22 assumes it is operating on the main checkout — this step guarantees that.)
22. Invoke [[sync-main]]. It fast-forwards the local `E:\.prism` main checkout to `origin/main` (which carries the merged PR), reinstalls the editable package if `pyproject.toml` changed, rebuilds the SPA if any `prism_service/web/` source changed, and bounces the dev daemon. Refuses on dirty tree, divergent local, worktree cwd, or a wrong-fork origin.
23. Confirm `curl http://localhost:8888/api/version` returns `"version": "<VERSION>"`. If not — even after bounce — the editable install is pinned to a stale path or `PYTHONPATH` is dirty; fix that before declaring shipped. Do NOT manually edit the served version string to paper over a config bug.
24. If `sync-main` errors (most commonly: skill not available in this session, or the local checkout sits in a worktree), fall back to inline from a non-worktree cwd:
    - `git -C E:\.prism status --porcelain` → if clean, `git -C E:\.prism pull --ff-only origin main`
    - If `pyproject.toml` changed in the new range: `pip install -e E:\.prism\services\prism-service` against the dev venv
    - If `prism_service/web/` changed: from `E:\.prism\services\prism-service\prism_service\web`, `npm install && node_modules/.bin/vite build`
    - Restart dev daemon via [[prism-dev]] (handles PIDfile + `PRISM_DATA_DIR=E:\.prism\services\prism-service\data` — the CANONICAL store; NOT .dev-data — per [[feedback_dev_data_dir]])
    - Re-confirm step 23.

## What "shipped" actually means

The user wants the **new feature visible on the release surface AND the local dev surface**. These DON'T count:

- ❌ "commit pushed" — branch state, not release
- ❌ "PR merged" — main moved, but no published artifact
- ❌ "tag pushed" — CI is building, nothing published yet
- ❌ "release.yml green" — wheel published, but the running release daemon hasn't updated
- ❌ "release :7778 flipped" — release current but local dev :8888 still serves the old build, so the user's actual dev surface lies about the version

Only this counts:

- ✅ `releases/latest` = `v<VERSION>` (non-draft, installable wheel) — the binding proof production WILL auto-update (step 19a)
- ✅ `curl http://localhost:7778/api/version` returns `<VERSION>` **if reachable**; if not reachable from this session, the step-19a proof stands in — but say so honestly, don't claim to have watched it flip
- ✅ the user sees the new feature in the production Edge window — **on a fresh/hard-reloaded load**, not a cached one
- ✅ `curl http://localhost:8888/api/version` returns `<VERSION>` so the dev surface matches release

The dev surface (:8888) IS reachable and IS your responsibility — if it doesn't read `<VERSION>` after the refresh in steps 22–24, that's a real bug to fix; don't paper over it by editing the served string. For production, "published correctly + auto-updates" is the contract; only claim an *observed* flip if you actually curled it.

## Gotchas

- **Tauri release needs GITHUB_TOKEN passed explicitly** (v6.0.27 fix). If `release-tauri.yml` fails on the upload step with "GITHUB_TOKEN is required", check that the workflow's step env block still carries `GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}`. Job-level `permissions: contents: write` alone isn't enough — tauri-apps/tauri-action reads it from `process.env` directly.
- **Don't skip the version bump check**. PRISM_VERSION_NOTES is the canonical changelog — there is no separate `CHANGELOG.md` to maintain. Per [[patch-bump-per-iteration]], every user-visible commit bumps the patch.
- **Never `git push --force` to main.** Even with explicit user request, refuse and ask twice. The release infra is downstream and gets weird with rewritten tags.
- **Watchtower auto-update lag**: on Linux/Mac the auto-updater applies in-place; on Windows it surfaces "restart required" because pip can't replace a running python.exe. Tauri shells handle this via the embedded `latest.json` updater. Either way, the release daemon may need explicit attention if step 20 doesn't flip on its own.
- **Local dev cwd trap** (per [[feedback_local_preview_ports]]): the editable pip install is pinned to a specific path. If `prism` daemon serves the wrong code after sync-main, suspect a worktree cwd or stale `pip install -e` target; reinstall from `E:\.prism\services\prism-service` explicitly.
- **Stale-cache trap — verify the FIX, not a cached view (v6.2.3 lesson).** A feature can be correct server-side yet look broken because a client cached the old payload. Real case: community labels were enriched in `graph.db` and `hierarchy.json` served the new names, but the Sigma viewer had **cached `hierarchy.json`** and kept painting the old "prism service" labels on the same nodes — the user reasonably said "you did not fix it." Before claiming any data/UI change shipped: (1) verify against a **fresh** fetch (curl the API directly, or hard-reload / cache-bust the client), and (2) if a client fetches data that changes out-of-band (background enrichment, async workers), make sure that fetch is cache-busted / `no-store`. Matching counts + wrong labels = a caching problem, not a logic bug.
- **Resume after the tag (don't re-bump, don't refuse).** Once the tag is pushed it's public and irreversible-ish. If a downstream step fails (Tauri upload, wheel smoke) the pre-flight version check (step 1) will then *refuse* a re-run because the tag exists — that's a false block. Instead RESUME the remaining steps for the already-pushed tag (re-watch the run, re-trigger via `gh workflow run` or re-verify `releases/latest`). Only cut a new patch version if the published artifact itself is wrong and must be replaced; never delete/re-push an existing release tag to "redo" it.

## Dogfood option (recommended)

Wrap the ship in a PRISM task driven through conductor so the release shows up on `/conductor` with full audit trail:

1. `task_create` titled "Ship v<VERSION>".
2. Map the 8 SDLC steps onto the ship phases:
   - `review_previous_notes` / `draft_story` / `verify_plan` = pre-flight (steps 1-4)
   - `write_failing_tests` = local gates (steps 5-9)
   - `red_gate` = local gates verdict (approve only if all green)
   - `implement_tasks` = checkin + push + PR (steps 10-14)
   - `verify_green_state` = CI gate + release tags + workflow runs (steps 15-19)
   - `green_gate` = handoff verify, release + LOCAL dev (steps 20-24)
3. Each `conductor_gate(approve)` carries the validation evidence collected at that phase (per [[gate-validation]]).
4. The ship becomes a row on `/conductor` swimlanes — visible to anyone, replayable from history.
