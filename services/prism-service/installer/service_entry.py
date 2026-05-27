"""PyInstaller entrypoint for the bundled prism-service sidecar.

Frozen as `prism-service.exe` and dropped next to `prism-shell.exe` by
the Tauri installer. The Tauri shell spawns this exe instead of looking
for a host-installed Python — the user never has to run
`pipx install prism-service`.

Why a dedicated entry instead of reusing `prism_service.cli.prism_cli`:
the CLI's foreground `start` re-execs `sys.executable -m prism_service.main`,
which doesn't work inside a PyInstaller --onefile bundle (sys.executable
points at the frozen exe; there's no `-m` semantics on the embedded
interpreter). Tauri only ever needs one launch mode — synchronous
foreground uvicorn — so we sidestep the CLI entirely.

PyInstaller resolves `prism_service.main`'s deferred imports (FastAPI
routes, engines, etc.) via the hidden-imports list in pyinstaller.spec.
The SPA bundle at prism_service/web_dist/ is added as bundled data and
loaded from sys._MEIPASS at runtime via the existing
`Path(__file__).parent / "web_dist"` lookup in main.py.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    # Default ports match the Tauri shell's WebView URL (7778) and the
    # MCP transport (7777). Honor any env overrides the shell wants to
    # pass through.
    os.environ.setdefault("PRISM_UI_PORT", "7778")
    os.environ.setdefault("PRISM_MCP_PORT", "7777")

    # Force UTF-8 stdio so version notes / em-dashes in logs don't blow
    # up under Windows cp1252.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    # Deferred import — keeps PyInstaller's static analysis happy and
    # surfaces a clear error message if the frozen bundle is missing a
    # heavy dep (torch / fastapi / etc).
    import uvicorn
    from prism_service.config import UI_PORT
    from prism_service.main import app

    uvicorn.run(app, host="127.0.0.1", port=UI_PORT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
