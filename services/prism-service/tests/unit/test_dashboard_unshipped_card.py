"""Red tests for task b22576bb -- "Dashboard shows work that never shipped".

DONE means SHIPPED (owner 2026-07-16): merged and validated on main, not
merely gate-passed. Today nothing on the Dashboard tells a person that a
task marked `status=done` still sits on an unmerged branch. This slice adds
a stranded-work scan + Dashboard section for exactly that.

Backend (services/prism-service/prism_service/api/tasks.py): a NEW
`get_stranded_work` scan resolves shipped-ness via
`git log origin/main --grep="[task:<id8>]"` -- squash-safe, because it reads
commit MESSAGES on origin/main directly instead of walking ancestry.
`get_task_delivery`'s existing pipeline (:547-623) resolves "merged" via
`git merge-base --is-ancestor sha origin/main` (:591), which is exactly the
ancestry approach that false-negatives on a squash merge (task 499ba9c9 owns
fixing THAT endpoint) -- this slice must not inherit that bug and must not
be mistaken for fixing it.

UI (DashboardPage.tsx): a new "Stranded work" card, hydration-guarded like
every other card on this page (task 89e90d1a's pattern), rendering one
`Link` per stranded row to `/tasks/${task_id}?project=...` -- the deep link
the owner explicitly asked for -- reachable the instant a person lands on
Dashboard, no extra click needed. A calm, no-alarm-word empty state when
zero tasks are stranded (owner 2026-07-21: "idle"/"stalled" read as "you
must act"; a genuinely clean board must not sound alarmed).

The NEW symbols this task adds (`_is_shipped_on_main`, `get_stranded_work`)
are imported LAZILY inside each test, mirroring
tests/unit/test_approve_finishes_delivery.py's own convention -- so a
pre-implementation run of this file is a genuine RED (rc==1, real test
FAILUREs) rather than a collection ERROR (rc==2), which is what the red_gate
machine seat requires ("red not demonstrated ... wanted rc==1").

Every git scenario below is built against a DISPOSABLE throwaway repo in
tmp_path -- never against the real E:\\.prism checkout -- so the rule is
proven generically, not merely matching this machine's known stranded rows.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "prism_service" / "web" / "src"
_DASH = _SRC / "pages" / "DashboardPage.tsx"


# ---------------------------------------------------------------------------
# git fixture helpers (mirrors test_approve_finishes_delivery.py's style)
# ---------------------------------------------------------------------------

def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                       text=True, check=True)
    return r.stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _set_origin_main(repo: Path, sha: str) -> None:
    """Point refs/remotes/origin/main at `sha` without standing up a real
    remote -- the scan reads origin/main directly, exactly what `git fetch`
    would leave behind."""
    _git(repo, "update-ref", "refs/remotes/origin/main", sha)


def _commit(repo: Path, path: str, body: str, subject: str) -> str:
    p = repo / path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", subject)
    return _git(repo, "rev-parse", "HEAD")


def _wire(monkeypatch, repo: Path, tasks: list, ws_map: dict | None = None):
    """Patch the collaborators `get_stranded_work` reads: task_workspace for
    branch resolution, claude_transcripts for repo path, get_project for the
    done-task list."""
    ws_map = ws_map or {}
    from prism_service.services import task_workspace
    monkeypatch.setattr(task_workspace, "workspace_for",
                        lambda tid: ws_map.get(tid))
    import prism_service.api.tasks as tasks_mod
    from prism_service.services import claude_transcripts as ct
    monkeypatch.setattr(ct, "_project_source_path", lambda project: str(repo))
    fake_task_svc = SimpleNamespace(
        list=lambda status=None, **kw: [
            t for t in tasks if status is None or t.status == status])
    monkeypatch.setattr(tasks_mod, "get_project",
                        lambda project: SimpleNamespace(task_svc=fake_task_svc))
    return tasks_mod


def _rows(project: str = "prism") -> list[dict]:
    from prism_service.api.tasks import get_stranded_work
    return get_stranded_work(project=project)["rows"]


# ---------------------------------------------------------------------------
# AC-1 -- squash-safe: a done task whose trailer IS on origin/main (even via
# a squash commit that shares no ancestry with the branch) is not stranded
# ---------------------------------------------------------------------------

def test_a_squash_merged_done_task_is_not_reported_stranded(tmp_path, monkeypatch):
    task_id = "sq1234ab"
    repo = _init_repo(tmp_path)
    branch = f"prism/ws/{task_id}"
    _git(repo, "checkout", "-qb", branch)
    tip = _commit(repo, "feature.txt", "work\n", f"add feature [task:{task_id}]")

    # Squash-merge simulation: main gets a DIFFERENT commit (different
    # message -> different sha) carrying the same trailer, sharing only the
    # pre-branch base as parent -- a sibling, not a descendant.
    _git(repo, "checkout", "-q", "main")
    squash_sha = _commit(repo, "feature.txt", "work\n",
                         f"add feature (#1) [task:{task_id[:8]}]")
    assert squash_sha != tip

    # Prove the squash precondition explicitly: the branch tip is NOT an
    # ancestor of what landed on main -- the ancestry check this slice must
    # NOT use would false-negative exactly here.
    anc = subprocess.run(["git", "merge-base", "--is-ancestor", tip, squash_sha],
                         cwd=str(repo))
    assert anc.returncode != 0, (
        "test setup must reproduce a genuine squash: tip must NOT be an "
        "ancestor of the commit that landed on main")

    _set_origin_main(repo, squash_sha)
    task = SimpleNamespace(id=task_id, title="Add feature", status="done")
    _wire(monkeypatch, repo, [task],
         {task_id: {"branch": branch, "path": str(repo), "repo_root": str(repo)}})

    ids = [r["task_id"] for r in _rows()]
    assert task_id not in ids, (
        f"a squash-merged done task must not be reported stranded: {ids}")


# ---------------------------------------------------------------------------
# AC-2 -- a done task whose trailer is ABSENT from origin/main IS stranded,
# with a full row: task_id, title, commits_ahead, branch_exists_on_origin
# ---------------------------------------------------------------------------

def test_a_done_task_with_no_trailer_on_origin_main_is_stranded_with_full_row(
    tmp_path, monkeypatch,
):
    task_id = "st123456"
    repo = _init_repo(tmp_path)
    _set_origin_main(repo, _git(repo, "rev-parse", "HEAD"))
    branch = f"prism/ws/{task_id}"
    _git(repo, "checkout", "-qb", branch)
    _commit(repo, "a.txt", "1\n", f"work one [task:{task_id}]")
    _commit(repo, "b.txt", "2\n", f"work two [task:{task_id}]")
    # This branch WAS pushed (a remote-tracking ref exists for it).
    _git(repo, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")

    task = SimpleNamespace(id=task_id, title="Ship the thing", status="done")
    _wire(monkeypatch, repo, [task],
         {task_id: {"branch": branch, "path": str(repo), "repo_root": str(repo)}})

    rows = _rows()
    row = next((r for r in rows if r["task_id"] == task_id), None)
    assert row is not None, f"expected {task_id} to be reported stranded: {rows}"
    assert row["title"] == "Ship the thing"
    assert row["commits_ahead"] == 2, row
    assert row["branch_exists_on_origin"] is True, row


# ---------------------------------------------------------------------------
# AC-3 -- local-only branch is DIFFERENTIABLE from pushed-but-unmerged
# ---------------------------------------------------------------------------

def test_local_only_branch_is_differentiable_from_pushed_but_unmerged(
    tmp_path, monkeypatch,
):
    repo = _init_repo(tmp_path)
    _set_origin_main(repo, _git(repo, "rev-parse", "HEAD"))

    local_id = "loc12345"
    local_branch = f"prism/ws/{local_id}"
    _git(repo, "checkout", "-qb", local_branch)
    _commit(repo, "a.txt", "x\n", f"local only work [task:{local_id}]")
    _git(repo, "checkout", "-q", "main")

    pushed_id = "psh12345"
    pushed_branch = f"prism/ws/{pushed_id}"
    _git(repo, "checkout", "-qb", pushed_branch)
    _commit(repo, "b.txt", "y\n", f"pushed work [task:{pushed_id}]")
    _git(repo, "update-ref", f"refs/remotes/origin/{pushed_branch}", "HEAD")
    _git(repo, "checkout", "-q", "main")

    tasks = [SimpleNamespace(id=local_id, title="Local only", status="done"),
             SimpleNamespace(id=pushed_id, title="Pushed, open PR", status="done")]
    ws_map = {
        local_id: {"branch": local_branch, "path": str(repo), "repo_root": str(repo)},
        pushed_id: {"branch": pushed_branch, "path": str(repo), "repo_root": str(repo)},
    }
    _wire(monkeypatch, repo, tasks, ws_map)

    rows = {r["task_id"]: r for r in _rows()}
    assert local_id in rows and pushed_id in rows, rows
    assert rows[local_id]["branch_exists_on_origin"] is False, rows[local_id]
    assert rows[pushed_id]["branch_exists_on_origin"] is True, rows[pushed_id]
    assert rows[local_id]["state"] != rows[pushed_id]["state"], (
        "a never-pushed (data-loss-risk) branch must carry a DIFFERENT "
        f"`state` than a pushed-but-unmerged one: {rows}")
    assert rows[local_id]["state"] == "local_only", rows[local_id]
    assert rows[pushed_id]["state"] == "pushed_unmerged", rows[pushed_id]


# ---------------------------------------------------------------------------
# AC-4 -- non-done tasks are never reported stranded
# ---------------------------------------------------------------------------

def test_non_done_tasks_are_never_reported_stranded(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _set_origin_main(repo, _git(repo, "rev-parse", "HEAD"))
    task_id = "prog1234"
    branch = f"prism/ws/{task_id}"
    _git(repo, "checkout", "-qb", branch)
    _commit(repo, "wip.txt", "x\n", f"wip [task:{task_id}]")
    _git(repo, "checkout", "-q", "main")

    task = SimpleNamespace(id=task_id, title="Still working", status="in_progress")
    _wire(monkeypatch, repo, [task],
         {task_id: {"branch": branch, "path": str(repo), "repo_root": str(repo)}})

    assert _rows() == [], (
        "a task that is not status=done must never appear in the stranded scan")


# ---------------------------------------------------------------------------
# AC-5 -- a bare substring occurrence of the id (no bracketed trailer) does
# not count as a shipped match
# ---------------------------------------------------------------------------

def test_bare_substring_occurrence_does_not_count_as_a_trailer_match(
    tmp_path, monkeypatch,
):
    task_id = "ab12cd34"
    repo = _init_repo(tmp_path)
    # message CONTAINS the id substring, but not as the real "[task:xxx]"
    # bracketed trailer -- must not satisfy the shipped check.
    sha = _commit(repo, "x.txt", "1\n",
                  f"unrelated note mentioning task:{task_id}extra in prose")
    _set_origin_main(repo, sha)

    task = SimpleNamespace(id=task_id, title="Not actually shipped", status="done")
    _wire(monkeypatch, repo, [task], {})

    ids = [r["task_id"] for r in _rows()]
    assert task_id in ids, (
        "a bare substring occurrence of the id (no bracketed [task:xxx] "
        f"trailer) must NOT satisfy the shipped check: rows={_rows()}")


# ---------------------------------------------------------------------------
# AC-6 -- shipped-ness resolves via message-grep on origin/main, never
# ancestry, and credits task 499ba9c9 as the ancestry-bug owner
# ---------------------------------------------------------------------------

def test_shipped_check_is_squash_safe_and_credits_the_ancestry_bug_owner():
    import inspect
    from prism_service.api import tasks as tasks_mod
    assert hasattr(tasks_mod, "_is_shipped_on_main"), (
        "expected a dedicated squash-safe shipped-ness helper, e.g. "
        "_is_shipped_on_main(repo, task_id) -> bool")
    src = inspect.getsource(tasks_mod._is_shipped_on_main)
    assert "merge-base" not in src and "is-ancestor" not in src, (
        f"shipped-ness must never use SHA ancestry (that is get_task_delivery's "
        f"squash bug, task 499ba9c9's to fix, not this slice's): {src}")
    assert "--grep" in src, "must resolve shipped-ness via `git log --grep`"
    assert "origin/main" in src
    assert "499ba9c9" in src, (
        "a code comment must name task 499ba9c9 as the owner of the existing "
        "ancestry-based /delivery endpoint's squash bug")


# ---------------------------------------------------------------------------
# AC-7 -- the scan is reachable as a real GET route, not just a python
# function (the affordance a browser/UI fetch actually hits)
# ---------------------------------------------------------------------------

def test_stranded_endpoint_is_registered_on_the_tasks_router():
    from prism_service.api.tasks import router
    hits = [
        (r.path, sorted(r.methods)) for r in router.routes
        if hasattr(r, "methods") and hasattr(r, "path")
        and r.path.rstrip("/").endswith("stranded")
    ]
    assert any("GET" in methods for _p, methods in hits), (
        f"a GET route ending in /stranded must be registered on the tasks "
        f"router (found matching paths: {hits}; all routes: "
        f"{[r.path for r in router.routes if hasattr(r, 'path')]})")


# ---------------------------------------------------------------------------
# UI (source-reading, no JS test runner in this repo -- see
# tests/unit/test_conductor_page_animated_cleanup_ui.py and
# tests/unit/test_dashboard_hydration_skeleton_ui.py for the convention)
# ---------------------------------------------------------------------------

def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _block(src: str, anchor: str, span: int = 1200) -> str:
    i = src.find(anchor)
    assert i != -1, f"anchor {anchor!r} not found in {_DASH}"
    return src[i:i + span]


def test_ac_ui1_dashboard_fetches_stranded_work_with_a_hydration_flag():
    src = _read(_DASH)
    assert "/api/tasks/stranded" in src, (
        "DashboardPage must fetch the new stranded-work endpoint")
    fetch_window = _block(src, "/api/tasks/stranded", 400)
    assert re.search(r"[Ll]oaded|[Hh]ydrated", fetch_window), (
        "the stranded-work fetch must flip a named hydration flag on settle "
        "(the pattern stateLoaded/actLoaded already establish), never leave "
        "the card to infer freshness from data being null")


def test_ac_ui2_stranded_section_renders_a_deep_link_per_row():
    src = _read(_DASH)
    m = re.search(r"stranded[\w.?]*\.map\(", src)
    assert m, (
        "expected a `.map(` over the stranded rows to render one row each "
        f"(no such map found in source)")
    window = src[m.start():m.start() + 900]
    assert re.search(r"<Link\b", window), (
        f"each stranded row must render a <Link> (the rendered tag, not just "
        f"the import) to the task deep link: {window!r}")
    href_re = re.compile(
        r"to=\{`/tasks/\$\{[\w.]*task_id\}\?project=")
    assert href_re.search(window), (
        "the Link's `to` must be the deep link `/tasks/${task_id}?project=...` "
        f"the owner explicitly asked for: {window!r}")


def test_ac_ui3_empty_state_reads_calm_no_alarm_words():
    src = _read(_DASH)
    card = _block(src, "Stranded work", 1800)
    m = re.search(r"<Empty>([^<]+)</Empty>", card)
    assert m, (
        f"expected an <Empty>...</Empty> literal for the zero-stranded state "
        f"inside the Stranded work card: {card!r}")
    empty_copy = m.group(1)
    for word in ("idle", "stalled", "urgent", "danger", "warning", "alert"):
        assert word not in empty_copy.lower(), (
            f"the zero-stranded empty state must read calm (owner 2026-07-21: "
            f"'idle'/'stalled' read as 'you must act') -- found {word!r} in "
            f"{empty_copy!r}")
    assert len(empty_copy.strip()) > 10, (
        f"empty state must say something substantive, not a bare dash: "
        f"{empty_copy!r}")


def test_ac_ui4_stranded_card_is_hydration_guarded():
    src = _read(_DASH)
    card = _block(src, "Stranded work", 1800)
    assert "Skeleton" in card, (
        "the Stranded work card must render a Skeleton pre-hydration, like "
        "every other card on this page")
    assert re.search(r"[Ss]tranded\w*[Ll]oaded|[Hh]ydrated", card), (
        f"the Skeleton must be gated on a NAMED hydration flag, not data "
        f"truthiness: {card!r}")
