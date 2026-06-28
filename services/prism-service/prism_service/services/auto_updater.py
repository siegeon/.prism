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
       3. Sets restart_required=True AND *requests* a restart of the
          live process so the served version actually flips (task
          bc9b1a88, 5th recurrence). The request/perform seam keeps the
          #66 guard intact: the daemon thread only sets a flag
          (request_restart) — it NEVER calls os.execv/os.execvp itself.
          The MAIN thread (which owns uvicorn) observes the flag,
          gracefully stops the server (drains the loop / closes
          sockets), and only THEN os.execv's a fresh process image
          (perform_restart). #66's silent death was an in-thread execvp
          from this daemon thread dropping live sockets with no
          traceback — that path stays gone.
  * Service status (current version, latest known, last check, last
    error) is exposed via /api/update-status for the SPA's
    Settings → Service card.

Honest scope limits:
  * Docker installs ignore this entirely (the running container has no
    pip to upgrade against itself, and Watchtower handles their case).
    The updater short-circuits if it detects /.dockerenv.
  * The restart is supervisor-agnostic: the main-thread os.execv
    re-execs in place under a bare `prism start --daemon` (the pidfile
    manager does not respawn), and a clean exit lets an external
    supervisor respawn — both land on the new version.
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
# Issue #66: NEVER os.execvp/os.execv from THIS daemon thread. The old
# _self_restart() did exactly that, replacing the process image in place
# (same PID, no traceback, sockets dropped) — the silent-death signature.
# The safe seam (task bc9b1a88): the daemon thread only REQUESTS a restart
# (request_restart -> sets _restart_requested), and the MAIN thread that
# owns uvicorn observes it, gracefully stops the server, and only THEN
# calls perform_restart() (os.execv). So the restart is no longer deferred
# forever on all platforms — it is handed to the main thread.
_DEFER_RESTART = False

# Main-thread restart handoff. The daemon thread sets this flag after a
# successful apply; the uvicorn main thread polls restart_requested(),
# drains + stops the server, then calls perform_restart(). _restart_lock
# guards the flag; never call os.execv off the main thread.
_restart_requested_flag = False
_restart_lock = threading.RLock()


def request_restart() -> None:
    """Daemon-thread-safe: ASK the main thread to restart the process.

    This NEVER execs — it only flips a flag. The main thread polls
    restart_requested() and performs the real os.execv via
    perform_restart(). Keeping the exec off this thread is the #66 guard.
    """
    global _restart_requested_flag
    with _restart_lock:
        _restart_requested_flag = True


def restart_requested() -> bool:
    """True once a successful apply has requested a main-thread restart."""
    with _restart_lock:
        return _restart_requested_flag


def clear_restart_request() -> None:
    """Reset the restart request (used by tests and after a perform)."""
    global _restart_requested_flag
    with _restart_lock:
        _restart_requested_flag = False


