"""Split-brain detection + healing for the PRISM daemon (GH #181).

The healthy invariant is ONE process owning BOTH service ports: UI (7778) on
the main-thread uvicorn and MCP (7777) on a daemon-thread uvicorn. A botched
auto-update restart can leave TWO daemon families, each owning ONE port (new
UI on :7778, orphaned old worker squatting :7777) — a silent split-brain that
`prism status` / `/api/version` report as green while every MCP caller is
stranded on the stale daemon.

This module is the detective + the medic, deliberately stdlib-only and OS-tool
based (no psutil dep): map each port to its owning pid, decide whether that is
a split-brain, and reap the orphan with a SIGTERM->SIGKILL escalation (the
#181 orphan ignored SIGTERM). Every lookup is best-effort and returns None on
any failure so a detection NEVER raises a FALSE split-brain (NFR-1).
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Callable, Optional


def port_owner_pid(port: int | str) -> Optional[int]:
    """Best-effort pid owning the LISTEN socket on `port`, else None.

    Windows: `netstat -ano`. POSIX: `ss -ltnp`, then `lsof` fallback. Returns
    None when undeterminable (never raises) so callers never see a FALSE
    split-brain (NFR-1)."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    if port <= 0:
        return None
    try:
        if sys.platform.startswith("win"):
            return _win_port_owner(port)
        return _posix_port_owner(port)
    except (OSError, ValueError):
        return None


def _win_port_owner(port: int) -> Optional[int]:
    out = subprocess.run(
        ["netstat", "-ano", "-p", "TCP"],
        capture_output=True, text=True, check=False,
    )
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        # proto  local-addr  foreign-addr  state  pid
        if len(parts) >= 5 and parts[0].upper().startswith("TCP") \
                and parts[3].upper() == "LISTENING" \
                and parts[1].rsplit(":", 1)[-1] == str(port):
            try:
                return int(parts[4])
            except ValueError:
                continue
    return None


def _posix_port_owner(port: int) -> Optional[int]:
    ss = subprocess.run(
        ["ss", "-ltnp"], capture_output=True, text=True, check=False,
    )
    if ss.returncode == 0 and ss.stdout:
        for line in ss.stdout.splitlines():
            if "LISTEN" not in line:
                continue
            cols = line.split()
            # ... State Recv-Q Send-Q Local-Address:Port ...  users:(("p",pid=N,..))
            local = cols[3] if len(cols) >= 4 else ""
            if local.rsplit(":", 1)[-1] != str(port):
                continue
            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
    lsof = subprocess.run(
        ["lsof", f"-iTCP:{port}", "-sTCP:LISTEN", "-t", "-n", "-P"],
        capture_output=True, text=True, check=False,
    )
    if lsof.returncode == 0 and lsof.stdout.strip():
        try:
            return int(lsof.stdout.split()[0])
        except (ValueError, IndexError):
            return None
    return None


def _pid_alive(pid: int) -> bool:
    """True iff PID names a live process. Mirrors cli._is_alive /
    supervisor.server_process_alive (biases True on PermissionError)."""
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, check=False,
        )
        import csv
        for row in csv.reader((out.stdout or "").splitlines()):
            if len(row) >= 2 and row[1].strip() == str(pid):
                return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


_TASKKILL = "TASKKILL"
# signal.SIGKILL is undefined on Windows; resolve via getattr so this module
# stays importable + unit-testable (the POSIX branch can be exercised with
# _win=False) on any host. Only the POSIX runtime path actually delivers these.
_SIGTERM = getattr(signal, "SIGTERM", 15)
_SIGKILL = getattr(signal, "SIGKILL", 9)


def _default_signal(pid: int, sig) -> None:
    """Deliver `sig` to `pid`. The sentinel _TASKKILL maps to a Windows hard
    tree-kill (no POSIX-signal equivalent); everything else is os.kill."""
    if sig == _TASKKILL:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            check=False, capture_output=True,
        )
        return
    os.kill(pid, sig)


