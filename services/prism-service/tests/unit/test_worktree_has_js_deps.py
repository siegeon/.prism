"""Worktree JS deps — task 0384b04d.

The conductor gives every task its own git worktree
(`task_workspace.ensure_workspace`), but `node_modules` is gitignored, so a
fresh worktree has zero JS dependencies. Any UI slice that runs this repo's
REAL typecheck (`npm run build`, i.e. `tsc -b && vite build` — `tsc --noEmit`
is a known false green here) fails immediately with "'tsc' is not
recognized". `ensure_workspace` now LINKS (never copies) the main checkout's
web `node_modules` into every fresh worktree it creates.

  AC-1  the linked node_modules RESOLVES to real file content in the main
        checkout's tree, not merely a path that "exists" - oracle: reading
        a marker file through the worktree's linked node_modules returns
        the exact bytes present in the main checkout's node_modules.
  AC-2  the link is LIVE, not a one-time snapshot/copy - oracle: a file
        written into the main checkout's node_modules AFTER the worktree
        was created is visible through the worktree's node_modules.
  AC-3  web_dist is never entangled - oracle: a sentinel written into the
        worktree's web_dist does not appear in the main checkout's
        web_dist, and vice versa.
  AC-4  worktree creation does not regress when the main checkout has no
        node_modules yet - oracle: ensure_workspace still succeeds with no
        link created.
  AC-5  removing a worktree through remove_workspace never destroys the
        LINK TARGET's contents - oracle: after ensure_workspace links
        node_modules and remove_workspace tears the worktree down, the
        main checkout's node_modules/.bin still exists with all its
        original shims. A naive recursive delete (git worktree remove
        --force is one; so is a bare shutil.rmtree/rm -rf) can FOLLOW a
        Windows junction and delete what it points to rather than just
        the link — confirmed empirically: it destroyed the real
        node_modules/.bin (96 shims) in the main checkout during
        verification of this fix.
"""
from __future__ import annotations

import importlib
import subprocess
from pathlib import Path

WEB = Path("services") / "prism-service" / "prism_service" / "web"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                    capture_output=True)


def _tw(tmp_path, monkeypatch):
    """Fresh task_workspace module bound to an isolated data dir, so each
    test gets its own task_workspaces/index.json rather than sharing the
    real dev data dir."""
    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    import prism_service.services.task_workspace as tw
    importlib.reload(tw)
    return tw


def _fake_main_repo(tmp_path: Path, with_node_modules: bool = True) -> Path:
    """A temp git repo shaped like the real PRISM checkout: committed
    web/package.json, plus UNTRACKED node_modules and web_dist (both are
    gitignored in the real repo, so both are absent from a fresh worktree
    checkout regardless — creating them untracked here reproduces that)."""
    repo = tmp_path / "main_repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@test")
    _git(repo, "config", "user.name", "t")
    web = repo / WEB
    web.mkdir(parents=True)
    (web / "package.json").write_text("{}\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "baseline")

    if with_node_modules:
        bin_dir = web / "node_modules" / ".bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "tsc-marker.txt").write_text("REAL_TSC_BINARY\n",
                                                  encoding="utf-8")

    dist = repo / WEB.parent / "web_dist"
    dist.mkdir(parents=True)
    (dist / "main-bundle.txt").write_text("MAIN_CHECKOUT_BUNDLE\n",
                                           encoding="utf-8")
    return repo


def test_fresh_worktree_links_resolvable_node_modules(tmp_path, monkeypatch):
    """AC-1 + AC-2."""
    tw = _tw(tmp_path, monkeypatch)
    repo = _fake_main_repo(tmp_path)

    rec = tw.ensure_workspace("wsdeps-task-1", repo_root=str(repo))
    ws = Path(rec["path"])

    linked_marker = ws / WEB / "node_modules" / ".bin" / "tsc-marker.txt"
    assert linked_marker.exists(), (
        "node_modules was not linked into the fresh worktree at all")
    assert linked_marker.read_text(encoding="utf-8") == "REAL_TSC_BINARY\n", (
        "the link exists but does not resolve to the real file content — "
        "a broken/malformed junction (path exists, content unreachable)")

    # AC-2: a file written into the SOURCE after the link was made must be
    # visible through the worktree's link — proves a live link, not a
    # one-time copy.
    (repo / WEB / "node_modules" / "new-dep.txt").write_text(
        "NEW_DEP\n", encoding="utf-8")
    seen_through_link = ws / WEB / "node_modules" / "new-dep.txt"
    assert seen_through_link.exists(), (
        "a file added to the source node_modules after worktree creation "
        "is invisible through the worktree's node_modules — this is a "
        "copy, not a link")
    assert seen_through_link.read_text(encoding="utf-8") == "NEW_DEP\n"

    tw.remove_workspace("wsdeps-task-1")


