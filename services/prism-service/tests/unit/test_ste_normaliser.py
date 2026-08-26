"""STE normaliser (task 36283d72) — deterministic regex fixes plus the
checks a regex must not attempt on its own, and the TaskService wiring
that runs the normaliser on every task write.

No model call anywhere in this file. Every assertion is a plain string
or offset comparison against a fixed input.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import ste  # noqa: E402


def _mk_service(tmp_path):
    from prism_service.services.task_service import TaskService

    return TaskService(str(tmp_path / "tasks.db"))


# ----------------------------------------------------------------------
# One test per substitution rule
# ----------------------------------------------------------------------


def test_contraction_expands():
    fixed, rules = ste.normalize("We don't agree with that.")
    assert fixed == "We do not agree with that."
    assert rules == ["contraction"]


def test_contraction_im_stays_capital():
    fixed, rules = ste.normalize("i'm not sure about this.")
    assert fixed == "I am not sure about this."
    assert rules == ["contraction"]


def test_semicolon_becomes_sentence_break():
    fixed, rules = ste.normalize("Run the build; check the output.")
    assert fixed == "Run the build. Check the output."
    assert rules == ["semicolon"]


def test_filler_phrase_swapped():
    fixed, rules = ste.normalize(
        "We did this in order to keep things simple."
    )
    assert fixed == "We did this to keep things simple."
    assert rules == ["filler"]


def test_filler_utilize_swapped():
    fixed, rules = ste.normalize("Utilize the existing helper.")
    assert fixed == "Use the existing helper."
    assert rules == ["filler"]


def test_nominalisation_swapped():
    fixed, rules = ste.normalize(
        "The team will perform an analysis of the logs."
    )
    assert fixed == "The team will analyze the logs."
    assert rules == ["nominalisation"]


def test_phrasal_verb_swapped():
    fixed, rules = ste.normalize("Spin up a scratch daemon first.")
    assert fixed == "Start a scratch daemon first."
    assert rules == ["phrasal-verb"]


def test_marketing_word_deleted():
    fixed, rules = ste.normalize("This gives a seamless login flow.")
    assert "seamless" not in fixed.lower()
    assert "  " not in fixed
    assert rules == ["marketing"]


def test_marketing_word_deletion_fixes_dangling_article():
    fixed, rules = ste.normalize("The new build is blazing-fast.")
    assert fixed == "The new build is."
    assert rules == ["marketing"]


def test_whitespace_collapsed():
    fixed, rules = ste.normalize("Two    spaces   in a row.")
    assert fixed == "Two spaces in a row."
    assert rules == ["whitespace"]


# ----------------------------------------------------------------------
# Protected spans
# ----------------------------------------------------------------------


def test_semicolon_inside_code_span_survives():
    text = "Check `a;b` before you continue."
    fixed, rules = ste.normalize(text)
    assert fixed == text
    assert rules == []


def test_semicolon_inside_code_span_is_flagged_by_check():
    text = "Check `a;b` before you continue."
    findings = ste.check(text)
    semicolon_findings = [f for f in findings if f.rule == "semicolon"]
    assert len(semicolon_findings) == 1
    f = semicolon_findings[0]
    assert text[f.start:f.end] == ";"


def test_contraction_inside_url_survives():
    text = "See https://example.com/it's-fine for the writeup."
    fixed, rules = ste.normalize(text)
    assert fixed == text
    assert rules == []


def test_uuid_untouched():
    text = "Linked to 550e8400-e29b-41d4-a716-446655440000 already."
    fixed, rules = ste.normalize(text)
    assert fixed == text
    assert rules == []


def test_fenced_block_byte_identical():
    text = "Before.\n```\ndon't; utilize this\n```\nAfter."
    fixed, rules = ste.normalize(text)
    # The fenced block itself never changes, even though the prose
    # around it does not need a fix here either (single sentences,
    # no marketing words).
    assert "```\ndon't; utilize this\n```" in fixed
    assert fixed.startswith("Before.")
    assert fixed.endswith("After.")


def test_quoted_string_with_contraction_survives():
    text = "He said 'don't touch it' during the review."
    fixed, rules = ste.normalize(text)
    assert fixed == text
    assert rules == []


# ----------------------------------------------------------------------
# Hedge / compliant round-trip
# ----------------------------------------------------------------------


def test_hedge_sentence_unchanged_zero_findings():
    text = "The request may have failed."
    fixed, rules = ste.normalize(text, mode="flavored")
    assert fixed == text
    assert rules == []
    assert ste.check(text, mode="flavored") == []


def test_compliant_text_round_trips_unchanged():
    text = "The build failed. Check the logs and rerun the suite."
    fixed, rules = ste.normalize(text, mode="flavored")
    assert fixed == text
    assert rules == []


# ----------------------------------------------------------------------
# Golden test — six realistic task descriptions
# ----------------------------------------------------------------------

_GOLDEN_DESCRIPTIONS = [
    "We don't ship this until QA signs off on every scenario in the "
    "checklist. See services/prism-service/prism_service/services/"
    "task_service.py for the exact write path that needs the fix.",

    "To fix task 36283d72, someone needs to spin up a scratch daemon "
    "on the dev host, reproduce the failure against the pinned suite, "
    "confirm the root cause, and only then write up the findings.",

    "It's fine to utilize the existing helper for this change since it "
    "already covers the common case; don't add a new one just for this "
    "ticket. Check `services/prism-service/tests/unit/test_ste_normaliser.py` "
    "for the pinned suite that exercises it.",

    "Prior to release, someone on the team should perform an analysis "
    "of the logs at https://example.com/logs/latest, note any repeated "
    "errors across the last three deploys, and file a follow-up ticket "
    "for each root cause that is not already tracked in the backlog.",

    "The fix is tracked in mx-abc9fc and depends on the migration "
    "recorded under 550e8400-e29b-41d4-a716-446655440000. We're "
    "confident the fix holds once that migration lands on the shared "
    "staging database.",

    "Due to the fact that the daemon restarts slowly after a config "
    "change on this host, we don't retry automatically on the first "
    "failure during a normal deploy; a number of callers already "
    "handle the timeout on their own with a small backoff loop around "
    "the call.",
]


def test_golden_descriptions_bounded_word_count_change():
    for text in _GOLDEN_DESCRIPTIONS:
        fixed, _rules = ste.normalize(text, mode="flavored")
        before_words = len(text.split())
        after_words = len(fixed.split())
        delta = abs(after_words - before_words)
        assert delta <= max(1, round(before_words * 0.10)), (
            f"word count moved too far for: {text!r} -> {fixed!r}"
        )


def test_golden_descriptions_protected_spans_byte_identical():
    for text in _GOLDEN_DESCRIPTIONS:
        protected = ste._protected_spans(text)
        fixed, _rules = ste.normalize(text, mode="flavored")
        fixed_protected = ste._protected_spans(fixed)
        original_chunks = [text[s:e] for s, e in protected]
        fixed_chunks = [fixed[s:e] for s, e in fixed_protected]
        assert original_chunks == fixed_chunks, (
            f"a protected span moved for: {text!r} -> {fixed!r}"
        )
        # Every original protected chunk must still appear verbatim.
        for chunk in original_chunks:
            assert chunk in fixed, f"{chunk!r} missing from {fixed!r}"


# ----------------------------------------------------------------------
# check() structural rules
# ----------------------------------------------------------------------


def test_sentence_length_flagged_in_strict_mode():
    text = (
        "This sentence has more than twenty words in it because it "
        "keeps going and going without any punctuation to stop it at "
        "all whatsoever today."
    )
    findings = ste.check(text, mode="strict")
    hits = [f for f in findings if f.rule == "sentence-length"]
    assert len(hits) == 1
    assert hits[0].start == 0
    assert hits[0].end == len(text)
    assert text[hits[0].start:hits[0].end] == text


def test_sentence_length_not_flagged_under_flavored_threshold():
    text = "This sentence has more than twenty words in it because " \
           "it keeps going a little bit today."
    # 17 words -- under both thresholds.
    findings = ste.check(text, mode="flavored")
    assert not [f for f in findings if f.rule == "sentence-length"]


def test_passive_voice_flagged():
    text = "The file was deleted by the script."
    findings = ste.check(text, mode="flavored")
    hits = [f for f in findings if f.rule == "passive-voice"]
    assert len(hits) == 1
    assert text[hits[0].start:hits[0].end] == "was deleted"


def test_passive_voice_allowlist_not_flagged():
    text = "The value is required for this to work."
    findings = ste.check(text, mode="flavored")
    assert not [f for f in findings if f.rule == "passive-voice"]


def test_present_perfect_flagged():
    text = "The team has completed the migration."
    findings = ste.check(text, mode="flavored")
    hits = [f for f in findings if f.rule == "present-perfect"]
    assert len(hits) == 1
    assert text[hits[0].start:hits[0].end] == "has completed"


def test_present_perfect_skipped_after_hedge():
    text = "The migration might have completed already."
    findings = ste.check(text, mode="flavored")
    assert not [f for f in findings if f.rule == "present-perfect"]


def test_hedge_stacking_flagged():
    text = "This might possibly fail under load."
    findings = ste.check(text, mode="flavored")
    hits = [f for f in findings if f.rule == "hedge-stacking"]
    assert len(hits) == 1
    assert hits[0].start == 0
    assert hits[0].end == len(text)


def test_multi_instruction_flagged_strict_only():
    text = "Run the tests, then commit the change."
    strict_hits = [
        f for f in ste.check(text, mode="strict") if f.rule == "multi-instruction"
    ]
    flavored_hits = [
        f for f in ste.check(text, mode="flavored") if f.rule == "multi-instruction"
    ]
    assert len(strict_hits) == 1
    assert strict_hits[0].start == 0
    assert strict_hits[0].end == len(text)
    assert flavored_hits == []


def test_long_paragraph_flagged():
    sentences = " ".join(f"Sentence number {i} is here." for i in range(8))
    findings = ste.check(sentences, mode="flavored")
    hits = [f for f in findings if f.rule == "long-paragraph"]
    assert len(hits) == 1
    assert hits[0].start == 0
    assert hits[0].end == len(sentences)


def test_paragraph_of_six_sentences_not_flagged():
    sentences = " ".join(f"Sentence number {i} is here." for i in range(6))
    findings = ste.check(sentences, mode="flavored")
    assert not [f for f in findings if f.rule == "long-paragraph"]


# ----------------------------------------------------------------------
# Sentence splitting must not fire on abbreviations / decimals / dotted
# identifiers
# ----------------------------------------------------------------------


def test_sentence_split_ignores_abbreviation():
    text = "Fix the flaky tests, e.g. the retry loop, before you ship."
    protected = ste._protected_spans(text)
    sentences = ste._split_sentences(text, protected)
    assert len(sentences) == 1


def test_sentence_split_ignores_version_number():
    text = "This shipped in v7.13.70 with no other changes."
    protected = ste._protected_spans(text)
    sentences = ste._split_sentences(text, protected)
    assert len(sentences) == 1


def test_sentence_split_ignores_dotted_module_path():
    text = "The write path lives in api.tasks.py near the top."
    protected = ste._protected_spans(text)
    sentences = ste._split_sentences(text, protected)
    assert len(sentences) == 1


def test_sentence_split_still_splits_real_sentences():
    text = "First sentence here. Second sentence here."
    protected = ste._protected_spans(text)
    sentences = ste._split_sentences(text, protected)
    assert len(sentences) == 2
    assert sentences[0][0] == "First sentence here."
    assert sentences[1][0] == "Second sentence here."


# ----------------------------------------------------------------------
# apply() and style_block()
# ----------------------------------------------------------------------


def test_apply_normalizes_then_checks():
    text = "We don't do this in order to be robust; it's fine."
    fixed_text, findings = ste.apply(text, mode="flavored")
    expected_fixed, _rules = ste.normalize(text, mode="flavored")
    assert fixed_text == expected_fixed
    assert findings == ste.check(expected_fixed, mode="flavored")


def test_style_block_shape():
    fields = {
        "title": (["contraction"], []),
        "description": ([], [
            ste.Finding(rule="passive-voice", message="msg", start=0,
                        end=3, excerpt="was"),
        ]),
        "oracle": ([], []),
    }
    block = ste.style_block(fields)
    assert block["fixed"] == {"title": ["contraction"]}
    assert block["findings"] == [{
        "field": "description",
        "rule": "passive-voice",
        "message": "msg",
        "excerpt": "was",
    }]
    assert "oracle" not in block["fixed"]


# ----------------------------------------------------------------------
# TaskService wiring
# ----------------------------------------------------------------------


def test_create_normalizes_description(tmp_path):
    svc = _mk_service(tmp_path)
    text = "We don't do this in order to be robust; it's fine."
    task = svc.create(title="Wiring check", description=text)

    expected, expected_rules = ste.normalize(text, mode="flavored")
    assert task.description == expected

    assert "description" in svc.last_style["fixed"]
    assert set(svc.last_style["fixed"]["description"]) == set(expected_rules)

    rows = svc.history(task.id)
    actions = [h.action for h in rows]
    assert "ste_normalise" in actions


def test_create_never_rewrites_plan_doc(tmp_path):
    svc = _mk_service(tmp_path)
    plan_doc = "```\ndon't;\n```"
    task = svc.create(title="Plan doc check", plan_doc=plan_doc)
    assert task.plan_doc == plan_doc


def test_update_normalizes_description_and_records_history(tmp_path):
    svc = _mk_service(tmp_path)
    task = svc.create(title="Wiring check", description="Starter text.")

    text = "We don't do this in order to be robust; it's fine."
    updated = svc.update(task.id, description=text)

    expected, expected_rules = ste.normalize(text, mode="flavored")
    assert updated.description == expected
    assert "description" in svc.last_style["fixed"]
    assert set(svc.last_style["fixed"]["description"]) == set(expected_rules)

    rows = svc.history(task.id)
    actions = [h.action for h in rows]
    assert "ste_normalise" in actions


def test_update_never_rewrites_plan_doc(tmp_path):
    svc = _mk_service(tmp_path)
    task = svc.create(title="Plan doc check")
    plan_doc = "```\ndon't;\n```"
    updated = svc.update(task.id, plan_doc=plan_doc)
    assert updated.plan_doc == plan_doc


# ----------------------------------------------------------------------
# Trailing punctuation after a path or URL belongs to the sentence
# (found live on 7.13.72: "Endpoints.cs; each" kept its semicolon
# because the path mask swallowed the ";").
# ----------------------------------------------------------------------


def test_semicolon_after_a_path_becomes_a_sentence_break():
    from prism_service.services import ste

    text = "Routes defined in Features/*/Endpoints.cs; each delegates to a Handler.cs. Don't use controllers."
    fixed, rules = ste.normalize(text, "flavored")
    assert "Features/*/Endpoints.cs. Each delegates to a Handler.cs." in fixed, fixed
    assert "Do not use controllers." in fixed
    assert "semicolon" in rules and "contraction" in rules
    assert not [f for f in ste.check(fixed, "flavored") if f.rule == "semicolon"]


def test_punctuation_after_a_url_is_not_protected():
    from prism_service.services import ste

    text = "See https://example.org/docs/a.html; then run it."
    fixed, rules = ste.normalize(text, "flavored")
    assert "https://example.org/docs/a.html. Then run it." in fixed, fixed
    assert "semicolon" in rules
    # The URL itself is still byte-identical and a contraction inside it survives.
    text2 = "Open https://example.org/don't/x, it's the entry point."
    fixed2, _ = ste.normalize(text2, "flavored")
    assert "https://example.org/don't/x" in fixed2 and "it is the entry point" in fixed2
