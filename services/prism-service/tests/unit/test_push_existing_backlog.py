"""RED scaffold -- widen the existing-backlog push UI to Jira (task 56074410).

The PRISM SPA has no JS test runner (test_conductor_page_animated_cleanup_ui.py's
established convention), so this file pins the acceptance criteria by
asserting the ACTUAL SettingsPage.tsx source, parsing the enclosing JSX
branch rather than matching a bare substring.

Two gaps in SettingsPage.tsx:
  1. The render site (~line 3785) gates the whole BacklogPush affordance on
     ``c.provider === "github"`` only, so the Jira card never gets it. Must
     widen to name "jira" explicitly too (never every provider in the
     registry, mx-804f8e).
  2. BacklogPush's own confirm button (~line 3553) and success message
     (~line 3564) hardcode the literal string "GitHub", so a Jira push would
     lie about which provider it just pushed to. Must read a provider-
     generic label off the connector instead.
"""

from __future__ import annotations

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
_SETTINGS_PAGE = (_SERVICE_ROOT / "prism_service" / "web" / "src"
                  / "pages" / "SettingsPage.tsx")


def _source() -> str:
    return _SETTINGS_PAGE.read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """Slice from ``function <name>(`` to the next top-level ``function ``
    declaration -- the same discipline test_the_claude_card... lessons
    demand: never a fixed character window, always the enclosing block."""
    start = src.index(f"function {name}(")
    end = src.index("\nfunction ", start + 1)
    return src[start:end]


def _enclosing_brace_block(src: str, needle: str) -> str:
    """The ``{ ... }`` JSX expression container that encloses ``needle``,
    found by BALANCING braces rather than a no-nested-brace regex -- the
    render site's guard itself contains nested ``{c}``/``{project}`` prop
    braces, which a naive ``[^{}]*`` pattern cannot see past."""
    idx = src.index(needle)
    depth = 0
    start = None
    i = idx - 1
    while i >= 0:
        ch = src[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                start = i
                break
            depth -= 1
        i -= 1
    assert start is not None, f"no enclosing {{...}} found before {needle!r}"

    depth = 0
    j = start
    while j < len(src):
        ch = src[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:j + 1]
        j += 1
    raise AssertionError(f"unbalanced braces looking for the block enclosing {needle!r}")


# ── AC-1: the render site names "jira" explicitly, alongside "github" ──

def test_backlog_push_render_site_names_jira_explicitly():
    src = _source()
    connectors_body = _function_body(src, "ConnectorsSection")

    # Find the actual JSX conditional that renders <BacklogPush ...>, not a
    # stray reference to the component name elsewhere in the file.
    guard = _enclosing_brace_block(connectors_body, "<BacklogPush")
    assert "<BacklogPush" in guard

    # The guard must check the provider against BOTH "github" and "jira" --
    # never widen to "every provider in the registry" (mx-804f8e): a
    # PROVIDERS.includes(...)-style check would also silently cover any
    # future provider with no review of whether it can push at all.
    assert '"github"' in guard and '"jira"' in guard, (
        f"BacklogPush's render guard must name github AND jira explicitly, "
        f"got: {guard!r}")
    assert "c.provider ===" in guard or "connector.provider ===" in guard or \
        "c.provider ==" in guard, (
        f"expected an explicit provider equality check, got: {guard!r}")


def test_backlog_push_render_site_still_requires_tracking():
    src = _source()
    connectors_body = _function_body(src, "ConnectorsSection")
    guard = _enclosing_brace_block(connectors_body, "<BacklogPush")
    assert "tracking" in guard, (
        "widening to jira must not drop the existing 'has something "
        "tracked' guard -- an untracked connector must still get nothing")


# ── AC-2: the confirm label is provider-generic, never a literal "GitHub" ──

def test_confirm_button_label_is_not_hardcoded_to_github():
    src = _source()
    body = _function_body(src, "BacklogPush")
    # Locate the confirm button specifically (the element with onClick={confirm}),
    # not the whole component body, so a "to GitHub" string anywhere else in
    # the file cannot accidentally satisfy this.
    btn_start = body.index("onClick={confirm}")
    btn_end = body.index("</button>", btn_start)
    confirm_button = body[btn_start:btn_end]
    assert "to GitHub" not in confirm_button, (
        "the confirm label must not hardcode GitHub -- it renders for jira "
        "too now, and would lie about the destination")
    # It must instead be driven by something on the connector (name, or a
    # per-provider label map), i.e. a template expression, not a bare
    # string literal for the destination.
    assert re.search(r'\$\{[^}]*connector', confirm_button), (
        f"expected the confirm label to interpolate something off "
        f"`connector`, got: {confirm_button!r}")


def test_success_message_is_not_hardcoded_to_github():
    src = _source()
    body = _function_body(src, "BacklogPush")
    done_start = body.index("{done &&")
    done_end = body.index("</p>", done_start)
    done_block = body[done_start:done_end]
    assert "on GitHub" not in done_block, (
        "the post-confirm success message must not hardcode GitHub either")
    assert re.search(r'\$\{[^}]*connector', done_block), (
        f"expected the success message to interpolate something off "
        f"`connector`, got: {done_block!r}")


# ── AC-3: regression guard -- github's own affordance must stay reachable ──

def test_github_backlog_push_affordance_is_unregressed():
    src = _source()
    connectors_body = _function_body(src, "ConnectorsSection")
    guard = _enclosing_brace_block(connectors_body, "<BacklogPush")
    assert '"github"' in guard, (
        "widening to jira must not have dropped github from the guard")
