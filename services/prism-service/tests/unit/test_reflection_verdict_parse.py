"""Reflection verdict parsing must tolerate prose/fences around the JSON.

The reflection sub-agent is asked for a bare JSON object, but real replies
sometimes prefix a sentence of reasoning or wrap the object in a ```json
fence. Before this fix the whole-string-fence-only parser dropped those as
"not valid JSON", discarding the verdict (score, narrative, AND any proposed
memories) and abandoning the candidate — the upstream reason the learning
loop minted ~0 memories. Pins the recovery path.
"""

import pytest

from prism_service.services.reflection_runner import _extract_verdict


def test_bare_json_object_parses():
    raw = '{"qualitative_score": 0.7, "new_memories": []}'
    v = _extract_verdict(raw)
    assert v["qualitative_score"] == 0.7
    assert v["new_memories"] == []


def test_fenced_json_object_parses():
    raw = '```json\n{"qualitative_score": 0.4, "new_memories": []}\n```'
    v = _extract_verdict(raw)
    assert v["qualitative_score"] == 0.4


def test_prose_before_json_is_recovered():
    # The exact failure mode observed live on candidate d1d9eae1: a sentence
    # of reasoning, a blank line, then the verdict object.
    raw = (
        "The transcript contradicts the present state, and there is no "
        "stable, verifiable convention to capture.\n\n"
        '{"qualitative_score": 0.15, "narrative": "checked E:\\\\.prism", '
        '"new_memories": []}'
    )
    v = _extract_verdict(raw)
    assert v["qualitative_score"] == 0.15
    assert v["new_memories"] == []


def test_prose_with_braces_then_real_verdict_prefers_verdict():
    # Prose that itself contains a brace must not shadow the real object.
    raw = (
        "I considered the set {a, b} of files.\n"
        '{"qualitative_score": 0.9, "new_memories": [{"name": "x"}]}'
    )
    v = _extract_verdict(raw)
    assert v["qualitative_score"] == 0.9
    assert v["new_memories"][0]["name"] == "x"


def test_unparseable_text_raises():
    with pytest.raises(ValueError):
        _extract_verdict("no json here at all")
