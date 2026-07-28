"""RED scaffold — EVERY evidence renderer handles text (task 48ee8f05).

Task 939756eb made logs/diffs servable and taught EvidenceView to render them,
but the GATE PANEL uses a different component: EvidenceGallery, whose item kind
is only "image" | "video". The API's new kind "text" therefore falls through to
an <img src=...txt>, painting a BROKEN IMAGE tile — which is exactly what the
owner saw on task dbbea1d3.

That miss happened because the previous slice pinned ONE renderer. This suite
enumerates them ALL, so adding a third renderer without text support fails.

The SPA has no JS test runner, so these ACs are pinned by asserting the ACTUAL
TSX source (the convention used by every other *_ui.py test here).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_WEB = _SERVICE_ROOT / "prism_service" / "web" / "src"
_GALLERY = _WEB / "components" / "EvidenceGallery.tsx"
_VIEW = _WEB / "components" / "EvidenceView.tsx"

# Every component that renders task evidence. A new renderer belongs HERE, and
# the guard below forces it to handle text before it can ship.
EVIDENCE_RENDERERS = [_GALLERY, _VIEW]


def _read(p: Path) -> str:
    assert p.exists(), f"expected renderer missing: {p}"
    return p.read_text(encoding="utf-8")


# ── AC-1: the gallery knows the text kind ──────────────────────────────

def test_gallery_kind_admits_text():
    """EVERY kind annotation must admit text — including the one that derives a
    kind for files cited by the completion proof, which is the path that made a
    cited .txt render as a broken image tile."""
    src = _read(_GALLERY)
    kinds = re.findall(r'kind\s*\??:\s*("(?:image|video)"(?:\s*\|\s*"\w+")*)', src)
    assert len(kinds) >= 3, (
        f"expected the item type, the api type AND the derived kind to be "
        f"annotated; found {kinds}")
    for union in kinds:
        assert '"text"' in union, (
            f'the kind union {union!r} must admit "text" — otherwise a log is '
            "coerced down the image path and paints a broken tile")


# ── AC-2 / AC-3: text renders as text, in the tile AND the lightbox ────

def test_gallery_has_a_text_branch():
    src = _read(_GALLERY)
    assert '=== "text"' in src or "=== 'text'" in src, (
        "EvidenceGallery needs an explicit text branch")
    assert "<pre" in src, (
        "a text artifact must render as preformatted text, not an <img>")


def test_text_is_not_routed_into_the_image_element():
    """The bug: every non-video item fell through to <img src={it.url}>.

    Anchored on the REAL JSX elements (not any '<img' substring, which also
    appears in prose) so the assertion cannot be satisfied by a comment.
    """
    src = _read(_GALLERY)
    tile_img = src.index("<img src={it.url}")
    text_branch = src.index('it.kind === "text"')
    assert text_branch < tile_img, (
        "the tile's text branch must be evaluated BEFORE the <img> fallback")
    box_img = src.index("<img src={shown.url}")
    box_text = src.index('shown.kind === "text"')
    assert box_text < box_img, (
        "the lightbox's text branch must be evaluated BEFORE the image viewer")


# ── AC-4: media still works ────────────────────────────────────────────

def test_image_and_video_branches_survive():
    src = _read(_GALLERY)
    assert "<img" in src, "image evidence must still render an <img>"
    assert "<video" in src, "video evidence must still render a <video>"


# ── AC-5: text is rendered, never hidden ───────────────────────────────

def test_text_items_are_not_filtered_out():
    src = _read(_GALLERY)
    assert 'kind !== "text"' not in src and "kind !== 'text'" not in src, (
        "text evidence must be RENDERED, not filtered out of the gallery")


# ── AC-6: every renderer is guarded ────────────────────────────────────

@pytest.mark.parametrize("path", EVIDENCE_RENDERERS, ids=lambda p: p.name)
def test_every_evidence_renderer_handles_text(path):
    src = _read(path)
    handles_text = ('"text"' in src or "'text'" in src
                    or "isTextSrc" in src or "TextEvidence" in src)
    assert handles_text, (
        f"{path.name} renders evidence but has no text handling — a cited log "
        "would paint a broken image tile there (the defect this suite exists "
        "to prevent)")