def kill_pid_escalating(
    pid: int,
    term_timeout_s: float = 3.0,
    *,
    _kill: Optional[Callable] = None,
    _alive: Optional[Callable[[int], bool]] = None,
    _sleep: Callable[[float], None] = time.sleep,
    _win: Optional[bool] = None,
) -> list:
    """SIGTERM, then SIGKILL after term_timeout_s if it survives — the orphan in
    #181 IGNORED SIGTERM, so graceful-stop alone left a port-squatting zombie;
    we ALWAYS escalate. Windows has no SIGTERM, so it is a single `taskkill /F`.
    Returns the list of signals actually sent (test-observable). The injectable
    seams keep the escalation logic unit-testable on any host."""
    if pid <= 0:
        return []
    win = sys.platform.startswith("win") if _win is None else _win
    kill = _kill or _default_signal
    alive = _alive or _pid_alive
    sent: list = []
    if win:
        kill(pid, _TASKKILL)
        sent.append(_TASKKILL)
        return sent
    try:
        kill(pid, _SIGTERM)
        sent.append(_SIGTERM)
    except ProcessLookupError:
        return sent
    deadline = time.monotonic() + max(0.0, term_timeout_s)
    while time.monotonic() < deadline:
        if not alive(pid):
            return sent
        _sleep(0.05)
    try:
        kill(pid, _SIGKILL)
        sent.append(_SIGKILL)
    except ProcessLookupError:
        pass
    return sent


@dataclass
class SplitBrainReport:
    healthy: bool
    ui_port: int
    mcp_port: int
    ui_pid: Optional[int]
    mcp_pid: Optional[int]
    pidfile_pid: Optional[int]
    orphan_pid: Optional[int]
    reason: str


def detect_split_brain(
    ui_port: int | str,
    mcp_port: int | str,
    pidfile_pid: Optional[int] = None,
    owner: Optional[Callable] = None,
) -> SplitBrainReport:
    """A split-brain is two DISTINCT pids each owning one of the two ports.

    Conservative by design (NFR-1): a port owned by no one / undeterminable, or
    a single pid owning BOTH ports, is reported HEALTHY — only two different
    live owners trip it, so a down or mid-boot daemon never false-positives.
    The canonical keeper is the pidfile pid when it owns a port, else the UI
    owner (the live /api/version responder); the OTHER pid is the orphan."""
    _owner = owner or port_owner_pid
    ui_pid = _owner(ui_port)
    mcp_pid = _owner(mcp_port)

    def _mk(healthy, orphan, reason):
        return SplitBrainReport(
            healthy=healthy, ui_port=int(ui_port), mcp_port=int(mcp_port),
            ui_pid=ui_pid, mcp_pid=mcp_pid, pidfile_pid=pidfile_pid,
            orphan_pid=orphan, reason=reason,
        )

    if ui_pid is None or mcp_pid is None:
        return _mk(True, None, "ports unowned/undeterminable — no split detected")
    if ui_pid == mcp_pid:
        return _mk(True, None, f"single owner pid {ui_pid} holds both ports")
    # Two distinct owners — split-brain.
    if pidfile_pid == mcp_pid:
        keeper, orphan = mcp_pid, ui_pid
    else:
        keeper, orphan = ui_pid, mcp_pid
    return _mk(
        False, orphan,
        f"SPLIT-BRAIN: UI :{ui_port} owned by pid {ui_pid}, MCP :{mcp_port} "
        f"owned by pid {mcp_pid} (keep {keeper}, orphan {orphan})",
    )


def heal_split_brain(
    report: SplitBrainReport,
    term_timeout_s: float = 3.0,
    killer: Optional[Callable] = None,
) -> Optional[int]:
    """Reap the orphan pid (escalating SIGTERM->SIGKILL). No-op when healthy.
    Returns the reaped pid, or None."""
    if report.healthy or not report.orphan_pid:
        return None
    (killer or kill_pid_escalating)(report.orphan_pid, term_timeout_s)
    return report.orphan_pid
