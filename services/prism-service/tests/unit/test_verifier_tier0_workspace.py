"""Tier0 full-suite pytest workspace tests (task 271dacc9).

Green-gate Tier0 must run the suite from the SERVICE workspace, not the
repo root. At the repo root, pytest falls back to full rootdir collection
(the root pyproject's testpaths no longer exists) and walks the fork data
dir (data-selfimprove/projects/prism/graphify-src/** — a stale full repo
mirror), where duplicate ``tests.*`` module basenames collide ("import
file mismatch") and every collection ERRORS -> tier0=fail -> every
green_gate needs a manual distinct-actor override (latched 9f20b605 and
e70cdcda).

Contract encoded here:
  * AC-1 — the full-suite pytest run executes from the service suite root
    (derived from layout / running package, never a hardcoded path) and
    records its run cwd in the claim evidence.
  * AC-2 — a data-dir mirror with colliding tests modules no longer
    false-fails collection.
  * AC-3 — the verifier is NOT weakened: a real failing test in the
    service tree still fails Tier0 with exit 1 (test failure), not a
    collection error.
  * AC-4 — the resolved PRISM data dir is defensively --ignore'd when it
    lies inside the suite root.
  * AC-5 — ruff falls back to ``python -m ruff`` when the binary is not
    on PATH but the module is importable.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services.verifier_service import run_tier0   # noqa: E402


# ----------------------------------------------------------------------
# Layout builders — a temp git repo mimicking the fork's poisoned shape
# ----------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=str(cwd), capture_output=True, text=True, timeout=30,
    )


def _init_repo(ws: Path) -> None:
    """git repo with one commit so `git diff HEAD` resolves; everything
    written AFTERWARDS is untracked and lands in Tier0's diff scope."""
    ws.mkdir(parents=True, exist_ok=True)
    _git(ws, "init")
    (ws / "README.md").write_text("seed\n", encoding="utf-8")
    _git(ws, "add", "README.md")
    _git(ws, "commit", "-m", "seed")


def _write_service_tree(root: Path, failing: bool = False) -> None:
    """A minimal prism-service-shaped distribution: pyproject.toml +
    prism_service package + tests/."""
    (root / "prism_service").mkdir(parents=True, exist_ok=True)
    (root / "prism_service" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'svc'\nversion = '0'\n", encoding="utf-8")
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "test_suite_marker.py").write_text(
        "def test_real_suite_marker():\n    assert True\n", encoding="utf-8")
    if failing:
        (tests / "test_planted_failure.py").write_text(
            "def test_planted_failure():\n"
            "    assert False, 'planted red'\n", encoding="utf-8")


def _write_colliding_mirror(base: Path) -> None:
    """A stale repo mirror (graphify-src-style) whose rootless `tests`
    dir carries the SAME test module basename as the real suite —
    collected together they raise 'import file mismatch'."""
    mirror = (base / "projects" / "prism" / "graphify-src" /
              "services" / "prism-service" / "tests")
    mirror.mkdir(parents=True, exist_ok=True)
    (mirror / "test_suite_marker.py").write_text(
        "def test_real_suite_marker():\n    assert True\n", encoding="utf-8")


def _pytest_claim(claims):
    hits = [c for c in claims if c.kind == "tooling.pytest"]
    assert hits, ("Tier0 emitted no tooling.pytest claim — the full-suite "
                  "run never fired")
    return hits[0]


# ----------------------------------------------------------------------
# AC-1 — suite-root derivation (pure)
# ----------------------------------------------------------------------


def test_pytest_suite_root_prefers_service_layout(tmp_path):
    from prism_service.services.verifier_service import _pytest_suite_root
    ws = tmp_path / "repo"
    svc = ws / "services" / "prism-service"
    _write_service_tree(svc)
    assert _pytest_suite_root(ws) == svc.resolve()


