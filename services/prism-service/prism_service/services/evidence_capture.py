"""Pure capture logic for attaching real, observable evidence to a gate's
completion_proof: a browser walkthrough (screenshot/video), the verbatim
source of the test that pins an acceptance criterion, and provenance
metadata. Deliberately kept out of policy files (verifier_service.py /
oracle_spec.py) -- this module has no say over gate decisions, it only
produces artifacts a trusted runner can attach.

Graceful degradation is the core contract for capture_walkthrough: capture
is opt-in proof, never a crash surface for the receipt mint.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def capture_walkthrough(
    url: str,
    out_dir: str,
    selector: Optional[str] = None,
    video: bool = True,
    now=None,
) -> dict:
    """Best-effort browser walkthrough capture. Launches chromium headless via
    Playwright's sync API, navigates to ``url``, waits for ``selector`` (or
    load state) and takes a full-page screenshot. When ``video`` is true the
    context records video into ``out_dir``. ANY failure -- missing
    playwright, browser launch failure, unreachable app, timeout -- is
    caught and returned as an error dict; this function never raises.
    """
    result = {"screenshot": None, "video": None, "error": None}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # pragma: no cover - exercised when uninstalled
        result["error"] = f"playwright unavailable: {exc}"
        return result

    try:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context_kwargs = {}
                if video:
                    context_kwargs["record_video_dir"] = str(out)
                context = browser.new_context(**context_kwargs)
                try:
                    page = context.new_page()
                    page.goto(url, wait_until="load", timeout=15000)
                    if selector:
                        page.wait_for_selector(selector, timeout=15000)
                    screenshot_path = out / "screenshot.png"
                    page.screenshot(path=str(screenshot_path), full_page=True)
                    result["screenshot"] = str(screenshot_path)
                    video_obj = page.video
                finally:
                    context.close()
                if video and video_obj is not None:
                    try:
                        result["video"] = str(video_obj.path())
                    except Exception:
                        pass
            finally:
                browser.close()
    except Exception as exc:
        result["screenshot"] = None
        result["video"] = None
        result["error"] = str(exc)
    return result


def assertion_source_for(node_id: str, workspace: str) -> str:
    """Given a pytest node id ``path::test_name`` or
    ``path::Class::test_name``, read the VERBATIM source of that test
    function from disk via stdlib ``ast`` (no import, no pytest plugin).
    Returns "" when the file, class, or function cannot be resolved.
    """
    try:
        parts = node_id.split("::")
        if len(parts) < 2:
            return ""
        rel_path = parts[0]
        func_name = parts[-1]
        class_name = parts[1] if len(parts) == 3 else None

        file_path = Path(workspace) / rel_path
        if not file_path.is_file():
            return ""
        source = file_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(file_path))

        def _find_func(body, name):
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                    return node
            return None

        target_body = tree.body
        if class_name:
            cls_node = None
            for node in tree.body:
                if isinstance(node, ast.ClassDef) and node.name == class_name:
                    cls_node = node
                    break
            if cls_node is None:
                return ""
            target_body = cls_node.body

        func_node = _find_func(target_body, func_name)
        if func_node is None:
            return ""

        segment = ast.get_source_segment(source, func_node)
        return segment or ""
    except Exception:
        return ""


def provenance(build_version: str, tree_sha: str, now=None) -> dict:
    """Provenance metadata for a captured artifact. ``now`` is an injectable
    datetime for deterministic tests; defaults to the real current UTC time.
    """
    ts = now if now is not None else datetime.now(timezone.utc)
    return {
        "captured_by": "conductor-runner",
        "captured_at": ts.isoformat(),
        "build": build_version,
        "tree_sha": tree_sha,
    }
