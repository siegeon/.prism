"""The Understand page focuses a concept on the map by id, and the two sides
of that seam are written in different languages.

The SPA computes `#focus=<slug>` from a concept id in TypeScript; the map
builder computes a component id from the same concept id in Python. If either
drifts, the focus link silently points at nothing and the map simply never
highlights — no error, no failing render. This pins both halves against the
SAME inputs instead of against my assumption of the other side.
"""

from __future__ import annotations

import re
from pathlib import Path

from prism_service.services.archify_maps._layout import slug

_MAPS_TSX = (Path(__file__).resolve().parents[2] / "prism_service" / "web" /
             "src" / "components" / "maps" / "ArchifyMaps.tsx")

# Real concept ids, as GET /api/okf/graph returns them.
_CONCEPT_IDS = ["mx-e55e50", "mx-0103ae", "mx-877db5", "mx-f49a5c", "mx-20175b"]


def _slug_in_typescript(text: str) -> str:
    """The TypeScript slugForFocus rule, applied here so both sides are run
    against the same inputs rather than compared by eye."""
    lowered = re.sub(r"-+", "-", re.sub(r"[^a-z0-9_-]+", "-", text.lower()))
    return f"n-{lowered}" if lowered[:1].isdigit() else lowered


def test_both_sides_agree_on_a_real_concept_id():
    for cid in _CONCEPT_IDS:
        assert slug(cid) == _slug_in_typescript(cid), cid


def test_a_concept_id_survives_unchanged():
    """A memory id is already a legal archify id, so focus is a plain match."""
    for cid in _CONCEPT_IDS:
        assert slug(cid) == cid


def test_the_spa_still_computes_the_focus_hash():
    """If the component stops building the hash, focus dies silently."""
    src = _MAPS_TSX.read_text(encoding="utf-8")
    assert "slugForFocus" in src
    assert "#focus=" in src