def test_pytest_suite_root_falls_back_to_workspace(tmp_path):
    from prism_service.services.verifier_service import _pytest_suite_root
    ws = tmp_path / "generic"
    ws.mkdir()
    (ws / "pyproject.toml").write_text("[project]\nname='g'\nversion='0'\n",
                                       encoding="utf-8")
    assert _pytest_suite_root(ws) == ws.resolve()


# ----------------------------------------------------------------------
# AC-2 — the fork's poisoned layout must not false-fail Tier0
# ----------------------------------------------------------------------


def test_tier0_green_despite_datadir_mirror_collision(tmp_path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    # repo root is python-detected (like the real fork root pyproject)
    (ws / "pyproject.toml").write_text(
        "[project]\nname = 'repo'\nversion = '0'\n", encoding="utf-8")
    _write_service_tree(ws / "services" / "prism-service")
    _write_colliding_mirror(ws / "data-selfimprove")

    claim = _pytest_claim(run_tier0(ws))
    assert claim.status == "pass", (
        f"collection-explosion layout false-failed Tier0: {claim.feedback} "
        f"/ {claim.evidence.get('stdout', '')[:400]}")
    # AC-1 teeth: the claim proves WHERE it ran
    cwd = str(claim.evidence.get("cwd", ""))
    assert cwd.replace("\\", "/").endswith("services/prism-service"), (
        f"full suite did not run from the service suite root (cwd={cwd!r})")


# ----------------------------------------------------------------------
# AC-3 — verifier NOT weakened: real failures still fail, exit 1
# ----------------------------------------------------------------------


def test_tier0_fails_on_real_test_failure_not_collection_noise(tmp_path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    (ws / "pyproject.toml").write_text(
        "[project]\nname = 'repo'\nversion = '0'\n", encoding="utf-8")
    _write_service_tree(ws / "services" / "prism-service", failing=True)
    _write_colliding_mirror(ws / "data-selfimprove")

    claim = _pytest_claim(run_tier0(ws))
    assert claim.status == "fail", (
        "a REAL failing test in the service tree must fail Tier0 "
        f"(got {claim.status})")
    # exit 1 = tests ran and failed; exit 2 would be the collection
    # explosion masking the real signal.
    assert claim.evidence.get("exit_code") == 1, (
        f"expected a real test failure (exit 1), got "
        f"exit={claim.evidence.get('exit_code')} — collection error?")
    assert "test_planted_failure" in claim.evidence.get("stdout", ""), (
        "the planted failing test never ran — Tier0 failed for the wrong "
        "reason (collection noise, not the red test)")


# ----------------------------------------------------------------------
# AC-4 — resolved data dir INSIDE the suite root is defensively ignored
# ----------------------------------------------------------------------


def test_data_dir_inside_suite_root_is_ignored(tmp_path, monkeypatch):
    ws = tmp_path / "svc-root"
    _init_repo(ws)
    _write_service_tree(ws)                      # workspace IS the suite root
    _write_colliding_mirror(ws / "datadir")      # data dir inside the repo
    monkeypatch.setenv("PRISM_DATA_DIR", str(ws / "datadir"))

    claim = _pytest_claim(run_tier0(ws))
    assert claim.status == "pass", (
        "a data dir INSIDE the suite root poisoned collection: "
        f"{claim.feedback} / {claim.evidence.get('stdout', '')[:400]}")


# ----------------------------------------------------------------------
# AC-5 — ruff module fallback (no more 'ruff not installed' skips)
# ----------------------------------------------------------------------


def test_ruff_cmd_module_fallback(monkeypatch):
    import importlib.util
    import shutil as _shutil
    from prism_service.services import verifier_service as vs

    # on PATH -> bare binary
    monkeypatch.setattr(_shutil, "which",
                        lambda name: "ruff" if name == "ruff" else None)
    assert vs._ruff_cmd() == ["ruff"]

    # not on PATH but importable -> python -m ruff
    monkeypatch.setattr(_shutil, "which", lambda name: None)
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name: object() if name == "ruff" else None)
    assert vs._ruff_cmd() == [sys.executable, "-m", "ruff"]

    # neither -> None (claim stays 'skipped')
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert vs._ruff_cmd() is None
