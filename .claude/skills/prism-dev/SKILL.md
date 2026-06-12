---
name: prism-dev
description: Launch the PRISM dev instance on this Windows host — source-run native (editable pip install), ports 8887 (MCP) / 8888 (web), Edge --app window. Use when the user says "start dev", "run our app", "hit play", "launch the dev instance", "start prism", "is dev running", or any variant. This is THE way to run PRISM locally for development — never propose docker / pipx / Tauri Rust compile as alternatives.
version: 1.0.0
---

# PRISM dev launcher

## When to use

Any time the user wants the PRISM app running locally for development on this Windows host. Triggers:

- "start dev" / "run our app" / "hit play" / "launch the dev instance"
- "is dev up?" / "start prism" / "spin up dev"
- "the app should be running" / user is looking at an empty browser window

## Topology (do not violate)

- **WSL pipx prism on 7777/7778** = "my linux machine on release branch." Leave it alone.
- **Windows-native source-run on 8887/8888** = dev. This is what this skill manages.
- **No other instances.** No docker preview, no second pipx, no random high ports.

## Recipe

All paths assume `E:\.prism` is the repo root. The user develops in the main checkout; bg sessions may be in `.claude/worktrees/<name>/`. The recipe uses the main checkout as the editable install target.

### One-time setup (skip if `E:\.prism\.venvs\dev\Scripts\python.exe` exists)

```powershell
uv venv E:\.prism\.venvs\dev --python 3.12
uv pip install --python E:\.prism\.venvs\dev\Scripts\python.exe `
    --index-url https://download.pytorch.org/whl/cpu "torch>=2.2"
uv pip install --python E:\.prism\.venvs\dev\Scripts\python.exe `
    -e E:\.prism\services\prism-service
Push-Location E:\.prism\services\prism-service\prism_service\web
npm install --no-audit --no-fund
npm run build
Pop-Location
```

### Every launch

```powershell
# Stop any existing dev daemon
Get-NetTCPConnection -State Listen -LocalPort 8887,8888 -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep 2

# Rebuild SPA if .tsx changed since last build
# (cheap — Vite finishes in ~3s; skip only if you're sure nothing changed)
Push-Location E:\.prism\services\prism-service\prism_service\web
npm run build
Pop-Location

# Launch — CRITICAL: cwd must be E:\.prism (main checkout root), NOT a worktree
$env:PRISM_MCP_PORT  = "8887"
$env:PRISM_UI_PORT   = "8888"
$env:PRISM_DATA_DIR  = "E:\.prism\services\prism-service\data"   # CANONICAL store (real data); NOT .dev-data — see feedback_dev_data_dir
$env:PRISM_DEV_MODE  = "1"        # surfaces the amber DEV pill in the SPA footer
# Belt-and-suspenders: PRISM_DEV_MODE already gates the auto-updater's
# pip-install path, but make the dev daemon never even consider self-updating.
$env:PRISM_AUTO_UPDATE          = "off"  # never pip-install a release wheel over the editable source
$env:PRISM_AUTO_UPDATE_INTERVAL = "0"    # kill the background poll loop entirely
$env:PYTHONNOUSERSITE           = "1"    # ignore the %APPDATA% user-site shadow copy
Start-Process -FilePath "E:\.prism\.venvs\dev\Scripts\python.exe" `
  -ArgumentList "-m","prism_service.main" `
  -WorkingDirectory "E:\.prism" `
  -PassThru -NoNewWindow `
  -RedirectStandardOutput "$env:USERPROFILE\.claude\jobs\prism-dev-stdout.log" `
  -RedirectStandardError  "$env:USERPROFILE\.claude\jobs\prism-dev-stderr.log"
Start-Sleep 5

# Verify it actually bound + serves the SPA (not a 503)
Invoke-WebRequest -Uri "http://127.0.0.1:8888/api/version" -UseBasicParsing |
  Select-Object -ExpandProperty Content

# Pop the native window (Edge in --app mode, chromeless)
Start-Process "msedge.exe" -ArgumentList `
  "--app=http://localhost:8888/?project=prism", `
  "--window-size=1440,900", `
  "--user-data-dir=$env:USERPROFILE\.claude\jobs\prism-edge-profile"
```

### One-time project creation (if `/api/projects` is empty)

```powershell
$body = '{"name":"prism","source_path":"E:\\.prism"}'
Invoke-WebRequest -Uri "http://127.0.0.1:8888/api/projects" -Method Post `
  -Body $body -ContentType "application/json" -UseBasicParsing
```

## Gotchas (each cost ~20 min the first time)

1. **The cwd trap.** If the bg session's cwd is inside a worktree (`.claude/worktrees/*/`), and that worktree also contains `services/prism-service/prism_service/`, Python's `sys.path[0] = ''` resolves the package from the worktree path FIRST, ignoring the editable install. `Path(__file__).parent / "web_dist"` then points inside the worktree — where the SPA was never built — and every `GET /` returns 503 "SPA build missing." **Fix:** always launch with `-WorkingDirectory "E:\.prism"` (main checkout). If the daemon must run from the worktree, build the SPA there too (`npm run build` in the worktree's `web/`).

2. **Don't compile Tauri shell for dev.** It exists at `services/prism-service/desktop/tauri-shell/` but `lib.rs` hardcodes `--ui-port 7778 --mcp-port 7777` and an old worktree cwd; `tauri.conf.json` hardcodes `http://localhost:7778`. First-time Rust compile is 5–10 min. Edge `--app` mode gives a chromeless native-feeling window in <1s. Until the Tauri shell is env-driven, use Edge.

3. **Don't kill the WSL pipx daemon on 7777/7778.** The auto-classifier will block you anyway. That's the user's release-branch instance — separate from dev.

4. **Patch-bump every user-visible commit** (per [[feedback-patch-bump-per-iteration]]). Bump `PRISM_VERSION` last digit + append a note in `PRISM_VERSION_NOTES`. The DEV pill + footer is how the user confirms they're on the latest build.

5. **No -ErrorAction SilentlyContinue on destructive ops** (per CLAUDE.md). Validate paths before deleting.

## What "running" actually means

The user wants the **window visible** with the SPA loaded. None of these alone count as done:

- ❌ "daemon is listening on 8888" — not visible
- ❌ "GET / returns 200" — not visible
- ❌ "I started it in the background" — not visible
- ✅ Edge `--app` window is open AND SPA renders AND footer shows `v<X.Y.Z> [DEV]`

Verify with `Invoke-WebRequest -Uri "http://127.0.0.1:8888/api/version"` and confirm `"dev_mode":true` is in the response.
