---
name: sync-main
description: Fast-forward the local E:\.prism main checkout to origin/main (which must be siegeon/.prism — not resolve-io), reinstall the editable package if pyproject changed, rebuild the SPA if the web/ source changed, and bounce the dev daemon so /api/version reports the new build. Use when the user says "sync main", "pull main", "/sync-main", "update dev", "get dev current", or asks why dev is on an older version than origin. Refuses on dirty tree, divergent local, worktree cwd, or a wrong-fork origin.
version: 1.1.0
---

# Sync main → dev

## When to use

- "sync main" / "pull main" / "/sync-main"
- "update dev" / "get dev current" / "why is dev on an old version"
- After noticing the SPA footer version is behind the latest tag on origin

## Topology (do not violate)

- Operates ONLY against the main checkout at `E:\.prism`. If cwd is `.claude/worktrees/*`, abort — worktrees track their own branches and aren't sync targets.
- Canonical remote is `origin` → `https://github.com/siegeon/.prism` (per [[user-identity-and-fork-sync]]). There is no `resolve-io/.prism` remote on this machine and there must never be one — that fork is a downstream consumer, not an upstream. Never fetch from, pull from, or add it as a remote here. The sync target is always `origin/main`.
- Bounces the **Windows-native dev daemon on 8887/8888** (per [[prism-dev]]). Never touches the WSL pipx release daemon on 7777/7778.

## Preconditions (refuse if any fail)

1. cwd is `E:\.prism`.
2. Current branch is `main`.
3. `origin` fetch URL matches `github.com[:/]siegeon/\.prism(\.git)?$`. If any remote named `resolve`, `resolve-io`, or `upstream` points at `resolve-io/.prism`, refuse and tell the user to `git remote remove` it — never silently sync from the wrong fork.
4. Working tree clean wrt tracked files. `.dev-data/`, `.prism/projects/`, `.prism/.mcp_started`, `.prism/prism.pid` are expected untracked and don't count.
5. `git rev-list --count origin/main..main` == 0. Local ahead of origin is divergence, not a sync target — hand back to the user.

## Recipe

```powershell
Push-Location E:\.prism

# 0. Pin the upstream — siegeon/.prism only. Refuse if origin is wrong or a resolve-io remote exists.
$origin_url = (git remote get-url origin) 2>$null
if ($origin_url -notmatch 'github\.com[:/]siegeon/\.prism(\.git)?$') {
  Pop-Location; throw "origin is '$origin_url' — expected github.com/siegeon/.prism, refusing"
}
$bad = (git remote -v) -split "`n" | Where-Object { $_ -match 'resolve-io/\.prism' }
if ($bad) {
  Pop-Location; throw "found resolve-io/.prism remote(s):`n$($bad -join "`n")`nremove with: git remote remove <name>"
}

# 1. Fetch and figure out the delta
git fetch origin main
$behind = [int](git rev-list --count main..origin/main)
$ahead  = [int](git rev-list --count origin/main..main)
if ($ahead -ne 0)  { Pop-Location; throw "local main is $ahead ahead of origin — divergent, refusing" }
if ($behind -eq 0) { Pop-Location; Write-Output "already current"; return }

# 2. Snapshot what's about to change so we know whether to reinstall + rebuild
$changed = (git diff --name-only main origin/main) -split "`n"
$needs_pip = $changed | Where-Object { $_ -match '^services/prism-service/pyproject\.toml$' }
$needs_spa = $changed | Where-Object { $_ -match '^services/prism-service/prism_service/web/' -and $_ -notmatch '/web_dist/' }

# 3. Fast-forward only — never merge
git pull --ff-only origin main

# 4. Reinstall the editable package if pyproject moved (new deps / entry points)
if ($needs_pip) {
  uv pip install --python E:\.prism\.venvs\dev\Scripts\python.exe `
    -e E:\.prism\services\prism-service
}

# 5. Rebuild the SPA if any web/ source changed (.tsx, package.json, tailwind config, etc.)
if ($needs_spa) {
  Push-Location E:\.prism\services\prism-service\prism_service\web
  npm install --no-audit --no-fund
  npm run build
  Pop-Location
}

# 6. Bounce the dev daemon only if it's currently listening — don't start one that wasn't running
$running = Get-NetTCPConnection -State Listen -LocalPort 8888 -ErrorAction SilentlyContinue
if ($running) {
  Get-NetTCPConnection -State Listen -LocalPort 8887,8888 -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
  Start-Sleep 2

  $env:PRISM_MCP_PORT = "8887"
  $env:PRISM_UI_PORT  = "8888"
  $env:PRISM_DATA_DIR = "E:\.prism\services\prism-service\data"   # CANONICAL store (real data); NOT .dev-data — see feedback_dev_data_dir
  $env:PRISM_DEV_MODE = "1"
  Start-Process -FilePath "E:\.prism\.venvs\dev\Scripts\python.exe" `
    -ArgumentList "-m","prism_service.main" `
    -WorkingDirectory "E:\.prism" `
    -PassThru -NoNewWindow `
    -RedirectStandardOutput "$env:USERPROFILE\.claude\jobs\prism-dev-stdout.log" `
    -RedirectStandardError  "$env:USERPROFILE\.claude\jobs\prism-dev-stderr.log" | Out-Null

  # Poll /api/version until the new build is serving, then echo the version
  $deadline = (Get-Date).AddSeconds(20)
  while ((Get-Date) -lt $deadline) {
    try {
      $v = (Invoke-WebRequest -Uri "http://127.0.0.1:8888/api/version" -UseBasicParsing -TimeoutSec 2).Content | ConvertFrom-Json
      "now serving v$($v.version) (dev_mode=$($v.dev_mode))"
      break
    } catch { Start-Sleep -Milliseconds 500 }
  }
}

Pop-Location
```

## Done means

- `git status` shows clean `main` at `origin/main` HEAD (where `origin` = `siegeon/.prism`).
- If the daemon was running before: `/api/version` reports the new version, and the SPA footer reflects it on browser refresh.
- If the daemon wasn't running: skill exits after pull/rebuild — leave dev startup to [[prism-dev]].

## Gotchas

1. **Origin must be `siegeon/.prism`.** Per [[user-identity-and-fork-sync]], `resolve-io/.prism` is a downstream consumer, not an upstream. If a misconfigured remote ever points at it, refuse loudly — silently syncing from the wrong fork would poison local dev with foreign commits.
2. **`--ff-only`, never merge.** A divergent local main aborts cleanly. Merge commits on main are a separate decision and not this skill's job.
3. **Don't restart what wasn't running.** Detect via the 8888 listener — only bounce if present. Cold-starting dev belongs to [[prism-dev]].
4. **Conditional rebuild matters.** Most syncs don't touch `web/` or `pyproject.toml`; skipping the npm install in those cases saves ~20s per sync. The diff against `main..origin/main` runs BEFORE the pull so we still have both refs.
5. **Patch-bump policy ([[feedback-patch-bump-per-iteration]]) is unchanged.** This skill consumes already-shipped patch bumps from origin; it doesn't create them.