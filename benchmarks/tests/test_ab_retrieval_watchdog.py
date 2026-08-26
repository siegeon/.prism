"""Gate suite for task 39244a32: the retrieval benchmark finishes instead of wedging.

Every test here runs the REAL `ab_retrieval.py run` as a subprocess against a
3-file fixture repo. The stall is injected on the harness's own call path via
the documented --candidate hook (benchmarks/tests/ab_stall_candidate.py), never
by a sleep inside a test (misfire #4 of the task). Expected on a trip: a
STALLED banner naming corpus+stage, a faulthandler all-threads dump, exit 3.
"""

from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "benchmarks" / "graft_parity" / "ab_retrieval.py"
STALL_CANDIDATE = "benchmarks.tests.ab_stall_candidate:block_forever"
BANNER = re.compile(
    r"^STALLED corpus=.+ stage=arm:candidate tool=brain_search "
    r"(case|file)=\d+/\d+ waited=\d+s$",
    re.MULTILINE,
)

FIXTURE_FILES = {
    "alpha.go": "package fixture\n\nfunc Alpha() int {\n\treturn 1\n}\n",
    "beta.go": "package fixture\n\nfunc Beta() int {\n\treturn 2\n}\n",
    "gamma.go": "package fixture\n\nfunc Gamma() int {\n\treturn 3\n}\n",
}


@pytest.fixture(scope="module")
def fx(tmp_path_factory):
    """A tiny 3-file .go repo plus a matching cases.json -- no network, no clone."""
    repo = tmp_path_factory.mktemp("ab_watchdog_repo")
    for name, content in FIXTURE_FILES.items():
        (repo / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "fixture"],
        cwd=repo, check=True,
    )
    cases = [
        {"sha": "fixture", "query": "Alpha", "gold_files": ["alpha.go"]},
        {"sha": "fixture", "query": "Beta", "gold_files": ["beta.go"]},
        {"sha": "fixture", "query": "Gamma", "gold_files": ["gamma.go"]},
    ]
    cases_path = tmp_path_factory.mktemp("ab_watchdog_cases") / "cases.json"
    cases_path.write_text(json.dumps(cases), encoding="utf-8")
    return repo, cases_path


def _run_harness(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, str(HARNESS), *args],
        cwd=ROOT, env=env, timeout=timeout,
        capture_output=True, text=True,
    )


def test_stalled_call_exits_nonzero(fx):
    repo, cases_path = fx
    t0 = time.monotonic()
    try:
        proc = _run_harness([
            "run", "--repo", str(repo), "--cases", str(cases_path),
            "--stall-timeout", "3",
            "--candidate", STALL_CANDIDATE,
            "--suffix", ".go", "--domain", "go",
        ], timeout=60)
    except subprocess.TimeoutExpired as exc:
        pytest.fail(
            f"harness hung past the 60s safety timeout instead of tripping "
            f"its own stall watchdog: {exc}")
        return
    wall = time.monotonic() - t0
    assert wall < 60, (
        f"harness took {wall:.1f}s, expected the watchdog to trip well inside it")
    assert proc.returncode not in (0, 1), (
        f"expected a stall-specific nonzero exit (not 0, not a bare error 1), "
        f"got {proc.returncode}\nstderr:\n{proc.stderr}")
    assert BANNER.search(proc.stderr), (
        f"expected a STALLED banner naming corpus+stage+tool+position+wait on "
        f"stderr, got:\n{proc.stderr}")
    assert "Thread 0x" in proc.stderr or "Current thread" in proc.stderr, (
        f"expected a faulthandler all-threads dump on stderr, got:\n{proc.stderr}")
    assert "block_forever" in proc.stderr, (
        f"expected the injected stall's own frame (block_forever) in the "
        f"thread dump, got:\n{proc.stderr}")


def test_progress_does_not_trip(fx, tmp_path):
    repo, cases_path = fx
    output = tmp_path / "result.json"
    proc = _run_harness([
        "run", "--repo", str(repo), "--cases", str(cases_path),
        "--stall-timeout", "3",
        "--suffix", ".go", "--domain", "go",
        "--output", str(output),
    ], timeout=60)
    assert proc.returncode == 0, (
        f"a run that makes real progress must not trip the watchdog, got "
        f"rc={proc.returncode}\nstderr:\n{proc.stderr}")
    assert "STALLED" not in proc.stderr, f"unexpected STALLED banner:\n{proc.stderr}"
    assert output.exists(), f"expected a result json at {output}"
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema"] == "prism.graft_parity_ab.v1"


def test_stall_timeout_default_is_on():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from benchmarks.graft_parity import ab_retrieval

    build_parser = getattr(ab_retrieval, "build_parser", None)
    assert build_parser is not None, (
        "ab_retrieval.py must expose build_parser() so --stall-timeout's "
        "default is checkable without actually running a harness")
    parser = build_parser()
    parsed = parser.parse_args(["run", "--repo", "x", "--cases", "y"])
    assert parsed.stall_timeout > 0

    help_text = subprocess.run(
        [sys.executable, str(HARNESS), "run", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=20,
    ).stdout
    assert "--stall-timeout" in help_text
    assert "disable" in help_text.lower()


def test_no_product_code_changed():
    try:
        base = subprocess.run(
            ["git", "merge-base", "HEAD", "origin/main"],
            cwd=ROOT, capture_output=True, text=True, timeout=20, check=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        pytest.skip(f"git merge-base unavailable in this checkout: {exc}")
    diff = subprocess.run(
        ["git", "diff", "--name-only", f"{base}..HEAD"],
        cwd=ROOT, capture_output=True, text=True, timeout=20, check=True,
    ).stdout
    touched_product = [
        line for line in diff.splitlines()
        if line.startswith("services/prism-service/prism_service/")
    ]
    assert not touched_product, (
        f"this task may not touch product code, but the diff includes: "
        f"{touched_product}")


def test_no_sleep_in_tests():
    source = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "sleep"
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "time"):
                offenders.append(node.name)
    assert not offenders, f"time.sleep() found inside test body of: {offenders}"
