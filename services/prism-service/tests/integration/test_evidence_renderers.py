"""Guard — NO evidence renderer can paint a broken tile (task 63d0086e).

History this suite exists to end: a cited log rendered as a BROKEN IMAGE three
separate times, because PRISM has three independent evidence renderers and each
fix touched only the ones the author happened to know about.

  939756eb  fixed EvidenceView.
  48ee8f05  fixed EvidenceGallery — and added a guard with a HAND-MAINTAINED
            list of those two files, which is exactly why it stayed green while
            TaskDetailPage's ProofShots kept imaging logs.

So the renderer set is now DISCOVERED from the source. There is no list to keep
up to date and no ignore list: a false positive is fixed by making the detector
more precise, never by suppressing a file.

The SPA has no JS test runner, so these ACs are pinned against the ACTUAL TSX
source (the convention used by every other *_ui.py test here).
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

_WEB_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"

# An <img> whose src is bound to an EVIDENCE/PROOF url expression. Matched
# structurally (the JSX), never by filename — a new component is caught the
# moment it renders one of these.
_EVIDENCE_IMG = re.compile(
    r"<img\s[^>]*src=\{\s*(?:it|shown|f|item|ev|shot)?\.?"
    r"(?:url|src)\s*\}", re.S)

# Evidence of text handling: a text kind branch, a text-src predicate, or a
# dedicated text component.
_TEXT_HANDLING = re.compile(
    r'===\s*"text"|kind\s*===\s*\'text\'|"text"\s*\?|isTextSrc|'
    r'TextEvidence|TextArtifact|ProofText', re.S)


def _tsx_files() -> list[Path]:
    return sorted(p for p in _WEB_SRC.rglob("*.tsx") if p.is_file())


def discover_evidence_renderers() -> list[Path]:
    """Every .tsx that renders an evidence image. DERIVED, never listed."""
    return [p for p in _tsx_files()
            if _EVIDENCE_IMG.search(p.read_text(encoding="utf-8"))]


DISCOVERED = discover_evidence_renderers()


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ── AC-5 / AC-6: discovery is real, and finds what we know is there ────

def test_the_guard_has_no_hardcoded_renderer_list():
    """The hand-maintained list WAS the defect generator — it must not return."""
    src = _read(_HERE)
    # Anchored to a MODULE-LEVEL assignment so this assertion cannot match its
    # own text (the naive substring check flagged itself).
    assert not re.search(r"^[A-Z_]*RENDERERS\s*=\s*\[", src, re.M), (
        "the renderer set must be discovered, not enumerated by hand")
    assert "rglob" in src, "the guard must walk the source tree"


def test_discovery_finds_the_known_renderers():
    """A detector that matches nothing would pass every other test vacuously."""
    names = {p.name for p in DISCOVERED}
    for expected in ("TaskDetailPage.tsx", "EvidenceGallery.tsx"):
        assert expected in names, (
            f"discovery must find {expected}; found {sorted(names)}")
    assert len(DISCOVERED) >= 2


# ── AC-7: every DISCOVERED renderer handles text ──────────────────────

@pytest.mark.parametrize("path", DISCOVERED, ids=lambda p: p.name)
def test_every_discovered_renderer_handles_text(path):
    assert _TEXT_HANDLING.search(_read(path)), (
        f"{path.name} renders evidence images but has no text handling — a "
        "cited .txt/.log/.json would paint a BROKEN TILE there. Give it a "
        "text branch; do not add an exclusion.")


# ── AC-1 / AC-2: the task page proof tiles + lightbox render text ─────

def test_proof_shots_renders_text_before_falling_back_to_an_image():
    src = _read(_WEB_SRC / "pages" / "TaskDetailPage.tsx")
    assert "ProofText" in src or 'kind === "text"' in src, (
        "ProofShots needs a text branch")
    assert "<pre" in src, "a cited log must render as preformatted text"
    # Scope to the ProofShots renderer — the file also contains unrelated
    # images (avatars), so a whole-file index would compare the wrong element.
    body = src[src.index("function ProofShots"):]
    tile_img = body.index("<img")
    first_text = min(
        (body.index(tok) for tok in ('kind === "text"', "ProofText")
         if tok in body),
        default=len(body))
    assert first_text < tile_img, (
        "the text branch must be evaluated BEFORE the image fallback")


def test_proof_lightbox_opens_text_as_text():
    src = _read(_WEB_SRC / "pages" / "TaskDetailPage.tsx")
    box = src[src.index("aria-modal"):]
    assert 'kind === "text"' in box or "ProofText" in box, (
        "the lightbox must open a text artifact as text, not the image viewer")


# ── AC-3 / AC-4: media survives, text is never hidden ─────────────────

def test_media_paths_survive():
    src = _read(_WEB_SRC / "pages" / "TaskDetailPage.tsx")
    assert "<img" in src, "screenshots must still render"


def test_text_citations_are_not_filtered_out():
    src = _read(_WEB_SRC / "pages" / "TaskDetailPage.tsx")
    assert 'kind !== "text"' not in src, (
        "text evidence must be RENDERED, not filtered out of the proof tiles")
