"""EXIT-half reachability tooth for green_gate (task c944cac2).

Counterpart to the AUTHORING-time check (oracle_authoring._unwired_reason,
task 597839d9): that one refuses at task-creation when allowed_files names
no existing production module -- a MODULE-level, allowed_files-driven check
that measured 0/3 against the real offending tasks (0665c829, d09bee0b,
7362c14e) because allowed_files is empty on 48/48 active tasks, and because
the real defect is SYMBOL-level: work_item_sync.py was imported/constructed
in production the whole time (api/integrations.py:27,235); only
WorkItemSync.register() itself had no production caller.

THIS tooth refuses at green_gate, reading the task's real worktree GIT DIFF
(never allowed_files), and is SYMBOL-level: a new production entry point (a
function/method the diff adds, undecorated, public) needs a non-test
production REFERENCE elsewhere in the tree -- an explicit call, a quoted
dynamic-dispatch-table or SPA-route-path string, or (tolerated outright) a
decorator (FastAPI route registration and similar framework wiring).

Non-policy module (control_plane.POLICY_FILES lists conductor_service.py,
not this file) so the diff into the policy file stays minimal at the call
site: see ConductorService.adjudicate_green_gate.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path


def _is_test_path(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if "tests" in parts:
        return True
    name = parts[-1] if parts else ""
    return (name.startswith("test_") or name.endswith("_test.py")
            or name == "conftest.py")


def _iter_def_sites(source: str):
    """Yield (qualname, name, lineno, decorated) for every function/method
    def in `source`. `lineno` is the `def` line itself (ast, py3.8+), so it
    lines up with a diff's added-line-number set. `decorated` is True for
    ANY decorator (route registration, DI, event hooks, ...) -- decorated
    entry points are framework-called, not explicit-call-site-called, so
    they are tolerated outright by the caller."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    out: list = []
    stack: list = []

    class _V(ast.NodeVisitor):
        def visit_ClassDef(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def _fn(self, node):
            qual = ".".join(stack + [node.name]) if stack else node.name
            out.append((qual, node.name, node.lineno,
                        bool(node.decorator_list)))
            self.generic_visit(node)

        visit_FunctionDef = _fn
        visit_AsyncFunctionDef = _fn

    _V().visit(tree)
    return out


_HUNK_RE = re.compile(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@')


def _added_line_numbers(workspace: Path, baseline: str, rel: str) -> set:
    """New-file line numbers this diff ADDS in `rel` (unified=0 hunks) --
    only these lines' `def`s count as NEW entry points for a file that
    already existed at `baseline`."""
    try:
        out = subprocess.run(
            ["git", "diff", "-U0", baseline, "--", rel],
            cwd=str(workspace), capture_output=True, text=True, timeout=10)
    except Exception:
        return set()
    if out.returncode != 0:
        return set()
    added: set = set()
    counter = None
    for line in out.stdout.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            counter = int(m.group(1))
            continue
        if counter is None or line.startswith(("+++", "---")):
            continue
        if line.startswith("+"):
            added.add(counter)
            counter += 1
    return added


def _candidate_files(repo_root: Path) -> list:
    """Tracked + untracked-not-ignored files, git-SCOPED -- so a gitignored
    data/task_workspaces tree (often thousands of nested worktree
    checkouts) is never walked; git already knows what to skip."""
    files: list = []
    for args in (["ls-files"],
                ["ls-files", "--others", "--exclude-standard"]):
        try:
            out = subprocess.run(["git", *args], cwd=str(repo_root),
                                 capture_output=True, text=True, timeout=15)
        except Exception:
            continue
        if out.returncode == 0:
            files.extend(ln.strip() for ln in out.stdout.splitlines()
                        if ln.strip())
    seen: set = set()
    ordered = []
    for f in files:
        if f not in seen:
            seen.add(f)
            ordered.append(f)
    return ordered


def _has_production_reference(symbol: str, defining_file: str,
                              repo_root, defining_lineno: int = 0) -> bool:
    """Does any file (including `defining_file` itself) reference `symbol`,
    defined at `defining_lineno` in `defining_file`? Three signals, all
    permissive-by-design (narrow the seat, never a blanket refusal):

      (a) an explicit `.symbol(` / `symbol(` call in a file that IMPORTS
          the defining module -- the import gate is what stops a same-named
          stdlib/framework call (atexit.register, in a file that never
          imports the defining module) from false-positiving; grep for
          `.register(` alone finds that atexit call in main.py and nothing
          else, which is the exact miss attempt 1's design would have made.
      (b) `symbol` appearing as an exact dispatch-table KEY ('symbol': ...)
          or as a path SEGMENT of a leading-'/' quoted string (a route
          path) -- dynamic dispatch and SPA fetch calls, tolerated with NO
          import requirement (a .tsx file cannot `import` a .py module).
          Scoped this narrowly (not "any quoted string containing the
          word") because a broad contains-check false-positived on plain
          English prose quoting the word "register" (__version__.py
          changelog text, github_auth.py's user-facing copy) -- measured
          live against this repo before landing this check.
      (c) a call to `symbol(` on any OTHER line of `defining_file` itself
          -- task 3baadd19 AC-5 (`_SpendTracker.charge`) found this
          missing: a new symbol whose only caller is a sibling function in
          the SAME module (the ordinary, correct way to write a private
          helper) was flagged unreachable, because the old code excluded
          the defining file from the search entirely. Only the symbol's
          own `def` line is excluded, so the definition can't trivially
          satisfy its own reference check.
    """
    repo_root = Path(repo_root)
    defining_rel = str(defining_file).replace("\\", "/")
    stem = Path(defining_file).stem
    sym = re.escape(symbol)
    stem_re = re.escape(stem)
    import_re = re.compile(
        r'(?:from\s+[\w.]*\b' + stem_re + r'\b[\w.]*\s+import\b'
        r'|from\s+[\w.]+\s+import\s+[^\n#]*\b' + stem_re + r'\b'
        r'|import\s+[\w.]*\b' + stem_re + r'\b)')
    call_re = re.compile(r'(?<!\w)' + sym + r'\s*\(')
    dispatch_key_re = re.compile(r'''['"]''' + sym + r'''['"]\s*:''')
    route_path_re = re.compile(
        r'''['"]/[^'"\n]*\b''' + sym + r'''\b[^'"\n]*['"]''')
    # A `def symbol(`/`async def symbol(` line matches call_re too (the
    # symbol name followed by `(`) -- excluded so the definition can never
    # trivially satisfy its own same-file reference check. Line-number
    # based exclusion (defining_lineno) is the precise signal when the
    # caller has it; this pattern-based one is the fallback when it does
    # not (defining_lineno defaults to 0, matching no real line).
    def_line_re = re.compile(r'^\s*(?:async\s+)?def\s+' + sym + r'\s*\(')

    try:
        candidates = _candidate_files(repo_root)
    except Exception:
        return True
    if not candidates:
        # Nothing to inspect (git unavailable / empty repo) -- abstain
        # rather than refuse work we cannot see.
        return True

    for rel in candidates:
        if rel.replace("\\", "/") == defining_rel:
            try:
                text = (repo_root / rel).read_text(encoding="utf-8",
                                                   errors="ignore")
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), start=1):
                if i == defining_lineno or def_line_re.match(line):
                    continue
                if call_re.search(line):
                    return True
            continue
        if _is_test_path(rel):
            continue
        try:
            text = (repo_root / rel).read_text(encoding="utf-8",
                                               errors="ignore")
        except OSError:
            continue
        if dispatch_key_re.search(text) or route_path_re.search(text):
            return True
        if rel.endswith(".py") and import_re.search(text) \
                and call_re.search(text):
            return True
    return False


