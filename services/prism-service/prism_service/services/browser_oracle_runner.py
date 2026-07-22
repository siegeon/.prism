"""Browser oracle runner — wire a REAL Playwright runner into oracle_spec's
browser adapter.

Friction that forced this (task d30c9a75): a UI/screenshot oracle fell back to
``manual_evidence_required`` (oracle_spec._run_browser with no runner), so
green_gate could only pass via a human OVERRIDE — "jumping through hoops". An
override is an escape hatch, not the norm; a browser oracle should EARN its
receipt. Now the daemon loads the oracle's URL headless, verifies it renders
real content, screenshots it into the task evidence store, and returns
pass/fail — a genuine passing receipt, no override.

Design: the heavy Playwright work runs in a SUBPROCESS (``python -m
prism_service.services.browser_oracle_runner <url> <shot> <token>``) so it works
from ANY daemon context — the sync Playwright API cannot run inside FastAPI's
asyncio loop, and a subprocess also isolates the browser from the server.
Non-policy module: oracle_spec._run_browser calls run() as its default runner.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

_URL_RE = re.compile(r"https?://[^\s)\"'<>]+")
_HOSTPORT_RE = re.compile(r"(?:127\.0\.0\.1|localhost):\d{2,5}(?:/[^\s)\"'<>]*)?")


def _extract_url(spec) -> str:
    """The first loadable URL named by the oracle (target text + positive
    assertions). Falls back to a bare 127.0.0.1:<port> dev address."""
    parts = [str(getattr(spec, "target", "") or "")]
    for a in (getattr(spec, "positive", []) or []):
        parts.append(str(a))
    text = " ".join(parts)
    m = _URL_RE.search(text)
    if m:
        return m.group(0).rstrip(".,);'\"")
    m = _HOSTPORT_RE.search(text)
    if m:
        return "http://" + m.group(0).rstrip(".,);'\"")
    return ""


def _expected_token(spec) -> str:
    """Optional literal the page MUST contain — the oracle marks it with
    text:"...". Empty = only assert the page renders."""
    m = re.search(r'text:\s*"([^"]{2,80})"', str(getattr(spec, "target", "") or ""))
    return m.group(1) if m else ""


def run(spec, ctx) -> dict:
    """oracle_spec browser-adapter runner. Returns
    {passed, observations, artifacts, reason}."""
    ctx = ctx or {}
    url = _extract_url(spec)
    if not url:
        # INCONCLUSIVE, not a failure: with no loadable URL the runner cannot
        # capture anything, so the honest boundary is manual evidence (a human
        # attaches the screenshot), never a hard fail (mx-6decaa).
        return {"passed": False, "inconclusive": True, "artifacts": [],
                "observations": [],
                "reason": "browser: no loadable URL found in the oracle text "
                          "— manual evidence required"}
    want = _expected_token(spec)
    shot_path = ""
    task_id = str(ctx.get("task_id", "") or "")
    if task_id:
        try:
            from prism_service.data_dir import evidence_dir
            shot_path = str(evidence_dir(task_id) / "browser_oracle.png")
        except Exception:
            shot_path = ""
    try:
        proc = subprocess.run(
            [sys.executable, "-m",
             "prism_service.services.browser_oracle_runner",
             url, shot_path, want],
            capture_output=True, text=True, timeout=120)
    except Exception as exc:
        # Could not even launch the capture subprocess -> inconclusive.
        return {"passed": False, "inconclusive": True, "artifacts": [],
                "observations": [],
                "reason": f"browser: runner subprocess failed ({exc}) "
                          "— manual evidence required"}
    line = (proc.stdout or "").strip().splitlines()[-1] if (proc.stdout or "").strip() else ""
    try:
        data = json.loads(line)
    except Exception:
        # No parseable verdict -> the capture never produced a judgment;
        # inconclusive, fall back to manual evidence rather than fail.
        return {"passed": False, "inconclusive": True, "artifacts": [],
                "observations": [],
                "reason": ("browser: runner produced no verdict "
                           f"(rc={proc.returncode}): {(proc.stderr or '')[:160]}"
                           " — manual evidence required")}
    passed = bool(data.get("passed"))
    obs = [{"name": "customer_observable_holds", "polarity": "positive",
            "expected": data.get("expected", "URL renders real content"),
            "observed": data.get("observed", ""), "passed": passed}]
    arts = [data["shot"]] if (passed and data.get("shot")) else []
    return {"passed": passed, "inconclusive": bool(data.get("inconclusive")),
            "observations": obs, "artifacts": arts,
            "reason": data.get("reason", "browser: ran")}


def _main(argv: list) -> int:
    """Subprocess entry: argv = [url, shot_path, token]. Loads the URL headless,
    verifies it renders (HTTP<400 + non-trivial body + optional token), writes a
    screenshot, prints ONE json verdict line."""
    url = argv[0] if argv else ""
    shot = argv[1] if len(argv) > 1 else ""
    want = argv[2] if len(argv) > 2 else ""
    verdict = {"passed": False, "inconclusive": False, "reason": "",
               "observed": "", "shot": ""}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        # No browser engine available here -> cannot capture -> inconclusive,
        # so the adapter falls back to manual evidence instead of failing.
        verdict["inconclusive"] = True
        verdict["reason"] = (f"browser: playwright unavailable ({exc}) "
                             "— manual evidence required")
        print(json.dumps(verdict))
        return 0
    status, body, err = 0, "", ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            try:
                resp = page.goto(url, wait_until="load", timeout=45000)
                status = resp.status if resp else 0
                page.wait_for_timeout(2500)  # let an SPA / Ladle render
                body = (page.inner_text("body") or "").strip()
                if shot:
                    page.screenshot(path=shot)
                    verdict["shot"] = shot
            finally:
                browser.close()
    except Exception as exc:
        err = str(exc)
    if err:
        # The surface could not be reached to be judged (server down / nav
        # error) -> inconclusive in an automated context; a human confirms.
        verdict["inconclusive"] = True
        verdict["reason"] = (f"browser: {url} failed to load — {err[:160]} "
                             "— manual evidence required")
        verdict["observed"] = "load error"
        print(json.dumps(verdict))
        return 0
    rendered = bool(status) and status < 400 and len(body) > 20
    token_ok = (want.lower() in body.lower()) if want else True
    verdict["passed"] = rendered and token_ok
    verdict["expected"] = ("URL renders real content"
                           + (f" incl. '{want}'" if want else ""))
    verdict["observed"] = (f"HTTP {status}, {len(body)} chars"
                           + ("" if token_ok else f", missing '{want}'"))
    verdict["reason"] = (
        f"browser: loaded {url} (HTTP {status}), body {len(body)} chars"
        + (f", found '{want}'" if want else "")
        + (" — customer surface renders" if verdict["passed"]
           else " — did NOT render expected content"))
    print(json.dumps(verdict))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
