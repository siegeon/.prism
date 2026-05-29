"""Native auto-updater for the pipx-installed PRISM service (v6.0.1+).

This is the piece v6.0.0 forgot. Without it, the native install has no
way to discover or apply a new release short of `pipx reinstall` from
a git URL — which is what the user has been doing manually.

Design:

  * Single GitHub Releases call: GET /repos/<owner>/<repo>/releases/latest
    Cheap (1 req per check, no auth needed for public repos). Compares
    `tag_name` against the installed PRISM_VERSION. If a newer SemVer
    tag is present AND a .whl asset is attached, the updater proceeds.
  * Background thread polls every PRISM_AUTO_UPDATE_INTERVAL seconds
    (default 1800 = 30 min). Set to 0 to disable.
  * When a newer release is found and PRISM_AUTO_UPDATE=on (OPT-IN as of
    issue #66 — default OFF), the updater:
       1. Downloads the wheel to a temp file
       2. Calls `pip install --upgrade <wheel-path>` in the same
          interpreter (sys.executable)
       3. Sets restart_required=True and surfaces it via
          /api/update/status. It NEVER self-restarts in-place — doing
          that via os.execvp from a daemon thread dropped the live
          sockets with no traceback (#66). The new wheel takes effect on
          the next managed/manual restart.
  * Service status (current version, latest known, last check, last
    error) is exposed via /api/update-status for the SPA's
    Settings → Service card.

Honest scope limits:
  * Docker installs ignore this entirely (the running container has no
    pip to upgrade against itself, and Watchtower handles their case).
    The updater short-circuits if it detects /.dockerenv.
  * Linux/Mac get clean in-place upgrade. Windows pip-upgrade-while-
    running has historically been flaky (pip can't replace a running
    .exe); on Windows we write the sentinel and surface "restart to
    apply" in the UI instead of trying the auto-restart.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from prism_service.__version__ import PRISM_VERSION


GITHUB_REPO = os.environ.get("PRISM_UPDATE_REPO", "siegeon/.prism")
_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_USER_AGENT = f"prism-service/{PRISM_VERSION} (auto-updater)"
_POLL_INTERVAL_S = int(os.environ.get("PRISM_AUTO_UPDATE_INTERVAL", "1800"))
# Auto-apply is ON by default (restored in v6.2.7). The v6.2.4 silent
# death was the os.execvp self-restart (removed below), NOT the apply
# itself: apply now runs `pip install --upgrade` in THIS background
# daemon thread (the asyncio UI loop on the main thread keeps serving —
# subprocess.run releases the GIL while waiting) and only sets
# restart_required=True; it never replaces the live process. Native
# (Tauri) installs update via the Tauri updater bundle-swap instead, so
# the pip path there simply no-ops. Set PRISM_AUTO_UPDATE=off to opt out.
_AUTO_APPLY = os.environ.get("PRISM_AUTO_UPDATE", "on").lower() in (
    "on", "true", "1", "yes",
)
# Issue #66: NEVER self-restart in-place. _self_restart() used os.execvp
# from the auto-updater DAEMON thread, replacing the process image (same
# PID, no traceback, sockets dropped) — the exact silent-death signature
# in the report. Defer the restart on ALL platforms; the new wheel takes
# effect on the next managed/manual restart, and restart_required is
# surfaced via /api/update/status.
_DEFER_RESTART = True


def _running_in_docker() -> bool:
    """Docker installs auto-update via Watchtower (or manually). The
    in-process updater would `pip install` into a running container
    image — wrong layer. Skip entirely."""
    if Path("/.dockerenv").exists():
        return True
    # Cgroup heuristic for podman / nerdctl.
    try:
        cg = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
        return "docker" in cg or "containerd" in cg
    except OSError:
        return False


def _is_newer_semver(a: str, b: str) -> bool:
    """Return True if SemVer string `a` is strictly newer than `b`.

    Tolerant of a leading 'v' on either side. Falls back to lexical
    comparison if either string isn't a clean N.N.N — that's still
    safer than incorrectly claiming "newer" on a malformed string.
    """
    def _norm(s: str) -> tuple:
        s = s.lstrip("v").strip()
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)(.*)$", s)
        if not m:
            return (None, s)
        return (int(m.group(1)), int(m.group(2)), int(m.group(3)),
                m.group(4) or "")
    na, nb = _norm(a), _norm(b)
    if na[0] is None or nb[0] is None:
        return a > b
    return na > nb


@dataclass
class UpdateStatus:
    running_version: str = PRISM_VERSION
    latest_version: Optional[str] = None
    latest_published_at: Optional[str] = None
    update_available: bool = False
    in_docker: bool = False
    auto_apply_enabled: bool = _AUTO_APPLY
    last_check_at: float = 0.0
    last_check_ok: bool = False
    last_error: str = ""
    restart_required: bool = False
    asset_url: Optional[str] = None
    poll_interval_s: int = _POLL_INTERVAL_S


_state = UpdateStatus(in_docker=_running_in_docker())
_state_lock = threading.RLock()


def get_status() -> dict:
    with _state_lock:
        return asdict(_state)


def _fetch_latest_release() -> dict:
    """Single GET to the GitHub Releases API. No auth needed for the
    public repo. Returns the parsed release JSON."""
    req = urllib.request.Request(
        _API_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": _USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _wheel_asset_url(release: dict) -> Optional[str]:
    """Return the browser_download_url of the first .whl asset, or None."""
    for asset in release.get("assets", []) or []:
        name = asset.get("name", "")
        if name.endswith(".whl"):
            url = asset.get("browser_download_url")
            if url:
                return url
    return None


def check_for_update() -> UpdateStatus:
    """One-shot version check. Refreshes the module's _state.

    Returns the new state. Safe to call from request handlers — it
    holds a mutex but the GitHub call timeout caps wall time at 20s.
    """
    with _state_lock:
        _state.last_check_at = time.time()
    try:
        release = _fetch_latest_release()
    except urllib.error.HTTPError as e:
        with _state_lock:
            _state.last_check_ok = False
            _state.last_error = f"github releases api: HTTP {e.code}"
        return _state
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        with _state_lock:
            _state.last_check_ok = False
            _state.last_error = f"github releases api: {type(e).__name__}: {e}"
        return _state

    tag = (release.get("tag_name") or "").strip()
    published = release.get("published_at")
    asset = _wheel_asset_url(release)
    available = bool(tag) and _is_newer_semver(tag, PRISM_VERSION)

    with _state_lock:
        _state.latest_version = tag
        _state.latest_published_at = published
        _state.update_available = available
        _state.asset_url = asset if available else None
        _state.last_check_ok = True
        _state.last_error = ""
    return _state


def apply_update() -> dict:
    """Download the wheel + pip install --upgrade. Returns a result dict.

    No-op (with an explanatory message) when:
      * running in docker (Watchtower's domain)
      * no update available yet (call check_for_update first)
      * no .whl asset on the latest release

    On success, sets restart_required=True. The actual restart is the
    caller's responsibility — see daemon.request_restart() — because
    we can't safely os.execvp a uvicorn worker thread from inside it.
    """
    import sys as _sys

    with _state_lock:
        if _state.in_docker:
            return {"ok": False, "reason": "running in docker; use Watchtower"}
        if not _state.update_available:
            return {"ok": False, "reason": "no update available; run check first"}
        if not _state.asset_url:
            return {"ok": False, "reason": "latest release has no .whl asset"}
        asset = _state.asset_url
        target = _state.latest_version

    # Download to a temp dir using the asset's real basename. pip>=24
    # validates wheel filenames against PEP 427's
    # {name}-{version}-{pyver}-{abi}-{plat}.whl shape and rejects
    # anything else — so tempfile.NamedTemporaryFile(suffix=".whl")
    # produces names like "tmpe06f9m57.whl" that pip refuses with
    # "Invalid wheel filename (wrong number of parts)". Pull the
    # PEP-427-shaped basename out of the GitHub asset URL instead.
    asset_basename = posixpath.basename(urllib.parse.urlparse(asset).path)
    if not asset_basename.endswith(".whl"):
        # Last-ditch fallback: synthesize a compliant name from the tag.
        # GitHub always gives us a real basename in practice, so this
        # branch is for safety only.
        asset_basename = f"prism_service-{target}-py3-none-any.whl"
    tmpdir = Path(tempfile.mkdtemp(prefix="prism-update-"))
    wheel_path = tmpdir / asset_basename
    try:
        with urllib.request.urlopen(asset, timeout=120) as resp:
            wheel_path.write_bytes(resp.read())
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return {"ok": False, "reason": f"download failed: {type(e).__name__}: {e}"}

    # Three install paths, in order:
    #   1. python -m pip install  (the legacy path, works when pip is in the venv)
    #   2. python -m ensurepip --upgrade ; python -m pip install
    #      (recovers from venvs that shipped without pip)
    #   3. uv pip install --python <sys.executable> <wheel>
    #      (works on pipx-uv-backed venvs that intentionally exclude pip)
    # Picks the first that exits 0. Surfaces a useful error if all fail.

    def _try(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            return None

    pip_cmd = [_sys.executable, "-m", "pip", "install", "--upgrade", str(wheel_path)]
    proc = _try(pip_cmd)
    if proc is None:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return {"ok": False, "reason": "pip install timed out after 600s"}

    # If pip is missing, try ensurepip + retry.
    if proc.returncode != 0 and "No module named pip" in (proc.stderr or ""):
        bootstrap = _try([_sys.executable, "-m", "ensurepip", "--upgrade"])
        if bootstrap is not None and bootstrap.returncode == 0:
            proc = _try(pip_cmd) or proc

    # Final fallback: uv pip install --python <sys.executable> <wheel>.
    # Picks up uv from PATH (pipx-uv-backed installs always have uv available).
    if proc.returncode != 0:
        uv_path = shutil.which("uv")
        if uv_path:
            uv_cmd = [uv_path, "pip", "install", "--python", _sys.executable,
                      str(wheel_path)]
            uv_proc = _try(uv_cmd)
            if uv_proc is not None:
                proc = uv_proc

    shutil.rmtree(tmpdir, ignore_errors=True)

    if proc.returncode != 0:
        return {
            "ok": False,
            "reason": f"install exit={proc.returncode} "
                      f"(tried pip, ensurepip+pip, uv pip)",
            "stderr": (proc.stderr or "")[-1200:],
        }

    with _state_lock:
        _state.restart_required = True

    return {
        "ok": True,
        "target_version": target,
        "restart_required": True,
        "restart_auto": not _DEFER_RESTART,
    }


def _loop() -> None:
    """Background thread entry. Sleeps the configured interval between
    checks. Auto-applies when enabled."""
    if _POLL_INTERVAL_S <= 0:
        print(
            "[auto_updater] disabled (PRISM_AUTO_UPDATE_INTERVAL=0)",
            file=sys.stderr, flush=True,
        )
        return
    if _running_in_docker():
        print(
            "[auto_updater] docker detected; updates are Watchtower's job",
            file=sys.stderr, flush=True,
        )
        return
    print(
        f"[auto_updater] checking GitHub Releases every {_POLL_INTERVAL_S}s "
        f"(auto-apply={_AUTO_APPLY})",
        file=sys.stderr, flush=True,
    )
    # Eager first check on startup so the SPA can show state immediately
    # rather than waiting the full interval.
    try:
        check_for_update()
        _maybe_apply()
    except Exception as e:
        print(
            f"[auto_updater] initial check failed: {e}",
            file=sys.stderr, flush=True,
        )
    while True:
        time.sleep(_POLL_INTERVAL_S)
        try:
            check_for_update()
            _maybe_apply()
        except Exception as e:
            print(
                f"[auto_updater] sweep failed: {e}",
                file=sys.stderr, flush=True,
            )


def _maybe_apply() -> None:
    """If auto-apply is on and an update is available, apply it +
    request restart."""
    if not _AUTO_APPLY:
        return
    with _state_lock:
        if not _state.update_available:
            return
        if _state.restart_required:
            return  # already applied, just waiting for restart
        target = _state.latest_version
    print(
        f"[auto_updater] new version {target} available — applying",
        file=sys.stderr, flush=True,
    )
    result = apply_update()
    if not result.get("ok"):
        print(
            f"[auto_updater] apply failed: {result.get('reason')}",
            file=sys.stderr, flush=True,
        )
        return
    print(
        f"[auto_updater] applied {target}; restart_required=True "
        f"(restart the daemon to pick up the new wheel)",
        file=sys.stderr, flush=True,
    )
    # Issue #66: we deliberately do NOT self-restart. The previous
    # os.execvp(...) from this daemon thread replaced the process image
    # in place with no traceback and dropped the live sockets — the
    # silent-death root cause. restart_required is surfaced via
    # /api/update/status; a managed/manual restart applies the wheel.


def start_auto_updater() -> None:
    """Daemon-thread entrypoint. Wired into main.py lifespan."""
    threading.Thread(target=_loop, daemon=True, name="prism-auto-updater").start()