def unreachable_entry_point_reason_for_diff(workspace, baseline: str) -> str:
    """The testable core: git-level, no allowed_files, no task lookup.

    Refuses when the WORKING TREE diff against `baseline` adds a new,
    undecorated, public function/method to a production .py file with no
    non-test production reference anywhere else in the tree. Returns a
    one-line reason naming every offending symbol, or "" when sound (or
    when there is nothing to inspect -- abstain, never refuse blind)."""
    workspace = Path(workspace)
    try:
        from prism_service.services.verifier_service import (
            _git_changed_files)
    except Exception:
        return ""
    try:
        changed = _git_changed_files(workspace, baseline)
    except Exception:
        return ""
    if not changed:
        return ""

    try:
        out = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(workspace), capture_output=True, text=True, timeout=10)
        untracked = {ln.strip() for ln in out.stdout.splitlines()
                    if ln.strip()} if out.returncode == 0 else set()
    except Exception:
        untracked = set()

    violations: list = []
    for rel in changed:
        if not rel.endswith(".py") or _is_test_path(rel):
            continue
        if rel.split("/")[0] == "web":
            continue  # SPA is a reference SOURCE, not scanned for entries
        full = workspace / rel
        if not full.is_file():
            continue
        try:
            source = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        is_new_file = rel in untracked
        added = None if is_new_file else _added_line_numbers(
            workspace, baseline, rel)
        if not is_new_file and not added:
            continue

        for qualname, name, lineno, decorated in _iter_def_sites(source):
            if name.startswith("_") or decorated:
                continue
            if not is_new_file and lineno not in added:
                continue
            if _has_production_reference(name, rel, workspace,
                                         defining_lineno=lineno):
                continue
            violations.append(f"{qualname} in {rel}")

    if not violations:
        return ""
    names = ", ".join(sorted(set(violations)))
    return (
        f"unreachable entry point(s): {names} -- new production symbol(s) "
        "with no non-test production caller anywhere in the tree (no "
        "explicit call site, route decorator, dynamic-dispatch key, or "
        "SPA route reference found); this adds a construction site with "
        "nothing wiring it into the running system")


