"""Task b407c930 — prism_install ships blank content for the two
prism-reflect files (github issue #214).

Root cause (measured against baseline 422c4d6a): a REAL build
(setuptools.build_meta.build_wheel / build_sdist) carries only 8 of the
10 files under prism_service/assets/ — the 8 *_hook.py files ship,
prism_reflect_agent.md and prism_reflect_command.md do not, because
neither [tool.setuptools.package-data] (pyproject.toml) nor MANIFEST.in
has an assets/ entry. tools.py's _load_asset() then swallows the
resulting read failure with a bare `except Exception: return ""`, so
_install_manifest ships blank content for those two install_files
entries with no signal to the caller.

These tests assert against BUILT ARTIFACTS (wheel/sdist namelists, and
an unpacked copy of the wheel imported in a subprocess) — never against
the source tree in-process, which tests/unit/test_install_manifest.py
already does today and is structurally blind to this bug (the source
tree always has all 10 files on disk).

`build`/`wheel` front-end packages are NOT installed in the dev venv
(confirmed via `python -c "import build"` -> ModuleNotFoundError), so
builds go straight through `setuptools.build_meta.build_wheel` /
`build_sdist`, exactly like a PEP 517 front-end would call them.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent  # services/prism-service
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# The 10 files that live on disk under prism_service/assets/ today.
_ASSET_NAMES = (
    "edit_learn_hook.py",
    "feedback_signal_hook.py",
    "hook_logger.py",
    "idle_rebuild_hook.py",
    "prism_reflect_agent.md",
    "prism_reflect_command.md",
    "skill_usage_hook.py",
    "stop_record_hook.py",
    "subagent_record_hook.py",
    "verifier_hook.py",
)
assert len(_ASSET_NAMES) == 10, "asset roster drifted — update this list with it"


def _build(build_fn, out_dir: Path) -> Path:
    """build_meta resolves the project root from the process CWD, and this
    monorepo's workspace root has multiple top-level dirs (plugins/,
    services/) that trip setuptools' flat-layout auto-discovery — so the
    build must run with cwd = services/prism-service (pyproject.toml's own
    directory), restored afterward."""
    prev = os.getcwd()
    os.chdir(_SERVICE_ROOT)
    try:
        name = build_fn(str(out_dir), config_settings=None)
    finally:
        os.chdir(prev)
    return out_dir / name


@pytest.fixture(scope="session")
def built_wheel(tmp_path_factory) -> Path:
    """A REAL wheel, built via setuptools.build_meta.build_wheel (the
    build_meta hooks a PEP 517 front-end calls) — not `python -m build`."""
    from setuptools import build_meta as _bm

    return _build(_bm.build_wheel, tmp_path_factory.mktemp("wheel_out"))


@pytest.fixture(scope="session")
def built_sdist(tmp_path_factory) -> Path:
    from setuptools import build_meta as _bm

    return _build(_bm.build_sdist, tmp_path_factory.mktemp("sdist_out"))


def _asset_basenames(names, marker: str = "prism_service/assets/") -> set[str]:
    """Prefix-tolerant: sdist members carry a `prism_service-<ver>/` prefix,
    wheel members do not. Match on the marker substring, not a fixed prefix."""
    out = set()
    for n in names:
        n = n.replace("\\", "/")
        if marker in n and not n.endswith("/"):
            out.add(n.split(marker, 1)[1])
    return out


# ----------------------------------------------------------------------
# AC-2 / AC-3 — the built artifacts themselves must carry all 10 files
# ----------------------------------------------------------------------


def test_wheel_contains_all_10_asset_entries(built_wheel):
    with zipfile.ZipFile(built_wheel) as zf:
        found = _asset_basenames(zf.namelist())
    missing = set(_ASSET_NAMES) - found
    assert not missing, (
        f"wheel {built_wheel.name} is missing prism_service/assets/ "
        f"entries: {sorted(missing)} (found {sorted(found)})"
    )


def test_sdist_contains_all_10_asset_entries(built_sdist):
    with tarfile.open(built_sdist) as tf:
        found = _asset_basenames(tf.getnames())
    missing = set(_ASSET_NAMES) - found
    assert not missing, (
        f"sdist {built_sdist.name} is missing prism_service/assets/ "
        f"entries (prefix-tolerant match): {sorted(missing)} "
        f"(found {sorted(found)})"
    )


# ----------------------------------------------------------------------
# AC-4 — prism_install manifest, built from an UNPACKED COPY of the real
# wheel, imported in a SUBPROCESS (never in-process: this test process
# still has the editable source-tree install on sys.meta_path, which
# would silently mask a packaging-only bug).
# ----------------------------------------------------------------------


@pytest.fixture(scope="session")
def unpacked_wheel(built_wheel, tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("wheel_unpacked")
    with zipfile.ZipFile(built_wheel) as zf:
        zf.extractall(dest)
    return dest


def test_prism_install_manifest_from_unpacked_wheel_has_real_reflect_content(
    unpacked_wheel,
):
    script = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(unpacked_wheel)!r})\n"
        "from prism_service.mcp.tools import _install_manifest\n"
        "m = _install_manifest(project_id='test')\n"
        "files = {f['path']: f for f in m['install_files']}\n"
        "out = {p: files.get(p, {}).get('content') for p in ("
        "  '.claude/agents/prism-reflect.md',"
        "  '.claude/commands/prism-reflect.md',"
        ")}\n"
        "print(json.dumps(out))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(unpacked_wheel),  # never the source tree
    )
    assert result.returncode == 0, (
        "subprocess import of _install_manifest from the UNPACKED WHEEL "
        f"failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    out = json.loads(result.stdout.strip().splitlines()[-1])
    agent_md = out.get(".claude/agents/prism-reflect.md")
    command_md = out.get(".claude/commands/prism-reflect.md")
    assert agent_md, (
        "prism_install (built from the real wheel) returned empty/absent "
        "content for .claude/agents/prism-reflect.md"
    )
    assert agent_md.strip().startswith("---"), "agent md needs frontmatter"
    assert "name: prism-reflect" in agent_md
    assert command_md, (
        "prism_install (built from the real wheel) returned empty/absent "
        "content for .claude/commands/prism-reflect.md"
    )


# ----------------------------------------------------------------------
# AC-5 — _install_manifest never ships a blank/whitespace-only entry: a
# failed-to-load asset must be REFUSED and reported additively, not
# shipped as an empty install_files content string.
# ----------------------------------------------------------------------


def test_install_manifest_refuses_blank_asset_and_reports_degraded(monkeypatch):
    import prism_service.mcp.tools as tools_mod

    monkeypatch.setattr(tools_mod, "_REFLECT_AGENT_MD", "")
    manifest = tools_mod._install_manifest(project_id="test")
    files = {f["path"]: f for f in manifest["install_files"]}

    assert ".claude/agents/prism-reflect.md" not in files, (
        "an asset whose content loaded blank must be REFUSED — never "
        "shipped as an install_files entry with empty/whitespace content"
    )
    for path, spec in files.items():
        content = spec.get("content", "")
        assert content.strip(), f"{path} shipped blank/whitespace content"

    degraded = manifest.get("degraded_assets") or []
    flat = json.dumps(degraded)
    assert (
        ".claude/agents/prism-reflect.md" in flat
        or "prism_reflect_agent.md" in flat
    ), f"expected degraded_assets to report the refused asset, got {degraded!r}"


# ----------------------------------------------------------------------
# AC-6 — _load_asset logs an ERROR (resolved absolute path + exception)
# on a load failure, still returns '', and never raises (module stays
# importable — every other test in this file importing tools_mod proves
# that at real import time; this pins the runtime failure path too).
# ----------------------------------------------------------------------


def test_load_asset_logs_error_with_resolved_path_on_missing_file(caplog):
    import prism_service.mcp.tools as tools_mod

    missing_name = "__does_not_exist_b407c930__.md"
    expected_path = (
        Path(tools_mod.__file__).resolve().parent.parent / "assets" / missing_name
    )

    with caplog.at_level(logging.ERROR, logger="prism_service.mcp.tools"):
        result = tools_mod._load_asset(missing_name)

    assert result == "", "_load_asset must still return '' on failure, never raise"
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, (
        "expected an ERROR-level log record when an asset fails to load; "
        f"got records: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    joined = " ".join(r.getMessage() for r in error_records)
    assert str(expected_path) in joined, (
        f"expected the RESOLVED ABSOLUTE PATH {expected_path} in the error "
        f"log, got: {joined!r}"
    )
