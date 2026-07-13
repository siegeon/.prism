@echo off
rem PRISM dev daemon — MANUAL launcher (owner declined any always-on
rem persistence 2026-07-13; do NOT wire this into a scheduled task or
rem service). Double-click or run from any shell to bring dev up with the
rem canonical env. PRISM_DEV_WATCH makes the running daemon re-exec itself
rem whenever the source on disk changes, so it stays current with edits.
set PRISM_MCP_PORT=8887
set PRISM_UI_PORT=8888
set PRISM_DATA_DIR=E:\.prism\services\prism-service\data
set PRISM_DEV_MODE=1
set PRISM_AUTO_UPDATE=off
set PRISM_AUTO_UPDATE_INTERVAL=0
set PYTHONNOUSERSITE=1
rem PRISM_DEV_WATCH stays OFF here: the watch restart rides the auto-updater's
rem os.execv path, which DIES on Windows without a supervisor to respawn it
rem (verified live 2026-07-13: touch -> re-exec -> successor died, 0 daemons).
rem Only enable it under `prism start --daemon` (supervisor respawns on exit).
cd /d E:\.prism
"E:\.prism\.venvs\dev\Scripts\python.exe" -m prism_service.main >> "%USERPROFILE%\.claude\jobs\prism-dev-stdout.log" 2>> "%USERPROFILE%\.claude\jobs\prism-dev-stderr.log"