def _fresh_merge_base(workspace: Path, stale_baseline: str) -> str:
    """Fresh diff base against origin/main's current tip, computed locally
    (no fetch -- whatever origin/main the workspace already has cached from
    its last fetch). A task's stored `baseline` is set once at worktree
    creation (task_workspace.ensure_workspace) and never updated afterward
    by any code path -- so it goes stale the moment the workspace is
    legitimately rebased onto a newer origin/main, and the stale baseline
    then diffs against every OTHER task's landed work too, misattributing
    it to this candidate (live incident: task 8582921d, 2026-08-26, a
    3-day-stale baseline produced a 168-file / +26093-line / 213-commit
    diff for a 4-file change and refused green_gate on symbols the task
    never touched).

    BUT origin/main can also lag BEHIND the stored baseline -- this repo's
    own self-dev carve-out commits straight to dev/main and pushes moments
    later, so a fresh worktree created off local HEAD while a just-made
    commit is still unpushed has a baseline AHEAD of origin/main. Blindly
    preferring the fresh point there would walk the diff base backward past
    real, already-landed, unrelated local commits and blame the candidate
    for them -- the identical-shaped defect found and fixed the same day in
    control_plane.py's `_fresh_diff_base` (candidate_policy_edits). So the
    fresh point is only adopted when the stored baseline is its ancestor
    (or equal) -- i.e. genuinely FORWARD motion, never backward. Returns ""
    on any failure (including "would regress") so the caller falls back to
    the stored baseline unchanged -- never raise, never refuse blind."""
    try:
        out = subprocess.run(
            ["git", "merge-base", "HEAD", "origin/main"],
            cwd=str(workspace), capture_output=True, text=True, timeout=10)
    except Exception:
        return ""
    if out.returncode != 0 or not out.stdout.strip():
        return ""
    fresh = out.stdout.strip().splitlines()[0]
    if not stale_baseline or fresh == stale_baseline:
        return fresh
    try:
        anc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", stale_baseline, fresh],
            cwd=str(workspace), capture_output=True, text=True, timeout=10)
    except Exception:
        return ""
    return fresh if anc.returncode == 0 else ""


def unreachable_entry_point_reason(task) -> str:
    """Task-level entry point for ConductorService.adjudicate_green_gate.

    Resolves the task's REAL conductor worktree and diffs it against its
    baseline; a task with no resolvable workspace (every existing
    test_gate_adjudicator*.py fixture, and every task this seat has never
    actually built a worktree for) ABSTAINS -- "" -- rather than refuse
    work it cannot even inspect. That is deliberate: a validator that
    blocks the unresolvable case gets routed around (oracle_authoring.py's
    own doctrine) and would break every neighbouring gate-adjudicator test
    the same way attempt 1 did."""
    task_id = str(getattr(task, "id", "") or "").strip()
    if not task_id:
        return ""
    try:
        from prism_service.services import task_workspace
        ws = task_workspace.workspace_for(task_id)
    except Exception:
        return ""
    path = (ws or {}).get("path") if ws else None
    baseline = (ws or {}).get("baseline") if ws else None
    if not path or not baseline:
        return ""
    try:
        if not Path(path).is_dir():
            return ""
        baseline = _fresh_merge_base(Path(path), baseline) or baseline
        return unreachable_entry_point_reason_for_diff(Path(path), baseline)
    except Exception:
        return ""