def perform_restart(server=None, drain_timeout_s: float = 10.0) -> None:
    """Re-exec the current process image. MAIN-THREAD ONLY.

    Drains the running uvicorn server FIRST (close sockets, #66), then
    re-execs `sys.executable -m prism_service.main` so the freshly-imported
    interpreter picks up the upgraded wheel and the served version flips.

    If `server` is passed, it is gracefully signalled to stop before the
    exec — `server.should_exit = True` (uvicorn drains its loop) and, if a
    `stop()` method exists, that is invoked too. When main.py already ran
    the server to completion before calling this, pass server=None.

    Supervisor-agnostic: a bare daemon re-execs in place; under an external
    supervisor the clean exit triggers a respawn. Guarded by _DEFER_RESTART
    so an operator can hard-disable the exec.
    """
    if _DEFER_RESTART:
        return
    # SPLIT-BRAIN GUARD (GH #181): mark a restart in progress BEFORE we drain.
    # The os.execv below re-execs IN PLACE (same pid) and closes both CLOEXEC
    # sockets, so the re-exec'd image is the single owner of both ports. The
    # risk is the out-of-process supervisor: it probes ONLY the UI port, sees it
    # hang during the drain + cold-boot, and fires a COMPETING kill+respawn that
    # stacks a second family. While this FRESH sentinel exists the supervisor
    # defers recovery; the re-exec'd server clears it on a healthy boot
    # (main._own_pidfile_write path). TTL-bounded so a crash can't wedge
    # recovery off forever (NFR-2).
    try:
        from prism_service.data_dir import write_restart_sentinel
        write_restart_sentinel(os.getpid())
    except Exception:
        pass
    if server is not None:
        # BOUNDED graceful-first drain (task 73ec7273, lineage of #66):
        # 1) should_exit -> uvicorn drains its loop + closes idle sockets;
        # 2) if a held-open MCP streamable-HTTP connection refuses to close,
        #    after drain_timeout_s set force_exit so uvicorn DROPS it and
        #    .run() can return. Graceful-first then force-after-timeout means
        #    in-flight Brain writes / task mutations get the drain window
        #    before any force-kill (likely_misfire d). We never gate the exec
        #    on a clean drain — the timeout is the ceiling.
        try:
            server.should_exit = True
        except Exception:
            pass
        stop = getattr(server, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass
        # Honor drain_timeout_s: give the graceful path that long, then force.
        deadline = time.monotonic() + max(0.0, float(drain_timeout_s))
        while time.monotonic() < deadline:
            if getattr(server, "force_exit", False):
                break
            time.sleep(0.05)
        try:
            server.force_exit = True
        except Exception:
            pass
    argv = [sys.executable, "-m", "prism_service.main"]
    os.execv(sys.executable, argv)


def _dev_mode() -> bool:
    """True when this is a source-run dev instance (PRISM_DEV_MODE truthy).

    Mirrors api/version._dev_mode env semantics: 1/true/yes/on are truthy
    (case-insensitive); everything else is false. A dev instance is the
    editable pip checkout — pip-installing a release wheel over it would
    shadow the source and drop live sockets, so the apply/pip path is gated
    on this (the version CHECK still runs). Read at CALL time, not import,
    so tests / the launcher can set it after the module loads."""
    return os.environ.get("PRISM_DEV_MODE", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _auto_update_forced_on() -> bool:
    """True only when PRISM_AUTO_UPDATE is EXPLICITLY set to a truthy value.

    This is the dev-mode escape hatch: PRISM_DEV_MODE alone defaults apply
    OFF, but an explicit PRISM_AUTO_UPDATE=on force-enables apply even in
    dev (for testing the install path). Unset => not forced."""
    raw = os.environ.get("PRISM_AUTO_UPDATE")
    if raw is None:
        return False
    return raw.strip().lower() in ("on", "true", "1", "yes")


def _dev_mode_blocks_apply() -> bool:
    """Apply is blocked when we're in dev mode AND the user has not
    explicitly forced PRISM_AUTO_UPDATE=on."""
    return _dev_mode() and not _auto_update_forced_on()


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

    On success, sets restart_required=True. It does NOT exec here — the
    actual restart is performed from the MAIN thread (see
    request_restart()/perform_restart()), because os.execv off a daemon
    thread drops live sockets with no traceback (#66). _maybe_apply()
    calls request_restart() after this returns ok.
    """
    import sys as _sys

    if _dev_mode_blocks_apply():
        # Source-run dev instance: pip-installing the release wheel over the
        # editable checkout shadows the source and drops live sockets. Block
        # the apply path exactly like docker; the version CHECK still runs.
        print(
            "[auto_updater] dev mode — auto-apply disabled "
            "(set PRISM_AUTO_UPDATE=on to force-enable in dev)",
            file=_sys.stderr, flush=True,
        )
        return {"ok": False, "reason": "dev mode — auto-apply disabled "
                "(PRISM_DEV_MODE set; export PRISM_AUTO_UPDATE=on to force)"}

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
    if _dev_mode_blocks_apply():
        print(
            "[auto_updater] dev mode — auto-apply disabled "
            "(set PRISM_AUTO_UPDATE=on to force-enable in dev)",
            file=sys.stderr, flush=True,
        )
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
        f"[auto_updater] applied {target}; requesting main-thread restart "
        f"to flip the served version",
        file=sys.stderr, flush=True,
    )
    # Issue #66 guard: do NOT exec from THIS daemon thread (that dropped
    # live sockets with no traceback). Only REQUEST the restart; the
    # uvicorn main thread observes restart_requested(), gracefully stops
    # the server, and then calls perform_restart() (os.execv) once the
    # sockets are closed. This is what finally flips the served version.
    request_restart()


def start_auto_updater() -> None:
    """Daemon-thread entrypoint. Wired into main.py lifespan."""
    threading.Thread(target=_loop, daemon=True, name="prism-auto-updater").start()
