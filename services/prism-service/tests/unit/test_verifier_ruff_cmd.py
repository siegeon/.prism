"""Regression tests for task 43a44af6: the Tier0 ruff lane must light up
whenever the ruff package is present in the daemon env — PATH exe OR
importable module — and only report skipped when neither resolves.
"""
from __future__ import annotations

import sys

from prism_service.services import verifier_service as vs


def test_ruff_cmd_prefers_path_exe(monkeypatch):
    monkeypatch.setattr(vs.shutil, "which", lambda name: "C:/tools/ruff.exe")
    assert vs._ruff_cmd() == ["ruff"]


def test_ruff_cmd_falls_back_to_module(monkeypatch):
    monkeypatch.setattr(vs.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        vs.importlib.util, "find_spec", lambda name: object() if name == "ruff" else None
    )
    assert vs._ruff_cmd() == [sys.executable, "-m", "ruff"]


def test_ruff_cmd_none_when_absent(monkeypatch):
    monkeypatch.setattr(vs.shutil, "which", lambda name: None)
    monkeypatch.setattr(vs.importlib.util, "find_spec", lambda name: None)
    assert vs._ruff_cmd() is None


def test_lane_emits_skipped_claim_when_ruff_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(vs.shutil, "which", lambda name: None)
    monkeypatch.setattr(vs.importlib.util, "find_spec", lambda name: None)
    claims = vs._run_python_tools(tmp_path, ["foo.py"])
    ruff_claims = [c for c in claims if c.kind == "tooling.ruff"]
    assert len(ruff_claims) == 1
    assert ruff_claims[0].status == "skipped"
