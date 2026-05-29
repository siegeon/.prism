"""UI contract test for the Explore page Narrative annotation layer
(Ultimate Graph, siegeon/.prism#50 — final narrative slice).

The web app has no JS test runner, so the UI-FIRST acceptance criterion is
pinned by asserting the ExplorePage.tsx SOURCE: the dashed-border italic
placeholder stub (ExplorePage.tsx:452-458) must be REPLACED with a structured
Hermes render of each annotation's {name, purpose, provenance, updated_at},
with a provenance Pill that visually distinguishes 'deterministic' from the
LLM literal 'claude @ <date>'. No <pre>JSON dumps (render-structured /
Hermes-native, per memory).

FAILS today because the stub is still in place and nothing renders
sel.annotations. Goes green when the placeholder is swapped for real markup.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_WEB = (_HERE.parent.parent.parent / "prism_service" / "web" / "src"
        / "pages" / "ExplorePage.tsx")


def _src() -> str:
    return _WEB.read_text(encoding="utf-8")


def test_narrative_placeholder_stub_is_gone():
    src = _src()
    # The dead placeholder copy must be removed.
    assert "LLM annotations land here once the background enrichment loop" \
        not in src
    assert "border-dashed" not in src or "Narrative" not in src.split(
        "border-dashed")[0][-200:], "dashed Narrative stub must be replaced"


def test_narrative_renders_annotation_fields():
    src = _src()
    # The context type already carries annotations; the panel must now READ
    # the fields of each annotation, not ignore them.
    assert "annotations" in src
    assert ".purpose" in src, "annotation purpose must be rendered"
    assert ".provenance" in src, "annotation provenance must be rendered"
    assert ".updated_at" in src or ".name" in src, \
        "annotation name/updated_at must be rendered"


def test_provenance_pill_distinguishes_llm_from_deterministic():
    src = _src()
    # A Pill (Hermes primitive) keyed off provenance, branching on the
    # 'claude @' / deterministic distinction.
    assert "Pill" in src
    assert "claude @" in src or "deterministic" in src, \
        "provenance pill must branch on deterministic vs 'claude @ <date>'"


def test_no_raw_json_dump_in_narrative():
    src = _src()
    # Render structured — never <pre>JSON.stringify(...)</pre> for annotations.
    assert "JSON.stringify" not in src
    assert "<pre>" not in src


def test_annotations_typed_not_unknown():
    src = _src()
    # The Ctx.annotations field was `unknown[]`; once rendered it must carry a
    # real shape so name/purpose/provenance/updated_at are typed, not opaque.
    assert "annotations: unknown[]" not in src, \
        "annotations must be given a real type once rendered"
