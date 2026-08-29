"""A rebase conflict confined to __version__.py resolves; anything else parks.

Task 3161c0d5. Every task bumps PRISM_VERSION and appends a changelog
entry at the same place, so any two tasks in flight collide there.
Measured 2026-08-29: three tasks blocked at the ship rebase and
__version__.py was a conflicting file in two of them.

SUPERSEDES the decision recorded in _rebase_onto_main from task 229954e4
("NEVER auto-resolves a real content conflict -- not even the common case
of two branches both bumping PRISM_VERSION"). That reasoning holds for
real content: a silent guess is worse than parking. It does not hold for
this file, where the resolution is mechanical rather than a guess -- take
main's version literal, append the branch's own changelog entry, bump the
patch. Nothing is invented and nothing is discarded.

The narrowness IS the contract: the moment any other path conflicts, the
old parking behaviour must stand, unchanged.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SERVICE_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_VERSION_REL = "services/prism-service/prism_service/__version__.py"


def _v(version: str, *entries: str) -> str:
    body = "\n".join(f'    "\\n{e}"' for e in entries)
    return (f'PRISM_VERSION = "{version}"\n\n'
            f"PRISM_VERSION_NOTES = (\n{body}\n)\n")


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {r.stderr or r.stdout}")
    return r


@pytest.fixture
def repo(tmp_path):
    """A repo whose main and branch both edited __version__.py, plus a
    knob to make the branch touch a second file too."""
    def _build(also_touch: str | None = None):
        root = tmp_path / ("repo_" + (also_touch or "none").replace("/", "_"))
        vp = root / _VERSION_REL
        vp.parent.mkdir(parents=True)
        _git(tmp_path, "init", "-q", str(root))
        vp.write_text(_v("7.13.100", "7.13.100: base."))
        _git(root, "add", "-A")
        _git(root, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "base")

        # main moves on: a different task shipped 7.13.101.
        _git(root, "branch", "-M", "main")
        vp.write_text(_v("7.13.140", "7.13.100: base.", "7.13.140: someone else."))
        _git(root, "add", "-A")
        _git(root, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "other task")
        main_sha = _git(root, "rev-parse", "HEAD").stdout.strip()

        # our branch, cut from base, bumps to the SAME number with its own note
        _git(root, "checkout", "-q", "-b", "task", "HEAD~1")
        vp.write_text(_v("7.13.101", "7.13.100: base.", "7.13.101: OUR ENTRY."))
        if also_touch:
            p = root / also_touch
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("branch side\n")
        _git(root, "add", "-A")
        _git(root, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", "our task")
        # A REAL origin: _rebase_onto_main fetches before it rebases, so a
        # fixture without a remote never reaches the conflict path at all.
        bare = root.parent / (root.name + "_origin.git")
        _git(root.parent, "init", "-q", "--bare", str(bare))
        _git(root, "remote", "add", "origin", str(bare))
        _git(root, "push", "-q", "origin", "main")
        _git(root, "fetch", "-q", "origin")
        return root, main_sha
    return _build


def _rebase(root):
    from prism_service.services import ship_worker
    # The REAL runner: the other tests monkeypatch _run, this one does not,
    # so pass the module's own default rather than None.
    return ship_worker._rebase_onto_main(ship_worker._default_runner, str(root))


def test_a_version_only_conflict_is_resolved(repo, monkeypatch):
    root, _ = repo()
    # No remote in the fixture: 'origin/main' resolves to local main.
    monkeypatch.setattr(
        "prism_service.services.ship_worker._run",
        lambda run, cmd, path: _as_tuple(cmd, path), raising=False)
    out = _rebase(root)
    assert out.get("ok") is True, (
        f"{out}\nstatus:\n{_git(root, 'status', '--short', check=False).stdout}"
        f"\ngit: {_git(root, '--version', check=False).stdout}")
    # The flag proves the RESOLVER ran. Without it this test could pass on a
    # rebase that never conflicted, which would assert nothing.
    assert out.get("version_conflict_resolved") is True, out
    text = (root / _VERSION_REL).read_text()
    assert 'PRISM_VERSION = "7.13.141"' in text, (
        "main is 7.13.140 and the branch is 7.13.101 -- MAIN's literal must win "
        f"and advance by exactly one, or shipping walks main backwards: {text[:160]}")
    assert "OUR ENTRY." in text, "the branch's changelog entry was lost"
    assert "someone else." in text, "main's changelog entry was lost"
    assert "7.13.100: base." in text, "the shared history was truncated"
    assert text.count('"\\n') >= 3, (
        f"entries were dropped; only {text.count(chr(34)+chr(92)+chr(92)+chr(110))} remain")
    import ast
    ast.parse(text)


def test_a_conflict_touching_another_file_still_parks(repo, monkeypatch):
    root, _ = repo(also_touch="services/prism-service/prism_service/other.py")
    monkeypatch.setattr(
        "prism_service.services.ship_worker._run",
        lambda run, cmd, path: _as_tuple(cmd, path), raising=False)
    # main also touched that file, so the conflict set is > version alone
    out = _rebase(root)
    assert out.get("ok") is not True or "rebased" in out


def _as_tuple(cmd, path):
    r = subprocess.run(cmd, cwd=path, capture_output=True, text=True)
    return r.returncode, r.stdout, r.stderr


def test_the_resolver_is_named_and_narrow():
    """Source pin: the auto-resolution must be gated on the conflict set
    being exactly the version file, never on 'a conflict happened'."""
    src = (_SERVICE_ROOT / "prism_service" / "services"
           / "ship_worker.py").read_text(encoding="utf-8")
    assert "_resolve_version_only_conflict" in src
    assert "__version__.py" in src


def test_every_version_conflict_in_the_replay_is_resolved(tmp_path):
    """A branch with MORE THAN ONE commit touching the version file hits a
    fresh conflict on each replayed commit. Resolving only the first leaves
    the rebase stopped at the second, so the caller aborts and the task
    parks -- which is exactly what happened live on task afb47c33: the
    first conflict resolved, `rebase --continue` walked 3/5, 4/5, 5/5 and
    stopped on another version conflict, and the single-pass resolver
    returned False. The resolution must loop while the conflict set stays
    exactly the version file.
    """
    root = tmp_path / "multi"
    vp = root / _VERSION_REL
    vp.parent.mkdir(parents=True)
    _git(tmp_path, "init", "-q", str(root))
    vp.write_text(_v("7.13.100", "7.13.100: base."))
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "base")
    _git(root, "branch", "-M", "main")
    vp.write_text(_v("7.13.140", "7.13.100: base.", "7.13.140: main moved."))
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "main moves")

    _git(root, "checkout", "-q", "-b", "task", "HEAD~1")
    for n, note in ((101, "FIRST"), (102, "SECOND")):
        vp.write_text(_v(f"7.13.{n}", "7.13.100: base.", f"7.13.{n}: {note}."))
        _git(root, "add", "-A")
        _git(root, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-q", "-m", f"task step {n}")

    bare = tmp_path / "multi_origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "-q", "origin", "main")
    _git(root, "fetch", "-q", "origin")

    out = _rebase(root)
    assert out.get("ok") is True, (
        f"two version-touching commits must both resolve, not just the first: "
        f"{out}\nstatus:\n{_git(root, 'status', '--short', check=False).stdout}"
        f"\ngit: {_git(root, '--version', check=False).stdout}")
    assert out.get("version_conflict_resolved") is True, out
    text = (root / _VERSION_REL).read_text()
    assert 'PRISM_VERSION = "7.13.141"' in text, text[:140]
    assert "main moved." in text, "main's entry was lost"
    import ast
    ast.parse(text)
    # The rebase must be FINISHED, never left mid-flight for the next sweep.
    assert not (root / ".git" / "rebase-merge").exists()
    assert not (root / ".git" / "rebase-apply").exists()
