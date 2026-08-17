#!/usr/bin/env python3
"""PostToolUse hook — implicit retrieval feedback signal.

When Claude calls mcp__prism__brain_search and subsequently Read/Edit one
of the returned source_files within a short window, this hook auto-emits
a brain_search_feedback 'up' signal. Turns observability into training
signal without requiring Claude to self-rate.

Advisory only: always exits 0, silent on any error, never blocks the
tool call.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

_BUFFER_REL = ".prism/feedback-buffer.jsonl"
_WINDOW_SECS = 600  # 10-minute correlation window
_MAX_ENTRIES = 50   # per-session buffer cap

# The remaining implementation is appended below via sequential edits
# so each chunk stays within the project's 30-line write limit.
def _project_root() -> Path:
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
    for s in (data.get("mcpServers") or {}).values():
        url = s.get("url", "")
        if "/mcp" in url and "project=" in url:
            base, q = url.split("?", 1)
            project = [p.split("=", 1)[1] for p in q.split("&")
                       if p.startswith("project=")][0]
            return base.rstrip("/"), project
    return None


_ALIVE_CACHE_REL = ".prism/.mcp-alive.json"
_ALIVE_TTL_S = 120.0


def _resolve_live_mcp(root: Path) -> "tuple[str, str] | None":
    """(base, project) for a REACHABLE MCP daemon, or None.

    .mcp.json names the release port (7777); on a dev box that daemon is
    often absent, and every hook paid a ~2s connection-refused wait per
    _mcp_call against it — measured as the whole 2276ms median per-Edit
    hook cost (task 86fac34e). Probe candidates with a 300ms TCP connect
    (env PRISM_HOOK_MCP_URL first, then .mcp.json, then the dev-port
    swap 7777->8887) and cache the verdict — ALIVE or DEAD — for 120s,
    so an absent daemon costs one probe per 2 minutes, not 2s per edit."""
    import os
    import socket
    from urllib.parse import urlsplit

    conn = _mcp_url_and_project(root)
    if conn is None:
        return None
    base, project = conn
    cands = [base]
    if ":7777" in base:
        cands.append(base.replace(":7777", ":8887"))
    env = os.environ.get("PRISM_HOOK_MCP_URL", "").strip().rstrip("/")
    if env:
        cands.insert(0, env)

    cache = root / _ALIVE_CACHE_REL
    now = time.time()
    try:
        c = json.loads(cache.read_text(encoding="utf-8"))
        if now - float(c.get("ts", 0)) < _ALIVE_TTL_S and c.get("cands") == cands:
            b = c.get("base")
            return (b, project) if b else None
    except Exception:
        pass

    live = None
    for cand in cands:
        try:
            u = urlsplit(cand)
            port = u.port or (443 if u.scheme == "https" else 80)
            with socket.create_connection((u.hostname, port), timeout=0.3):
                live = cand
                break
        except Exception:
            continue
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"base": live, "ts": now, "cands": cands}),
                         encoding="utf-8")
    except Exception:
        pass
    return (live, project) if live else None


def _buffer_path(root: Path) -> Path:
    return root / _BUFFER_REL


def _load_buffer(root: Path) -> list[dict]:
    p = _buffer_path(root)
    if not p.exists():
        return []
    out: list[dict] = []
    now = time.time()
    try:
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                row = json.loads(ln)
            except Exception:
                continue
            if now - float(row.get("ts", 0)) <= _WINDOW_SECS:
                out.append(row)
    except Exception:
        return []
    return out[-_MAX_ENTRIES:]


def _save_buffer(root: Path, rows: list[dict]) -> None:
    p = _buffer_path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8") as fh:
            for r in rows[-_MAX_ENTRIES:]:
                fh.write(json.dumps(r) + "\n")
    except Exception:
        pass


def _mcp_call(base: str, project: str, tool: str, args: dict) -> None:
    url = f"{base}/?project={project}"
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=4).read()
    except Exception as e:
        try:
            from hook_logger import log_hook_failure
            log_hook_failure(f"mcp_call:{tool}", e)
        except Exception:
            pass


def _parse_search_response(tool_response) -> list[dict]:
    """Extract (search_id, doc_id, source_file) tuples from a brain_search
    MCP response payload. The response format is a list of TextContent
    items; the first item's .text is a JSON list of result dicts."""
    try:
        content = (tool_response or {}).get("content") or []
        if not content:
            return []
        txt = content[0].get("text") or ""
        results = json.loads(txt)
        if not isinstance(results, list):
            return []
        out = []
        for r in results:
            if not isinstance(r, dict):
                continue
            sid = r.get("search_id")
            did = r.get("doc_id")
            sf = r.get("source_file")
            if sid and did:
                out.append({"search_id": int(sid), "doc_id": did,
                            "source_file": sf or ""})
        return out
    except Exception:
        return []


def _handle_search(root: Path, tool_response) -> None:
    hits = _parse_search_response(tool_response)
    if not hits:
        return
    buf = _load_buffer(root)
    now = time.time()
    for h in hits:
        buf.append({**h, "ts": now, "emitted": False})
    _save_buffer(root, buf)


def _handle_read_or_edit(root: Path, tool_input: dict,
                         signal: str = "up",
                         conn: "tuple[str, str] | None" = None) -> None:
    fp = (tool_input or {}).get("file_path") or ""
    if not fp:
        return
    buf = _load_buffer(root)
    if conn is None:
        conn = _resolve_live_mcp(root)
    if conn is None:
        return
    base, project = conn
    # Normalise: allow match on trailing segment too.
    fp_norm = str(fp).replace("\\", "/")
    for row in buf:
        if row.get("emitted"):
            continue
        sf = (row.get("source_file") or "").replace("\\", "/")
        if not sf:
            continue
        if sf == fp_norm or fp_norm.endswith("/" + sf) or sf.endswith(fp_norm):
            _mcp_call(base, project, "brain_search_feedback", {
                "search_id": int(row["search_id"]),
                "doc_id": row["doc_id"],
                "signal": signal,
                "note": f"implicit: {signal} from tool use",
            })
            row["emitted"] = True
    _save_buffer(root, buf)


def main() -> None:
    try:
        raw = sys.stdin.read()
        if not raw:
            return
        data = json.loads(raw)
    except Exception:
        return
    tool_name = data.get("tool_name") or ""
    tool_input = data.get("tool_input") or {}
    tool_response = data.get("tool_response") or {}
    root = _project_root()
    # ONE daemon probe (cached 120s) shared by every handler this dispatch
    # runs — never a per-call 2s connection-refused wait (task 86fac34e).
    conn = _resolve_live_mcp(root)

    if tool_name.endswith("brain_search"):
        _handle_search(root, tool_response)
    elif tool_name in ("Read", "Edit", "Write"):
        if conn is not None:
            _handle_read_or_edit(root, tool_input, signal="up", conn=conn)

    # Merged PostToolUse dispatch (task 86fac34e): Edit/Write/NotebookEdit
    # also feed edit-learn IN THIS PROCESS — .claude/settings.json used to
    # register a second hook entry for it, so a single Edit paid two cold
    # python starts (and, with the release daemon absent, two dead-daemon
    # waits). Same handler, same side effects, one interpreter.
    if tool_name in ("Edit", "Write", "NotebookEdit") and conn is not None:
        try:
            import importlib.util
            _el_path = Path(__file__).resolve().parent / "prism-edit-learn.py"
            spec = importlib.util.spec_from_file_location(
                "prism_edit_learn_hook", _el_path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.handle(data, conn)
        except Exception:
            pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
