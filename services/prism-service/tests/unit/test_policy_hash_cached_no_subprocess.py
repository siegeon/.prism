"""control_plane.policy_hash / oracle_spec.current_tree_sha stop shelling
out to git on a repeat call for the SAME commit (tick-cost pass, external
fixer, owner brief 2026-09-13, no PRISM ticket -- the last ~110ms of
GET /api/work/graph, one pending-gate node's oracle-evidence check: 6
`git show` subprocess calls per policy_hash() plus one `git rev-parse HEAD`
per current_tree_sha(), both re-run from scratch every poll).

_fast_head_sha reads .git/HEAD (and the ref it points at) straight off the
filesystem; policy_hash memoizes on (PRISM_VERSION, resolved sha,
repo_root) so the SAME commit's policy content is hashed once per process,
not once per poll -- correctness holds because the cache key is the
commit's own resolved sha, never a moving ref name."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def _git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    for rel in (
        "services/prism-service/prism_service/services/governance_rubrics.yaml",
        "services/prism-service/prism_service/services/arc_governance.py",
        "services/prism-service/prism_service/services/verifier_service.py",
        "services/prism-service/prism_service/services/oracle_spec.py",
        "services/prism-service/prism_service/services/conductor_service.py",
        "services/prism-service/prism_service/services/control_plane.py",
    ):
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x: 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    return repo


def test_fast_head_sha_matches_git_rev_parse(tmp_path):
    from prism_service.services.control_plane import _fast_head_sha
    repo = _git_repo(tmp_path)
    real = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
        check=True).stdout.strip()
    assert _fast_head_sha(repo) == real


def test_a_second_policy_hash_call_for_the_same_sha_makes_no_subprocess(
        tmp_path, monkeypatch):
    import prism_service.services.control_plane as cp
    cp._POLICY_HASH_CACHE.clear()
    repo = _git_repo(tmp_path)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
        check=True).stdout.strip()

    first = cp.policy_hash(sha, repo)
    assert first and first.startswith("sha256:")

    calls = []
    real_run = subprocess.run

    def _spy(*a, **k):
        calls.append((a, k))
        return real_run(*a, **k)

    monkeypatch.setattr(subprocess, "run", _spy)
    second = cp.policy_hash(sha, repo)
    assert second == first
    assert not calls, f"expected zero subprocess calls on cache hit: {calls}"


def test_current_tree_sha_makes_no_subprocess_on_a_normal_worktree(
        tmp_path, monkeypatch):
    from prism_service.services import oracle_spec
    repo = _git_repo(tmp_path)
    real = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
        check=True).stdout.strip()

    calls = []
    real_run = subprocess.run

    def _spy(*a, **k):
        calls.append((a, k))
        return real_run(*a, **k)

    monkeypatch.setattr(subprocess, "run", _spy)
    got = oracle_spec.current_tree_sha(str(repo))
    assert got == real
    assert not calls, f"expected zero subprocess calls via the fast path: {calls}"
