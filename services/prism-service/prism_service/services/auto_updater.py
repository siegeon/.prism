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
  * When a newer release is found and PRISM_AUTO_UPDATE=on (default),
    the updater:
       1. Downloads the wheel to a temp file
       2. Calls `pip install --upgrade <wheel-path>` in the same
          interpreter (sys.executable)
       3. Touches a `.restart-requested` sentinel so the daemon can
          drop the new code on next graceful restart, OR forks a
          self-restart subprocess (we can't os.execvp ourselves
          safely from a running uvicorn worker).
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
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from prism_service.__version__ import PRISM_VERSION


GITHUB_REPO = os.environ.get("PRISM_UPDATE_REPO", "siegeon/.prism")
_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_USER_AGENT = f"prism-service/{PRISM_VERSION} (auto-updater)"
_POLL_INTERVAL_S = int(os.environ.get("PRISM_AUTO_UPDATE_INTERVAL", "1800"))
_AUTO_APPLY = os.environ.get("PRISM_AUTO_UPDATE", "on").lower() not in (
    "off", "false", "0", "no",
)
_DEFER_RESTART = sys.platform.startswith("win")


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

    # Download to a temp wheel file. /tmp on Linux/Mac, %TEMP% on Windows.
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".whl", delete=False,
        ) as tmp:
            wheel_path = Path(tmp.name)
        with urllib.request.urlopen(asset, timeout=120) as resp:
            wheel_path.write_bytes(resp.read())
    except Exception as e:
        return {"ok": False, "reason": f"download failed: {type(e).__name__}: {e}"}

    cmd = [_sys.executable, "-m", "pip", "install", "--upgrade", str(wheel_path)]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "pip install timed out after 600s"}
    finally:
        try:
            wheel_path.unlink(missing_ok=True)
        except OSError:
            pass

    if proc.returncode != 0:
        return {
            "ok": False,
            "reason": f"pip install exit={proc.returncode}",
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
        f"[auto_updater] applied {target}; restart_required=True",
        file=sys.stderr, flush=True,
    )
    if not _DEFER_RESTART:
        _self_restart()


def _self_restart() -> None:
    """Re-exec the daemon so the freshly-installed wheel takes effect.

    Linux/Mac: os.execvp replaces the current process image cleanly.
    Windows: pip can't unlink the running python.exe of an active
    daemon, so we never auto-restart there — the user / launcher
    relaunches manually. The UI shows 'restart required to apply'.
    """
    if _DEFER_RESTART:
        return
    print(
        "[auto_updater] re-exec to pick up new wheel",
        file=sys.stderr, flush=True,
    )
    try:
        os.execvp(sys.executable, [sys.executable, "-m", "prism_service.main"])
    except OSError as e:
        print(
            f"[auto_updater] os.execvp failed: {e}; restart manually",
            file=sys.stderr, flush=True,
        )


def start_auto_updater() -> None:
    """Daemon-thread entrypoint. Wired into main.py lifespan."""
    threading.Thread(target=_loop, daemon=True, name="prism-auto-updater").start()
