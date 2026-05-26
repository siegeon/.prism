"""Per-project git source-pinning service (T5 of v5.1).

INV-2 from story 5.1: PRISM maintains its own clone of each tracked
project at `data/projects/<name>/source/`. Never touches the user's
working tree. Refresh trigger is ref-advance only — a fetch that
advances the pinned remote ref is the single event that invalidates
cached artifacts (the SHA changes, so the content-addressable cache
naturally misses).

All git invocations are explicit subprocess calls with `cwd=` pinned
to the project's source dir, no `shell=True`, no remote configuration
beyond what was passed to `ensure_cloned`. Push operations are never
issued.
"""

from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from prism_service.config import project_data_dir


# Strip `user[:password]@` userinfo from any URL we surface in errors so a
# PAT-in-URL clone failure doesn't leak the token into API responses or logs.
_URL_USERINFO_RE = re.compile(r"(\b[a-z][a-z0-9+.\-]*://)[^/@\s]+@")


def _scrub_credentials(text: str) -> str:
    return _URL_USERINFO_RE.sub(r"\1", text or "")


def _clear_dir(path: Path) -> None:
    """Remove everything inside `path` but leave `path` itself in place.

    Used to wipe a half-initialized clone so a retry with corrected
    credentials starts from a clean slate. Never traverses outside `path`.
    """
    import shutil
    if not path.is_dir():
        return
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


