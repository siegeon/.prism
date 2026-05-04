#!/usr/bin/env python3
"""PRISM SessionStart hook — keeps Brain/Graph in sync with disk.

Installed by PRISM version: 4.6.0


Walks the project source tree (respects .gitignore when git is available),
hashes each file, asks PRISM via prism_status which files have drifted,
and pushes the current content of drifted files via prism_refresh.

Installed by PRISM's prism_install / project_onboard manifest. The hook
reads its target MCP URL + project slug from .mcp.json at the project
root, so no hardcoded values live here — one hook works across projects.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

SOURCE_EXTS = {".py", ".ts", ".tsx", ".js", ".jsx", ".cs", ".go", ".rs",
               ".java", ".rb", ".php", ".cpp", ".c", ".h", ".hpp",
               ".md", ".yml", ".yaml", ".toml"}
SKIP_PARTS = {".git", "node_modules", "__pycache__", ".venv", "venv",
              "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
              ".next", ".nuxt", "target", ".claude", ".prism"}
MAX_FILE_BYTES = 300_000


def _project_root() -> Path:
    # Walk up from cwd looking for .mcp.json
    cur = Path.cwd()
    for d in [cur, *cur.parents]:
        if (d / ".mcp.json").exists():
            return d
    return cur


def _mcp_url_and_project(root: Path) -> tuple[str, str] | None:
    cfg = root / ".mcp.json"
    if not cfg.exists():
        return None
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        return None
    servers = (data.get("mcpServers") or {}).values()
    for s in servers:
        url = s.get("url", "")
        if "/mcp" in url:
            # Split out ?project= query
            if "project=" in url:
                base, q = url.split("?", 1)
                project = [p.split("=", 1)[1] for p in q.split("&")
                           if p.startswith("project=")][0]
                return base.rstrip("/"), project
    return None


def _mcp_call(base: str, project: str, tool: str, args: dict) -> dict:
    url = f"{base}/?project={project}"
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read().decode()
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            for line in raw.splitlines():
                if line.startswith("data: "):
                    return json.loads(line[6:])
        return json.loads(raw)


def _parse_result(resp: dict):
    content = resp.get("result", {}).get("content", [])
    if not content:
        return None
    text = content[0].get("text", "")
    try:
        return json.loads(text)
    except Exception:
        return text


def _git_tracked(root: Path) -> set[str] | None:
    """Return set of git-tracked relative paths, or None if no git repo."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True, text=True, timeout=15, check=True,
        ).stdout
        return {line.strip() for line in out.splitlines() if line.strip()}
    except Exception:
        return None


def _should_skip(path: Path, root: Path) -> bool:
    rel_parts = path.relative_to(root).parts
    if any(p in SKIP_PARTS for p in rel_parts):
        return True
    if path.suffix not in SOURCE_EXTS:
        return True
    try:
        sz = path.stat().st_size
    except OSError:
        return True
    if sz == 0 or sz > MAX_FILE_BYTES:
        return True
    return False


def _hash_file(p: Path) -> str | None:
    """Hash the TEXT form (newline-normalized utf-8) so hashes match
    what the server stores — avoids spurious CRLF-vs-LF drift on Windows."""
    try:
        text = p.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _collect(root: Path) -> dict[str, tuple[str, Path]]:
    """Return {rel_path: (sha256, abs_path)} for source files under root."""
    out: dict[str, tuple[str, Path]] = {}
    tracked = _git_tracked(root)
    if tracked:
        for rel in tracked:
            p = root / rel
            if not p.is_file() or _should_skip(p, root):
                continue
            sha = _hash_file(p)
            if sha:
                out[rel.replace("\\", "/")] = (sha, p)
    else:
        for p in root.rglob("*"):
            if not p.is_file() or _should_skip(p, root):
                continue
            sha = _hash_file(p)
            if sha:
                out[p.relative_to(root).as_posix()] = (sha, p)
    return out


