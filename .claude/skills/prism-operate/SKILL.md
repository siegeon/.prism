---
name: prism-operate
description: Operate the PRISM dev instance lifecycle on this Windows host — up / status / restart / stop / logs — as a DURABLE detached daemon (prism start --daemon + pidfile) on ports 8887 (MCP) / 8888 (web). Use when the user says "operate prism", "start dev", "is dev up", "restart dev", "stop dev", "dev status", "dev logs", "bounce dev", "the app is down", or any dev-lifecycle verb. Supersedes the one-shot launch in prism-dev: that used Start-Process, whose child is reaped when the background session tears down — this uses the CLI daemon so the instance survives.
version: 1.0.0
---

# PRISM operate (dev lifecycle)

The single operator for the Windows-native dev instance. Where `prism-dev`
*launches* once, this skill *operates* — up, status, restart, stop, logs —
and launches **durably** so the daemon does not die when a bg session ends.

## Topology (do not violate)

- **WSL pipx prism on 7777/7778** = release branch. Never touch it.
- **Windows-native source-run on 8887/8888** = dev. This skill owns it.
- **No other instances** — no docker, no second pipx, no random high ports.

## Why durable launch matters (the bug this skill fixes)

`prism-dev` launched the daemon with `Start-Process ... -NoNewWindow`. That
child is part of the background job's process tree, so when the Claude Code
bg session tears down, the daemon is reaped — `/api/version` goes from 200 to
"connection refused" with NO traceback in the log (it served fine until the
moment it was killed). **Fix:** launch with `prism start --daemon`, which
detaches the process and writes `<PRISM_DATA_DIR>\prism.pid`. The daemon then
outlives the session and is managed by pid, not by a live shell handle.

## Dev environment (every command sets these first)

```powershell
$env:PRISM_DATA_DIR             = "E:\.prism\services\prism-service\data"   # CANONICAL store: 12k brain docs / 88 tasks / 59 memories live here. NEVER .dev-data (empty) or %LOCALAPPDATA% (sparse) — see feedback_dev_data_dir
$env:PRISM_DEV_MODE             = "1"                      # amber DEV pill in SPA footer
$env:PRISM_AUTO_UPDATE          = "off"                    # never pip a release wheel over editable src
$env:PRISM_AUTO_UPDATE_INTERVAL = "0"                      # kill the poll loop
$env:PYTHONNOUSERSITE           = "1"                      # ignore %APPDATA% user-site shadow copy
$env:PRISM_WATCHDOG             = "off"                    # dev has NO out-of-process supervisor (services/supervisor.py runs only under the prod wrapper), so the in-process kill-watchdog's os._exit = permanent death. It false-positived on busy-but-healthy startup/index spells and killed dev on a ~90s loop. Dev never needs it — bounced constantly. (#155/#162 follow-up)
$env:PRISM_SUPERVISOR           = "off"                    # the #162 supervisor RESPAWNS the server every time it stops responding — each respawn pops a NEW console window. Harmless churn in prod (real wedge -> ~5s self-restart) but in dev it stacked ~20 empty windows when the watchdog kept self-killing. Dev wants ONE daemon, no auto-respawn.
```

Editable venv: `E:\.prism\.venvs\dev\Scripts\python.exe` (one-time setup lives
in the `prism-dev` skill — run that once if the venv is missing).

## up — start (or no-op if already healthy)

```powershell
# Rebuild SPA if .tsx changed (cheap, ~15s); skip only if certain nothing changed
Push-Location E:\.prism\services\prism-service\prism_service\web; npm run build; Pop-Location

# Set the dev env block above, then:
Push-Location E:\.prism
& "E:\.prism\.venvs\dev\Scripts\python.exe" -m prism_service.cli.prism_cli `
  start --daemon --ui-port 8888 --mcp-port 8887