def test_worktree_build_output_never_entangled_with_main_web_dist(
        tmp_path, monkeypatch):
    """AC-3."""
    tw = _tw(tmp_path, monkeypatch)
    repo = _fake_main_repo(tmp_path)

    rec = tw.ensure_workspace("wsdeps-task-2", repo_root=str(repo))
    ws = Path(rec["path"])

    main_dist = repo / WEB.parent / "web_dist"
    ws_dist = ws / WEB.parent / "web_dist"

    # web_dist is gitignored, so it must not exist in the fresh worktree —
    # if it did, that would mean SOMETHING carried it over (a link or a
    # copy), which is exactly the hazard AC-3 forbids.
    assert not ws_dist.exists(), (
        "the worktree's web_dist already exists right after creation — "
        "it must be its own independent, not-yet-built directory")

    ws_dist.mkdir(parents=True)
    (ws_dist / "task-bundle.txt").write_text("TASK_BUILD_OUTPUT\n",
                                              encoding="utf-8")

    assert (main_dist / "main-bundle.txt").read_text(
        encoding="utf-8") == "MAIN_CHECKOUT_BUNDLE\n"
    assert not (main_dist / "task-bundle.txt").exists(), (
        "a worktree build leaked into the MAIN checkout's web_dist — a "
        "task build could mutate the bundle the running daemon serves")

    tw.remove_workspace("wsdeps-task-2")


def test_worktree_creation_survives_missing_source_node_modules(
        tmp_path, monkeypatch):
    """AC-4."""
    tw = _tw(tmp_path, monkeypatch)
    repo = _fake_main_repo(tmp_path, with_node_modules=False)

    rec = tw.ensure_workspace("wsdeps-task-3", repo_root=str(repo))
    ws = Path(rec["path"])

    assert ws.exists(), "ensure_workspace must still create the worktree"
    assert not (ws / WEB / "node_modules").exists(), (
        "no node_modules link should be created when the main checkout "
        "has none to link from")

    tw.remove_workspace("wsdeps-task-3")


def test_removing_worktree_never_destroys_main_node_modules(
        tmp_path, monkeypatch):
    """AC-5. THIS is the deliverable: the main checkout's node_modules/.bin
    must survive removal of a worktree that links to it. A naive recursive
    delete of the worktree tree can follow the Windows junction into the
    main checkout and delete what it finds there — that is not "the link
    disappeared", it is the SOURCE directory's real contents gone."""
    tw = _tw(tmp_path, monkeypatch)
    repo = _fake_main_repo(tmp_path)
    bin_dir = repo / WEB / "node_modules" / ".bin"
    # A handful of shims, standing in for the real npm-installed
    # tsc/vite/etc binaries a dev session depends on.
    for i in range(5):
        (bin_dir / f"shim{i}.cmd").write_text(f"SHIM_{i}\n", encoding="utf-8")
    before = sorted(p.name for p in bin_dir.glob("*"))

    rec = tw.ensure_workspace("wsdeps-task-4", repo_root=str(repo))
    ws = Path(rec["path"])
    assert (ws / WEB / "node_modules" / ".bin" / "shim0.cmd").exists(), (
        "sanity: the link must be in place before we can test removing it")

    tw.remove_workspace("wsdeps-task-4")

    assert bin_dir.exists(), (
        "the main checkout's node_modules/.bin was DELETED by tearing "
        "down a worktree that merely linked to it — removing a worktree "
        "must never reach into the link's target")
    after = sorted(p.name for p in bin_dir.glob("*"))
    assert after == before, (
        f"node_modules/.bin contents changed after worktree removal: "
        f"before={before} after={after}")
