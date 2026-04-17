#!/usr/bin/env python3
"""PreToolUse hook — token-aware read guard.

Fires before Read tool calls. Advisory only (never blocks).
- Tracks files already read this session
- Warns on re-reads with token estimate
- Suggests Brain search as alternative for unfamiliar files

Session state stored in /tmp/.prism-session-reads.json
"""

import io
import json
import os
import sys
import time
from pathlib import Path

# Fix Windows console encoding
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Session read tracker — survives across hook invocations within a session
_SESSION_FILE = Path(os.environ.get("TEMP", "/tmp")) / ".prism-session-reads.json"
_SESSION_TTL = 3600 * 4  # 4 hours — assume new session after this


def _load_session() -> dict:
    """Load session read tracker, resetting if stale."""
    try:
        if _SESSION_FILE.exists():
            data = json.loads(_SESSION_FILE.read_text(encoding="utf-8"))
            if time.time() - data.get("started", 0) < _SESSION_TTL:
                return data
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return {"started": time.time(), "reads": {}}


def _save_session(session: dict) -> None:
    try:
        _SESSION_FILE.write_text(json.dumps(session), encoding="utf-8")
    except OSError:
        pass


def _estimate_tokens(file_path: str) -> int | None:
    """Estimate token count from file size (bytes / 4 heuristic)."""
    try:
        size = os.path.getsize(file_path)
        return size // 4
    except OSError:
        return None


def main():
    file_path = os.environ.get("TOOL_PARAMS_file_path", "")
    if not file_path:
        sys.exit(0)

    # Normalize path for consistent tracking
    try:
        file_path = str(Path(file_path).resolve())
    except (OSError, ValueError):
        sys.exit(0)

    session = _load_session()
    reads = session.get("reads", {})

    token_est = _estimate_tokens(file_path)
    token_note = f" (~{token_est} tokens)" if token_est else ""

    if file_path in reads:
        prev = reads[file_path]
        count = prev.get("count", 1)
        ago_seconds = int(time.time() - prev.get("last", time.time()))

        if ago_seconds < 60:
            ago_str = f"{ago_seconds}s ago"
        else:
            ago_str = f"{ago_seconds // 60}m ago"

        # Warn on re-read (advisory — exit 0, not exit 2)
        short_path = os.path.basename(file_path)
        print(
            f"Re-read #{count + 1} of {short_path}{token_note} "
            f"(last read {ago_str}). "
            f"Consider using Brain search if you need specific info.",
            file=sys.stderr,
        )

        reads[file_path] = {
            "count": count + 1,
            "first": prev.get("first", time.time()),
            "last": time.time(),
        }
    else:
        # First read — just record it, no warning
        reads[file_path] = {
            "count": 1,
            "first": time.time(),
            "last": time.time(),
        }

    session["reads"] = reads
    _save_session(session)

    # Always allow the read (exit 0)
    sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Never block on hook failure
        sys.exit(0)
