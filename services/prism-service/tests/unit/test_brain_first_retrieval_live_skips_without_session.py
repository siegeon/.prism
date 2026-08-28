"""AC-2..AC-5 in test_implement_brain_first_retrieval_live.py SKIP (not
FAIL) when PRISM_BRAIN_FIRST_DRIVE_SESSION is unset, instead of permanently
red-gating every PR's CI forever.

Root cause, live: those tests can ONLY be satisfied by a real fresh
`implement` drive's transcripts (env-var-located), which GitHub Actions CI
structurally cannot provide -- confirmed on PR #2350 (task 85f92e4b, run
33044282088) and independently on PR #2366 (task 9b0f7c4b): both blocked
on the SAME 4 tests, same reason, unrelated to either PR's own change.
NFR-3 (task 3a3f90da) deliberately chose a loud AssertionError over a
silent skip specifically to forbid a VACUOUS PASS -- that intent is
preserved here: a skip is neither a pass nor a false claim of success, it
is pytest's own honest "this check cannot run here" outcome, and it still
carries the exact diagnostic reason in the CI log. Only the "env var never
set" path changes; a MISCONFIGURED opt-in (session set but transcripts
missing) still fails loudly, unchanged.
"""

from __future__ import annotations

import os

import pytest


def test_missing_session_env_var_skips_not_fails(monkeypatch):
    monkeypatch.delenv("PRISM_BRAIN_FIRST_DRIVE_SESSION", raising=False)
    from tests.unit.test_implement_brain_first_retrieval_live import (
        _locate_drive_transcripts,
    )
    with pytest.raises(pytest.skip.Exception) as exc:
        _locate_drive_transcripts()
    assert "PRISM_BRAIN_FIRST_DRIVE_SESSION is not set" in str(exc.value)
    assert "3a3f90da" in str(exc.value), (
        "the skip reason must still name the NFR-3 provenance, matching "
        "the original AssertionError's diagnostic text")


def test_missing_transcript_root_still_fails_loudly(monkeypatch, tmp_path):
    """A deliberate opt-in (session set) with a bad root is a real
    misconfiguration -- NFR-3's vacuous-pass protection must still apply
    here, unchanged."""
    monkeypatch.setenv("PRISM_BRAIN_FIRST_DRIVE_SESSION", "fake-session")
    monkeypatch.setenv("PRISM_BRAIN_FIRST_TRANSCRIPT_ROOT",
                       str(tmp_path / "does-not-exist"))
    from tests.unit.test_implement_brain_first_retrieval_live import (
        _locate_drive_transcripts,
    )
    with pytest.raises(AssertionError, match="does not exist"):
        _locate_drive_transcripts()


def test_no_transcripts_found_still_fails_loudly(monkeypatch, tmp_path):
    monkeypatch.setenv("PRISM_BRAIN_FIRST_DRIVE_SESSION", "fake-session")
    monkeypatch.setenv("PRISM_BRAIN_FIRST_TRANSCRIPT_ROOT", str(tmp_path))
    from tests.unit.test_implement_brain_first_retrieval_live import (
        _locate_drive_transcripts,
    )
    with pytest.raises(AssertionError, match="No transcripts found"):
        _locate_drive_transcripts()