# Per-project locks so concurrent threads can't race on the same clone.
_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _project_lock(project: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lk = _LOCKS.get(project)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[project] = lk
        return lk


@dataclass
class SourceState:
    project: str
    source_dir: Path
    remote_url: str
    tracked_ref: str
    head_sha: str = ""
    advanced: bool = False


class SourceUnavailable(RuntimeError):
    """Raised when source operations are requested but the project has
    no configured remote_url. Callers should fall back to no-source mode
    rather than crashing."""


def _run_git(args: list[str], cwd: Path, timeout: int = 60) -> tuple[int, str, str]:
    """Run git with explicit cwd, no shell. Returns (rc, stdout, stderr).

    Injects the github credential helper from prism_service.services.github_auth
    when a `/data/.git-credentials` file exists, so private-repo clones
    Just Work after the operator pastes a PAT into the Connections
    panel. Imported lazily to avoid a circular import on module load.
    """
    from prism_service.services import github_auth
    pre = github_auth.git_credential_helper_args()
    proc = subprocess.run(
        ["git", *pre, *args],
        cwd=str(cwd),
        capture_output=True, text=True, timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def source_dir_for(project: str) -> Path:
    """Return the directory PRISM should analyze for this project.

    v5.2.0 — projects can be either:
      * Folder-mode (preferred): understand_state.json has a `source_path`
        field set to a directory (typically /code/<repo> bind-mounted from
        the host). We return that path as-is; no clone happens server-side.
      * Clone-mode (legacy): no source_path set. We return the per-project
        data/projects/<name>/source dir which ensure_cloned() fills via
        `git clone`.

    Falls back to the data-dir path if state can't be read so the function
    is safe to call before configuration.
    """
    from prism_service.engines import understand_engine as ue
    try:
        state = ue._read_state(project)
        sp = state.get("source_path") or ""
        if sp:
            p = Path(sp)
            if p.is_dir():
                return p
    except Exception:
        pass
    return project_data_dir(project) / "source"


def is_cloned(project: str) -> bool:
    """Renamed semantically — answers 'does PRISM know where this project's
    source lives, and is it readable?' Returns True for both folder-mode
    (source_path set and points at a real dir) and clone-mode (source/
    has a .git checkout)."""
    from prism_service.engines import understand_engine as ue
    try:
        sp = (ue._read_state(project).get("source_path") or "").strip()
        if sp and Path(sp).is_dir():
            return True
    except Exception:
        pass
    return (project_data_dir(project) / "source" / ".git").exists()


def set_source_path(project: str, source_path: str) -> dict:
    """Configure a project to read its source from a bind-mounted folder
    instead of a git clone. Validates the path is reachable; records it
    in understand_state.json. Idempotent.

    Returns the resolved state for the SPA's response.
    """
    from prism_service.engines import understand_engine as ue
    sp = (source_path or "").strip()
    if not sp:
        raise SourceUnavailable("source_path is required")
    p = Path(sp).resolve()
    if not p.is_dir():
        raise SourceUnavailable(
            f"source_path {sp!r} does not exist or is not a directory. "
            "Pass an absolute path that PRISM can read on this machine "
            "(native install) or inside the container (docker install — "
            "bind-mount the host path first via docker-compose `-v`)."
        )
    state = ue._read_state(project)
    state["source_path"] = str(p)
    state["mode"] = "folder"
    # Clear remote_url so the SPA knows this project isn't tracking a
    # remote anymore. Leaves tracked_ref alone since it might be useful
    # for drift-detection against the local git checkout's branch.
    state.pop("remote_url", None)
    ue._write_state(project, state)
    return {
        "project": project,
        "source_path": str(p),
        "mode": "folder",
        "head_sha": _current_sha_if_git(p),
    }


def _current_sha_if_git(path: Path) -> str:
    """If `path` is a git repo, return its current HEAD SHA. Otherwise ''."""
    if not (path / ".git").exists():
        return ""
    try:
        rc, out, _err = _run_git(["rev-parse", "HEAD"], path, timeout=10)
        return out.strip() if rc == 0 else ""
    except Exception:
        return ""


def ensure_cloned(
    project: str,
    remote_url: str,
    tracked_ref: str = "origin/main",
    *,
    fetch: bool = True,
) -> SourceState:
    """Idempotent: clone if absent, fetch if present, hard-reset to ref.

    The remote_url is recorded as `origin`. Subsequent calls verify
    that the configured origin matches; if not, the existing clone is
    left untouched and `SourceUnavailable` is raised — never silently
    repoint a clone at a different remote.
    """
    if not remote_url:
        raise SourceUnavailable(
            f"project {project!r} has no configured remote_url"
        )
    src = source_dir_for(project)
    state = SourceState(
        project=project, source_dir=src,
        remote_url=remote_url, tracked_ref=tracked_ref,
    )
    with _project_lock(project):
        prior_sha = ""
        if is_cloned(project):
            rc, out, _ = _run_git(["remote", "get-url", "origin"], src)
            if rc != 0 or out.strip() != remote_url:
                raise SourceUnavailable(
                    f"existing clone at {src} points at a different "
                    f"origin; refusing to re-point silently"
                )
            prior_sha = _rev_parse(src, tracked_ref) or ""
            if fetch:
                rc, _, err = _run_git(
                    ["fetch", "--prune", "origin"], src, timeout=180,
                )
                if rc != 0:
                    raise SourceUnavailable(
                        f"git fetch failed for project {project!r}: "
                        f"{_scrub_credentials(err.strip()) or f'exit {rc}'}"
                    )
        else:
            src.mkdir(parents=True, exist_ok=True)
            rc, _, err = _run_git(
                ["clone", "--filter=blob:none", remote_url, "."],
                src, timeout=300,
            )
            if rc != 0:
                # Leave the half-initialized source dir empty so a retry
                # with corrected credentials starts from a clean slate.
                _clear_dir(src)
                raise SourceUnavailable(
                    f"git clone failed for project {project!r}: "
                    f"{_scrub_credentials(err.strip()) or f'exit {rc}'}"
                )
        _run_git(["reset", "--hard", tracked_ref], src)
        state.head_sha = _rev_parse(src, "HEAD") or ""
        state.advanced = bool(prior_sha) and state.head_sha != prior_sha
    return state


def _rev_parse(src_dir: Path, ref: str) -> Optional[str]:
    rc, out, _ = _run_git(["rev-parse", "--verify", ref], src_dir)
    if rc != 0:
        return None
    return out.strip() or None


def current_sha(project: str, ref: str = "HEAD") -> str:
    """Return the SHA at `ref` in the project's source dir.

    Works for both clone-mode (data/projects/<name>/source is a git repo)
    and folder-mode (bind-mounted /code/<repo> that happens to be a git
    repo). If the folder isn't a git repo at all, returns a synthetic
    SHA derived from a directory-content hash so drift detection still
    has a stable identity to compare against.
    """
    src = source_dir_for(project)
    if not is_cloned(project):
        raise SourceUnavailable(f"project {project!r} has no source configured")
    if (src / ".git").exists():
        sha = _rev_parse(src, ref)
        if sha is None:
            raise SourceUnavailable(f"ref {ref!r} not found in {project}")
        return sha
    # Non-git folder: synthesize a stable SHA from a quick directory
    # fingerprint. Enough to detect "changed since last analyzed" for
    # drift purposes; not cryptographically meaningful.
    return _folder_fingerprint(src)


def _folder_fingerprint(path: Path) -> str:
    """Stable-ish hash over file paths + mtimes under `path`. Skipped dirs
    match the same skip set used by ingest_source_to_brain so the
    fingerprint reflects what PRISM actually cares about."""
    import hashlib
    h = hashlib.sha256()
    try:
        for p in sorted(path.rglob("*")):
            if not p.is_file():
                continue
            if any(part in _INGEST_SKIP_DIRS for part in p.parts):
                continue
            try:
                rel = str(p.relative_to(path)).replace("\\", "/")
                h.update(rel.encode("utf-8"))
                h.update(b"\x00")
                h.update(str(int(p.stat().st_mtime)).encode("utf-8"))
                h.update(b"\x00")
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return h.hexdigest()


def has_advanced(project: str, since_sha: str, tracked_ref: str = "origin/main") -> bool:
    """Cheap check: does the tracked ref point at a different SHA?"""
    if not is_cloned(project):
        return False
    new = _rev_parse(source_dir_for(project), tracked_ref)
    return bool(new) and new != since_sha


def diff_files(project: str, from_sha: str, to_sha: str) -> list[str]:
    """Paths changed between two SHAs (added/modified/renamed only).

    Returns posix-style relative paths inside the source dir. Deletions
    are excluded because analyzers can't read missing files.
    """
    src = source_dir_for(project)
    rc, out, _ = _run_git(
        ["diff", "--name-only", "--diff-filter=AMR",
         f"{from_sha}..{to_sha}", "--"],
        src,
    )
    if rc != 0:
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


_CODE_SUFFIXES = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".cs",
    ".go", ".rs", ".java", ".rb", ".php", ".cpp", ".c", ".h", ".hpp",
    ".md",
})