Pop-Location
Start-Sleep 7
# Verify bound + dev mode (NOT just "process exists")
(Invoke-WebRequest "http://127.0.0.1:8888/api/version" -UseBasicParsing).Content |
  ConvertFrom-Json | Select-Object version, dev_mode
# Pop the chromeless window
Start-Process "msedge.exe" -ArgumentList `
  "--app=http://localhost:8888/?project=prism","--window-size=1440,900",`
  "--user-data-dir=$env:USERPROFILE\.claude\jobs\prism-edge-profile"
```

## status — is dev up?

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8887,8888 -ErrorAction SilentlyContinue |
  Select-Object LocalPort, OwningProcess
Get-Content "E:\.prism\services\prism-service\data\prism.pid" -ErrorAction SilentlyContinue
try { (Invoke-WebRequest "http://127.0.0.1:8888/api/version" -UseBasicParsing -TimeoutSec 5).Content |
        ConvertFrom-Json | Select-Object version, dev_mode }
catch { "DOWN: $($_.Exception.Message)" }
```

## restart — bounce to pick up code changes

```powershell
# Set dev env block, then:
Push-Location E:\.prism
& "E:\.prism\.venvs\dev\Scripts\python.exe" -m prism_service.cli.prism_cli stop
Push-Location E:\.prism\services\prism-service\prism_service\web; npm run build; Pop-Location
& "E:\.prism\.venvs\dev\Scripts\python.exe" -m prism_service.cli.prism_cli `
  start --daemon --ui-port 8888 --mcp-port 8887
Pop-Location
Start-Sleep 7
(Invoke-WebRequest "http://127.0.0.1:8888/api/version" -UseBasicParsing).Content |
  ConvertFrom-Json | Select-Object version, dev_mode
```

## stop

```powershell
$env:PRISM_DATA_DIR = "E:\.prism\services\prism-service\data"
& "E:\.prism\.venvs\dev\Scripts\python.exe" -m prism_service.cli.prism_cli stop
```

If the pidfile is stale (process gone but file remains), fall back to port kill —
validate the port first, never blind-kill:

```powershell
Get-NetTCPConnection -State Listen -LocalPort 8887,8888 -ErrorAction SilentlyContinue |
  ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
```

## logs

```powershell
& "E:\.prism\.venvs\dev\Scripts\python.exe" -m prism_service.cli.prism_cli logs --follow
```

## Gotchas

1. **cwd trap.** Always run from `E:\.prism` (main checkout). If cwd is a
   worktree containing `services/prism-service/prism_service/`, `sys.path[0]`
   resolves the package there first and `web_dist` points at an unbuilt tree →
   every `GET /` is 503 "SPA build missing." Build the SPA in the worktree too,
   or just launch from the main checkout.
2. **Don't kill 7777/7778** — that's the WSL release daemon. The auto-classifier
   blocks it anyway.
3. **Patch-bump every user-visible commit** (PRISM_VERSION last digit +
   PRISM_VERSION_NOTES). The DEV pill + footer is how the user confirms the build.
4. **No `-ErrorAction SilentlyContinue` on destructive ops** (CLAUDE.md). Validate
   the port owner before Stop-Process.

## Known latent issue (not a launch blocker)

`prism_service/services/claude_memory.py` is referenced by `__version__.py` notes
and `tests/unit/test_claude_memory_import.py` but **does not exist** in the repo
(absent on origin/main — never landed, not a deletion). The transcript importer
guards the import (`cm = None`), so the daemon runs fine; the only effect is the
recurring `memory import error ImportError: cannot import name 'claude_memory'`
in stderr and auto-memory bridge being disabled. Landing that module is a
separate feature task — do not let this noise read as "dev is broken."

## What "running" actually means

- ❌ "daemon is listening" / "GET / 200" / "started in background" — not visible
- ✅ Edge `--app` window open AND SPA renders AND footer shows `v<X.Y.Z> [DEV]`,
  with `/api/version` returning `"dev_mode":true`.
