"""premise_grounded accepts numbered claim lists, not only '-'/'*' bullets
(task 43cefc52).

_claim_lines (arc_governance.py:246-261) recognises ONLY '- '/'* ' bullets.
A driver who wrote a numbered list ('1. claim ...' / '1) claim ...') gets
zero entries back, so score_premise_grounded's `if not claims:` branch
fires the SAME 'section is present but empty' message a genuinely empty
section gets - indistinguishable, and wrong: the section was not empty,
the bullet form just was not recognised.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _gov():
    from prism_service.services import arc_governance
    return arc_governance


def _rubric():
    return _gov().load_rubrics()["premise_grounded"]


CITATION = "services/prism-service/.github/workflows/unit-tests.yml:12"

DOT_LIST_GROUNDED = (
    "# Review notes\n\n## Premises\n"
    f"1. The failing lane is unit-tests.yml — {CITATION}\n")

PAREN_LIST_GROUNDED = (
    "# Review notes\n\n## Premises\n"
    f"1) The failing lane is unit-tests.yml — {CITATION}\n")

MIXED_DASH_AND_NUMBERED_GROUNDED = (
    "# Review notes\n\n## Premises\n"
    f"- The failing lane is unit-tests.yml — {CITATION}\n"
    "2. No Testcontainers config exists in the repo — UNVERIFIED, "
    "needs a follow-up grep\n")

DOT_LIST_UNGROUNDED = (
    "# Review notes\n\n## Premises\n"
    "1. The CI lanes are misconfigured somehow, needs a closer look\n")

GENUINELY_EMPTY_SECTION = "# Review notes\n\n## Premises\n\n"

PROSE_ONLY_SECTION = (
    "# Review notes\n\n## Premises\n"
    "The CI infrastructure looked stable during this review, nothing "
    "alarming stood out on a first pass.\n")


# ── AC-1: numbered lists are recognised identically to '-'/'*' ──────────

def test_numbered_dot_list_with_citation_is_accepted():
    res = _gov().score_premise_grounded(
        {"notes_md": DOT_LIST_GROUNDED}, _rubric())
    assert res["ok"] is True, res


def test_numbered_paren_list_with_citation_is_accepted():
    res = _gov().score_premise_grounded(
        {"notes_md": PAREN_LIST_GROUNDED}, _rubric())
    assert res["ok"] is True, res


def test_dash_and_numbered_bullets_mix_freely_in_one_section():
    res = _gov().score_premise_grounded(
        {"notes_md": MIXED_DASH_AND_NUMBERED_GROUNDED}, _rubric())
    assert res["ok"] is True, res
    assert "2 claim" in res["reason"], (
        "both the dash bullet and the numbered bullet must count as "
        f"separate claims: {res}")


# ── AC-2: an ungrounded NUMBERED claim names the real reason ────────────

def test_ungrounded_numbered_claim_names_the_citation_gap_not_empty():
    res = _gov().score_premise_grounded(
        {"notes_md": DOT_LIST_UNGROUNDED}, _rubric())
    assert res["ok"] is False, res
    reason = res["reason"].lower()
    assert "present but empty" not in reason, (
        "an ungrounded NUMBERED claim must not be reported as an empty "
        f"section: {res}")
    assert "citation" in reason or "marker" in reason, res


# ── AC-3: empty vs unrecognised-form are DISTINCT refusals ──────────────

def test_genuinely_empty_section_reports_empty():
    res = _gov().score_premise_grounded(
        {"notes_md": GENUINELY_EMPTY_SECTION}, _rubric())
    assert res["ok"] is False, res
    assert "present but empty" in res["reason"].lower(), res


def test_prose_only_section_is_refused_for_a_different_reason_than_empty():
    empty_res = _gov().score_premise_grounded(
        {"notes_md": GENUINELY_EMPTY_SECTION}, _rubric())
    prose_res = _gov().score_premise_grounded(
        {"notes_md": PROSE_ONLY_SECTION}, _rubric())
    assert prose_res["ok"] is False, prose_res
    assert prose_res["reason"] != empty_res["reason"], (
        "a section with unbulleted PROSE must not be reported with the "
        "identical message as a section with NOTHING under the heading - "
        f"empty={empty_res!r} prose={prose_res!r}")
    assert "present but empty" not in prose_res["reason"].lower(), (
        f"prose-only content is not the same failure as empty: {prose_res}")


# ── AC-4: over-widened matching must not eat trailing prose ─────────────

def test_prose_paragraph_does_not_get_folded_into_a_bogus_new_claim():
    """likely_misfire: an over-widened bullet regex could start treating an
    ordinary paragraph line as a fresh claim entry instead of folding it
    the way plain prose always has - which would report a citation gap for
    text the driver never intended as a claim at all."""
    notes = (
        "# Review notes\n\n## Premises\n"
        f"1. The failing lane is unit-tests.yml — {CITATION}\n\n"
        "This is just narrative context, not a second claim, and it is "
        "not itself bulleted or numbered.\n")
    res = _gov().score_premise_grounded({"notes_md": notes}, _rubric())
    assert res["ok"] is True, (
        "a trailing unbulleted prose paragraph must not turn into a "
        f"second, ungrounded claim entry: {res}")


# ── AC-5: a citation as a NESTED sub-bullet folds into its claim ────────

def test_citation_as_a_nested_sub_bullet_folds_into_the_claim_above_it():
    """Same bug class as _ac_lines (task 3a3f90da, 2026-08-26): a citation
    written as its own indented child bullet under the claim, rather than
    on the same line, must fold into that claim - not become a second,
    untracked entry that leaves the parent claim looking ungrounded."""
    notes = (
        "# Review notes\n\n## Premises\n"
        "- The failing lane is unit-tests.yml\n"
        f"  - {CITATION}\n"
    )
    claims = _gov()._claim_lines(
        _gov()._find_section(_gov()._sections(notes), "premises") or "")
    assert len(claims) == 1, (
        f"the nested citation bullet must fold into the claim above it, "
        f"not become its own entry: {claims!r}")
    assert CITATION in claims[0]

    res = _gov().score_premise_grounded({"notes_md": notes}, _rubric())
    assert res["ok"] is True, res
