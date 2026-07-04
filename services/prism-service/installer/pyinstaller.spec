# PyInstaller spec for the bundled prism-service Tauri sidecar.
#
# Build with (from services/prism-service/):
#   pyinstaller installer/pyinstaller.spec --noconfirm --clean
#
# Output: dist/prism-service.exe (Windows) — a single-file frozen
# bundle that the Tauri installer ships next to prism-shell.exe via
# bundle.externalBin in tauri.conf.json.
#
# Prereqs satisfied by CI before invoking this spec:
#   - npm run build inside prism_service/web/  (populates web_dist/)
#   - pip install -e .[dev] inside services/prism-service
#   - pip install pyinstaller
#
# `prism_service/web_dist/` is bundled as data (the SPA the frozen
# uvicorn serves at /). Hidden imports cover the dynamic / lazy
# imports PyInstaller's static analysis misses.
# -*- mode: python ; coding: utf-8 -*-

import os
import sys
from pathlib import Path

# When PyInstaller invokes a spec, __file__ is not defined — anchor on
# the spec's own dir via SPECPATH (PyInstaller injects this).
HERE = Path(SPECPATH).resolve()
SERVICE_ROOT = HERE.parent
PKG_ROOT = SERVICE_ROOT / "prism_service"
WEB_DIST = PKG_ROOT / "web_dist"

if not WEB_DIST.exists():
    raise SystemExit(
        f"web_dist missing at {WEB_DIST} — run `npm run build` in "
        f"prism_service/web/ before invoking pyinstaller"
    )

# Static assets the running service reads from disk relative to the
# package root. PyInstaller copies these into sys._MEIPASS at runtime;
# main.py's `Path(__file__).parent / 'web_dist'` resolves correctly
# because PyInstaller rewrites __file__ for frozen modules.
datas = [
    (str(WEB_DIST), "prism_service/web_dist"),
    (str(PKG_ROOT / "inference" / "prompts"), "prism_service/inference/prompts"),
]

# Hidden imports — modules referenced only via deferred / dynamic
# import sites that PyInstaller's static analysis can't follow.
# Errs on the side of completeness: 5MB of extra bytecode is cheaper
# than a ModuleNotFoundError at runtime on a customer machine.
hiddenimports = [
    # Service modules pulled in lazily from main.py timer threads
    "prism_service.engines.brain_engine",
    "prism_service.engines.conductor_engine",
    "prism_service.engines.understand_engine",
    "prism_service.engines.query_decomposer",
    "prism_service.engines.mulch",
    "prism_service.mcp.server",
    "prism_service.mcp.tools",
    "prism_service.mcp.understand_tools",
    "prism_service.routes.sse",
    "prism_service.routes.graph_static",
    "prism_service.services.scoring_service",
    "prism_service.services.reflection_runner",
    "prism_service.services.claude_transcripts",
    "prism_service.services.auto_updater",
    "prism_service.project_context",
    # Third-party deferred / plugin-style imports
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "sqlite_vec",
    "model2vec",
    "tokenizers",
    "sentence_transformers",
    "graphifyy",
    "tree_sitter",
    "tree_sitter_language_pack",
    # MCP transport surface — pulled by name in server.py
    "mcp.server.lowlevel",
    "mcp.server.streamable_http",
]


a = Analysis(
    [str(HERE / "service_entry.py")],
    pathex=[str(SERVICE_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Heavy optional deps we don't need at runtime
        "tkinter",
        "matplotlib",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "IPython",
        "jupyter",
        "notebook",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

# --onefile equivalent: collect binaries + data into the single exe.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="prism-service",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
