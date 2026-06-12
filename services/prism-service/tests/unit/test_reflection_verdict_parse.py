"""Reflection verdict parser tolerates prose-before-JSON.

Regression guard for v6.3.36. A reflect sub-agent told to "Output ONLY the
JSON object" routinely prepends a sentence or two of reasoning after 15
tool-using turns. The old whole-string fence-strip then failed and a perfectly
good verdict was abandoned as "verdict not valid JSON" (observed live:
``"...source is empty.\\n\\n{...}"``). ``_extract_verdict_json`` must recover
the object from prose before/after it, from markdown fences, or bare.
"""

from __future__ import annotations

import pytest

from prism_service.services.reflection_runner import _extract_verdict_json


def test_plain_json_object():
    assert _extract_verdict_json('{"qualitative_score": 0.5}') == {
        "qualitative_score": 0.5
    }


def test_markdown_fenced_json():
    assert _extract_verdict_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_prose_before_json_the_live_failure_mode():
    raw = (
        "The reflection target checkout is empty. I cannot verify any file "
        "under it, so no candidate memory can be grounded.\n\n"
        '{"qualitative_score": 0.35, "new_memories": [], "confidence": 0.4}'
    )
    out = _extract_verdict_json(raw)
    assert out["qualitative_score"] == 0.35
    assert out["new_memories"] == []


def test_prose_before_and_after_json():
    raw = 'Here is my verdict:\n{"confidence": 0.4}\nLet me know if you need more.'
    assert _extract_verdict_json(raw) == {"confidence": 0.4}


def test_fenced_json_with_leading_prose():
    assert _extract_verdict_json('Reasoning first...\n```json\n{"x": 2}\n```') == {
        "x": 2
    }


def test_no_json_raises():
    with pytest.raises(Exception):
        _extract_verdict_json("no json object here at all")


def test_bare_list_is_not_a_verdict():
    # a verdict must be an object; a top-level array is rejected
    with pytest.raises(Exception):
        _extract_verdict_json("[1, 2, 3]")