_INGEST_SKIP_DIRS = {
    "node_modules", "__pycache__", ".git", "dist", "build",
    "graphify-out", "graphify-src", "web_dist", ".venv", "venv",
    # v5.3.9 — folder-mode projects often point at a repo that contains
    # `.claude/worktrees/<branch>/` siblings (developer workflow). Each
    # worktree is a full copy of the repo, so without this skip the
    # walker hits 10-50× duplication. `.claude` also excludes
    # user-specific config (settings.local.json, etc) that has no
    # business in the project's knowledge index. `benchmarks` is the
    # PRISM-specific data-set dir that holds reference repos for eval
    # runs — ingesting those tanks signal-to-noise. `.tmp` covers most
    # editor scratch dirs.
    ".claude", "worktrees", "target", ".next", ".nuxt", "out",
    "benchmarks", ".tmp", ".cache", ".idea", ".vscode",
    "coverage", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    # v6.0.19 — `.venv` (singular) was the only venv name on the list,
    # but the prism-dev skill creates `E:\.prism\.venvs\dev\…` (PLURAL,
    # at the repo root). Without `.venvs` here, ingest pulled ~1900
    # site-packages .py files into the prism project's Brain — and the
    # real services/ source code never crossed the noise floor in
    # graphify community detection. `.dev-data` is the matching dev
    # data dir (`E:\.prism\.dev-data\projects\<p>\…`) — same problem,
    # it carries `.venvs` and `projects/<p>/source/` from prior runs.
    ".venvs", ".dev-data",
}