def main() -> int:
    root = _project_root()
    cfg = _mcp_url_and_project(root)
    if cfg is None:
        # No .mcp.json — user hasn't opted in. Silent skip.
        return 0
    base, project = cfg

    files = _collect(root)
    if not files:
        return 0

    hashes = {path: sha for path, (sha, _) in files.items()}
    try:
        resp = _mcp_call(base, project, "prism_status",
                         {"file_hashes": hashes})
    except Exception as e:
        print(f"[prism-sync] could not reach {base} ({e!r}); skipping",
              file=sys.stderr)
        return 0

    status = _parse_result(resp) or {}
    version = status.get("prism_version") or "?"
    print(
        f"[prism-sync] PRISM v{version} loaded for project '{project}'",
        file=sys.stderr,
    )
    drifted = status.get("drifted", []) or []
    if not drifted:
        # Always surface the version to Claude, even when there's no drift —
        # so the agent's first turn knows which PRISM build is live.
        print(json.dumps({
            "hookSpecificOutput": {
                "additionalContext": (
                    f"PRISM v{version} active for project '{project}'."
                ),
            },
        }))
        return 0

    # Re-ingest drifted files
    to_refresh: dict[str, str] = {}
    for entry in drifted:
        path = entry.get("path")
        if not path:
            continue
        fe = files.get(path)
        if not fe:
            continue
        try:
            to_refresh[path] = fe[1].read_text(encoding="utf-8")
        except Exception:
            pass
    if not to_refresh:
        return 0

    # Chunked refresh: push files in batches of CHUNK_SIZE with
    # skip_graph=true, then fire one graph_rebuild at the end. Avoids
    # the per-call graphify cost that dominates latency on larger syncs.
    CHUNK_SIZE = 25
    items = list(to_refresh.items())
    refreshed = 0
    for i in range(0, len(items), CHUNK_SIZE):
        batch = dict(items[i:i + CHUNK_SIZE])
        try:
            _mcp_call(
                base, project, "prism_refresh",
                {"files": batch, "skip_graph": True},
            )
            refreshed += len(batch)
        except Exception as e:
            print(
                f"[prism-sync] prism_refresh chunk {i // CHUNK_SIZE} "
                f"failed: {e!r}", file=sys.stderr,
            )
    if refreshed:
        try:
            _mcp_call(base, project, "graph_rebuild", {})
        except Exception as e:
            print(
                f"[prism-sync] graph_rebuild after sync failed: {e!r}",
                file=sys.stderr,
            )
        print(
            f"[prism-sync] refreshed {refreshed} drifted file(s) in "
            f"{(len(items) + CHUNK_SIZE - 1) // CHUNK_SIZE} chunk(s) + "
            "1 graph_rebuild",
            file=sys.stderr,
        )

    # LL-10: SessionStart reflection check. If a consolidation
    # candidate is ready, emit hookSpecificOutput.additionalContext so
    # Claude sees the brief on its first turn and can delegate to the
    # prism-reflect sub-agent. Silent no-op when nothing is pending.
    # SessionStart hooks receive a small JSON payload on stdin; extract
    # session_id so janitor_check can rate-limit and so the emitted
    # additionalContext can be linked to this session.
    session_id = ""
    try:
        import json as _json
        import sys as _sys
        session_id = (
            _json.loads(_sys.stdin.read() or "{}").get("session_id", "")
        )
    except Exception:
        pass
    if session_id:
        try:
            chk_resp = _mcp_call(
                base, project, "janitor_check", {"session_id": session_id},
            )
            payload = _parse_result(chk_resp) or {}
            if payload.get("ready") and payload.get("brief"):
                brief = payload["brief"]
                additional = (
                    f"PRISM reflection pending: candidate "
                    f"{brief.get('candidate_id', '?')}. Spawn the "
                    f"`prism-reflect` subagent using the brief below — "
                    f"call `janitor_check` if you need the live version, "
                    f"submit via `janitor_submit`. Brief: "
                    f"{json.dumps(brief)[:6000]}"
                )
                print(json.dumps({
                    "hookSpecificOutput": {
                        "additionalContext": additional,
                    },
                }))
        except Exception as e:
            print(
                f"[prism-sync] janitor_check failed: {e!r}",
                file=sys.stderr,
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
