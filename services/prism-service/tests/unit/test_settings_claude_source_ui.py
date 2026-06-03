"""RED scaffold — Settings per-project Claude-source card (task b6650506).

No JS test runner ships with the SPA, so the UI-FIRST acceptance is
pinned against SettingsPage.tsx SOURCE (same pattern as
test_memory_page_activation_ui.py). The card must:

  * be a real component (not the existing read-only claude_config_dir
    line), backed by the GET/POST /api/memory/claude-config endpoints;
  * show the persisted claude_project_dir, editable (an input + a save);
  * distinguish 'auto (slug)' vs 'explicit (reported by Claude)';
  * render structured Hermes primitives — NO raw <pre>/JSON.stringify
    of the config payload.

FAILS today: SettingsPage.tsx only shows `config dir -> {info.claude_config_dir}`
read-only; no claude-config card, no editor, no auto/explicit distinction.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SETTINGS = (
    _HERE.parent.parent.parent
    / "prism_service" / "web" / "src" / "pages" / "SettingsPage.tsx"
)


def _src() -> str:
    return _SETTINGS.read_text(encoding="utf-8")


def test_settings_calls_claude_config_endpoint():
    src = _src()
    assert "/api/memory/claude-config" in src, (
        "Settings must read/write the per-project claude-config endpoint"
    )


def test_settings_has_claude_source_card_component():
    src = _src()
    # A dedicated card/component for the reported Claude source.
    assert "ClaudeSource" in src or "claude-source" in src, (
        "a per-project Claude-source card component must exist"
    )


def test_settings_card_is_editable():
    src = _src()
    # An editable surface: an input bound to the dir + a save action that
    # POSTs the config.
    assert "claude_project_dir" in src, (
        "the card must reference the claude_project_dir field"
    )
    assert "api.post" in src, "the card must POST the edited dir"


def test_settings_distinguishes_auto_vs_explicit():
    src = _src()
    low = src.lower()
    assert "explicit" in low and "auto" in low, (
        "the card must distinguish auto (slug) vs explicit (reported by Claude)"
    )


def test_settings_card_renders_structured_not_raw_json():
    src = _src()
    # The config payload must not be dumped raw.
    assert "JSON.stringify(config" not in src
    # No <pre> wrapping the claude config block.
    block = src
    if "ClaudeSource" in src:
        block = src.split("ClaudeSource", 1)[-1][:4000]
    assert "<pre" not in block, (
        "render with Hermes primitives, not <pre> raw text"
    )