def ingest_source_to_brain(project: str, max_files: int = 2000) -> dict:
    """Walk data/projects/<project>/source/ and push code files into
    Brain + Graph for this project. Reuses existing services — no new
    indexing logic.

    Idempotent: brain_svc.index_doc replaces prior chunks for the same
    source_file. graph_svc.stage_doc + rebuild() refresh entities and
    relationships.

    Returns {"ingested": N, "rebuilt": bool, "skipped": K, "error"?: ...}.
    """
    from prism_service.project_context import get_project
    src = source_dir_for(project)
    if not src.is_dir():
        return {"ingested": 0, "rebuilt": False, "error": "source not cloned"}

    try:
        ctx = get_project(project)
        brain = ctx.brain_svc
        graph = getattr(ctx, "graph_svc", None)
    except Exception as e:
        return {"ingested": 0, "rebuilt": False, "error": str(e)}

    ingested = 0
    skipped = 0
    for path in src.rglob("*"):
        if not path.is_file():
            continue
        if any(part in _INGEST_SKIP_DIRS for part in path.parts):
            skipped += 1
            continue
        if path.suffix.lower() not in _CODE_SUFFIXES:
            skipped += 1
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            skipped += 1
            continue
        rel = str(path.relative_to(src)).replace("\\", "/")
        try:
            brain.index_doc(rel, content)
            if graph is not None:
                graph.stage_doc(rel, content)
            ingested += 1
        except Exception:
            skipped += 1
        if ingested >= max_files:
            break

    rebuilt = False
    rebuild_error = ""
    if graph is not None and ingested > 0:
        try:
            graph.rebuild(getattr(brain, "db_path", None))
            rebuilt = True
        except Exception as e:
            # Log to stderr so background-thread failures don't vanish.
            import sys
            rebuild_error = f"{type(e).__name__}: {e}"
            print(
                f"[ingest_source_to_brain] graph.rebuild failed: "
                f"{rebuild_error}", file=sys.stderr, flush=True,
            )

    return {
        "ingested": ingested, "skipped": skipped,
        "rebuilt": rebuilt, "rebuild_error": rebuild_error,
    }


def bootstrap_after_clone(project: str, max_files: int = 2000) -> dict:
    """Post-clone bootstrap: ingest source into Brain + Graph, then enqueue
    Understand-Anything analyzers. Called in a daemon thread from
    /api/projects (one-shot create-with-source) and /api/understand/configure.

    Returning a dict for tests; production callers fire-and-forget.
    """
    import sys
    from prism_service.engines import understand_engine as ue
    ingest_result = ingest_source_to_brain(project, max_files=max_files)
    refresh_status = "skipped"
    queued: list = []
    try:
        result = ue.UnderstandEngine(project).refresh()
        refresh_status = result.status
        queued = list(result.queued)
    except Exception as e:
        print(
            f"[bootstrap_after_clone] refresh failed for {project!r}: "
            f"{type(e).__name__}: {e}",
            file=sys.stderr, flush=True,
        )
    return {
        "ingest": ingest_result,
        "refresh_status": refresh_status,
        "queued": queued,
    }


def checkout(project: str, sha: str) -> Path:
    """Pin the source tree to `sha` and return its absolute path.

    Uses `git -c advice.detachedHead=false checkout <sha>` so the
    operator-facing log stays clean. The clone is detached at the
    target SHA; subsequent ensure_cloned() calls will reset back to
    the tracked ref.
    """
    src = source_dir_for(project)
    if not is_cloned(project):
        raise SourceUnavailable(f"project {project!r} is not cloned yet")
    with _project_lock(project):
        _run_git(
            ["-c", "advice.detachedHead=false", "checkout", sha],
            src,
        )
    return src
