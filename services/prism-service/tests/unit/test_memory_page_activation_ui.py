"""RED scaffold — MemoryPage activation UI (Tier 1, task 0bda3046).

The web app has no JS test runner, so the UI-FIRST acceptance criteria
are pinned by asserting the MemoryPage.tsx SOURCE (same pattern as
test_explore_narrative_ui.py):

  * the Entry type carries an `activation` field;
  * the "by value" grouping gains a visually-distinct "Fading" lane so
    decay is visible in the SPA, not headless;
  * each tile renders an activation badge (numeric / progress indicator).

FAILS today: MemoryPage.tsx has no `activation` field, no Fading bucket,
and no activation badge on the tile.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_WEB = (_HERE.parent.parent.parent / "prism_service" / "web" / "src"
        / "pages" / "MemoryPage.tsx")


def _src() -> str:
    return _WEB.read_text(encoding="utf-8")


def test_entry_type_has_activation_field():
    src = _src()
    assert "activation" in src, "MemoryPage must reference an activation field"
    # The Entry type block must declare it (number).
    entry_block = src.split("type Entry = {", 1)[-1].split("};", 1)[0]
    assert "activation" in entry_block, (
        "activation must be declared on the Entry type"
    )


def test_fading_lane_exists_in_by_value_grouping():
    src = _src()
    # A Fading lane/bucket label must exist in the "by value" buckets.
    assert "Fading" in src or "fading" in src, (
        "a Fading lane must surface low-activation entries in 'by value'"
    )
    # The value-bucket union currently is "proven" | "high" | "rest";
    # adding the Fading lane means a new bucket id appears.
    assert "fading" in src.lower(), "fading bucket id missing"


def test_fading_lane_is_visually_distinct_muted_tone():
    src = _src()
    # Decay must be VISIBLE and distinct — a muted/slate tone keyed to the
    # fading bucket (per UI-FIRST: not headless).
    fading_region = src.lower()
    assert "fading" in fading_region
    # A muted tone (slate) is used to render decay distinctly.
    assert "slate" in src, "fading lane must use a muted/slate tone"


def test_tile_renders_activation_badge():
    src = _src()
    # The tile must surface the computed activation score as a small badge
    # / numeric indicator. Reference the field off the entry inside the
    # tile render.
    assert "activation" in src
    # toFixed / Math.round / a numeric render of activation — the badge
    # shows the number, not just the raw field name.
    assert (".activation" in src or "entry.activation" in src or
            "e.activation" in src), "tile must read entry.activation"
    assert ("toFixed" in src or "Math.round" in src or "activation}" in src), (
        "activation badge must render a numeric value"
    )
