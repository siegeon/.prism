"""RED live-seam test — task 401811b8 "Radix color tokens — legible in both themes".

The dual-theme tokens must be SERVED by the dev daemon at 127.0.0.1:8888
(the built SPA CSS bundle), not merely sit in source: source-only tokens with
a stale web_dist/an unbounced daemon shipped false-greens before. Pins the
user-facing integration: npm build shipped + daemon bounced onto it.

NOT run in PR CI (pr-checks.yml's "Pytest (integration)" step explicitly
--ignores this file - task 170991cc); enforced locally where the dev daemon
is mandated always-on (feedback-dev-must-stay-current).
"""
from __future__ import annotations

import re
import urllib.request

_BASE = "http://127.0.0.1:8888"


def _get(path: str) -> str:
    with urllib.request.urlopen(_BASE + path, timeout=10) as r:
        return r.read().decode("utf-8", "replace")


def _served_css() -> str:
    html = _get("/")
    hrefs = re.findall(r'href="([^"]+\.css)"', html)
    assert hrefs, "SPA index.html served no CSS bundle at :8888"
    return "\n".join(
        _get(h if h.startswith("/") else "/" + h) for h in hrefs)


def test_live_css_carries_dual_theme_switching():
    css = _served_css()
    assert "--accent-amber-fg" in css, \
        "served CSS lost the accent token family entirely"
    assert re.search(r"data-theme|prefers-color-scheme", css), (
        "the CSS served at :8888 has no theme switching — the light-theme "
        "radix tokens were never built + bounced onto the live daemon"
    )


def test_live_css_carries_et_categorical_ramp():
    assert "--et-" in _served_css(), (
        "the CSS served at :8888 carries no --et-* categorical ramp — the "
        "entity-type palette never reached the live build"
    )
