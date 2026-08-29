"""Deterministic plan_gate teeth -- the defects a human caught by hand.

Task 72ccaf94 needed FIVE rounds at plan_gate. A human adjudicator caught
every defect; the flow caught none. Three of the five recur and are
MECHANICALLY CHECKABLE, so they belong in the flow, not in a reviewer's
eyes:

  1. absent_file_claim -- the plan asserted a test "does not exist" while it
     sat at services/prism-service/tests/unit/test_sqlite_maint.py:34.
  2. stop_if_pinned    -- a test named in task.stop_if was absent from
     task.verify, so the slice's own named risk was not pinned by its gate.
  3. already_green_ac  -- an acceptance criterion that ALREADY PASSES at the
     base commit was offered as the observation for an oracle clause. Three
     rounds running (AC-5, AC-12, AC-13). red_gate demands a failing test
     before implementation; plan_gate demanded nothing of the kind.

THIS MODULE IS NOT GATE POLICY (control_plane.POLICY_FILES). It never
decides a gate: it returns refusal STRINGS. The seats that consult it are
api/conductor_flow.py's entry-time autoclear and services/gate_adjudicator's
re-sweep -- both machine seats. A human's own Approve click goes through
gate_decide directly and is never blocked by anything here.

DEGRADE RULE (owner direction): a checker that cannot measure reports PASS
with a recorded reason. A plan is never refused because the tooling failed.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Optional


CHECKS: tuple[str, ...] = (
    "absent_file_claim", "stop_if_pinned", "already_green_ac")

LABELS: dict[str, str] = {
    "absent_file_claim":
        "No false 'does not exist' claim about a path that resolves",
    "stop_if_pinned":
        "Every test named in stop_if is pinned by verify",
    "already_green_ac":
        "An AC offered as an oracle observation fails at the base commit",
}

_TRUE = {"1", "true", "yes", "on"}


def measurement_enabled() -> bool:
    """Whether check 3 may spend a subprocess running the AC's own target at
    the base commit. ON by default; PRISM_PLAN_GATE_MEASURE_BASE=0 leaves the
    cheap declaration tier only (which still refuses an undeclared AC)."""
    raw = os.environ.get("PRISM_PLAN_GATE_MEASURE_BASE", "1").strip().lower()
    return raw in _TRUE


def _measure_timeout_s() -> float:
    try:
        return max(10.0, float(os.environ.get(
            "PRISM_PLAN_GATE_MEASURE_TIMEOUT_S", "180")))
    except (TypeError, ValueError):
        return 180.0


# ----------------------------------------------------------------------
# 1. absent_file_claim
# ----------------------------------------------------------------------
# Fires ONLY on an explicit does-not-exist assertion about a path that
# RESOLVES in the repo. A plan proposing to CREATE a file is the normal,
# correct shape and must never trip this -- so any line carrying a creation
# intent is skipped whole.
_SRC_EXT = r"(?:py|tsx?|jsx?|json|ya?ml|md|css|html|sql|sh|toml|cs)"
_PATH = rf"[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.{_SRC_EXT}"

_ABSENT_AFTER = re.compile(
    rf"(?P<path>{_PATH})(?::\d+)?\s*(?:\([^)]*\)\s*)?"
    r"(?:currently\s+|still\s+)?"
    r"(?:does\s+not\s+(?:yet\s+)?exist|doesn['’]?t\s+(?:yet\s+)?exist"
    r"|is\s+absent|is\s+missing|is\s+not\s+present)",
    re.IGNORECASE)

_ABSENT_BEFORE = re.compile(
    r"(?:there\s+is\s+no|there\s+are\s+no|no\s+such\s+file"
    r"|there\s+is\s+currently\s+no|we\s+have\s+no|the\s+repo\s+has\s+no"
    r"|nothing\s+at)\b[^.\n]{0,100}?(?P<path>" + _PATH + r")",
    re.IGNORECASE)

# Creation intent on the SAME line -- "test_x.py does not exist, so this
# slice creates it" is an honest plan, not a false absence claim.
_CREATE_HINT = re.compile(
    r"\b(creat\w*|add(?:s|ing|ed)?|new\s+file|introduc\w*|scaffold\w*"
    r"|generat\w*|will\s+write|we\s+write|writes?\s+it|this\s+slice\s+"
    r"(?:adds|writes)|to\s+be\s+written)\b",
    re.IGNORECASE)


def _git_out(root: Path, *args: str, timeout: float = 30.0) -> str:
    try:
        r = subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                           text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _tracked_files(root: Path) -> list[str]:
    """Every path git tracks at ``root`` -- the resolution table for a
    claimed-absent path written repo-relative, service-relative or bare."""
    return [ln.strip() for ln in _git_out(root, "ls-files").splitlines()
            if ln.strip()]


def _resolve(path: str, root: Path, tracked: list[str]) -> str:
    """The tracked path a claimed-absent reference actually names, or "".
    Matches an exact repo-relative hit first, then any tracked file whose
    path ENDS with the claimed one (so 'tests/unit/test_x.py' resolves the
    real 'services/prism-service/tests/unit/test_x.py')."""
    cand = str(path or "").replace("\\", "/").strip().lstrip("./")
    if not cand:
        return ""
    try:
        if (root / cand).is_file():
            return cand
    except OSError:
        pass
    suffix = "/" + cand
    for t in tracked:
        if t == cand or t.endswith(suffix):
            return t
    return ""


def absent_file_claim(plan_doc: str, root: Optional[Path]) -> str:
    """Refusal string when the plan asserts a path does not exist and that
    path resolves. "" when clean, or when there is no repo to resolve
    against (degrade to PASS -- never refuse for want of a measurement)."""
    text = str(plan_doc or "")
    if root is None or not text.strip():
        return ""
    tracked = _tracked_files(root)
    problems: list[str] = []
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _CREATE_HINT.search(line):
            continue
        for rx in (_ABSENT_AFTER, _ABSENT_BEFORE):
            for m in rx.finditer(line):
                claimed = m.group("path")
                resolved = _resolve(claimed, root, tracked)
                if resolved and resolved not in seen:
                    seen.add(resolved)
                    problems.append(f"{claimed} -> {resolved}")
    if not problems:
        return ""
    return ("plan_checks: the plan says a path does not exist and it does: "
            + "; ".join(problems)
            + " -- look before you claim absence, a false absence argues the "
              "reader out of looking")


# ----------------------------------------------------------------------
# 2. stop_if_pinned
# ----------------------------------------------------------------------
_TEST_FILE_RE = re.compile(r"[A-Za-z0-9_./\\-]*test[A-Za-z0-9_./\\-]*\.py",
                           re.IGNORECASE)
_TEST_FUNC_RE = re.compile(r"\btest_[A-Za-z0-9_]+\b")


def _test_tokens(text: str) -> list[str]:
    """Test names a stop_if clause references: file paths normalised to their
    stem (so 'tests/unit/test_x.py' and the workspace-root-relative form
    compare equal) plus bare test_* function names not already inside a
    matched path. A clause naming no test yields [] and is accepted."""
    out: list[str] = []
    spans: list[tuple[int, int]] = []
    for m in _TEST_FILE_RE.finditer(text or ""):
        spans.append(m.span())
        out.append(Path(m.group(0).replace("\\", "/")).name[:-3])
    for m in _TEST_FUNC_RE.finditer(text or ""):
        if any(s <= m.start() < e for s, e in spans):
            continue
        out.append(m.group(0))
    seen: set[str] = set()
    uniq: list[str] = []
    for t in out:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    return uniq


def _pinned_files(verify: Any, root: Optional[Path]) -> list[Path]:
    """The .py files task.verify actually pins, resolved under ``root``."""
    if root is None:
        return []
    out: list[Path] = []
    for v in (verify or []):
        for tok in re.split(r"\s+", str(v)):
            t = tok.strip().replace("\\", "/").lstrip("./")
            if not t.endswith(".py"):
                continue
            try:
                cand = root / t
                if cand.is_file():
                    out.append(cand)
            except OSError:
                continue
    return out


def _func_defined_in(func: str, files: list[Path]) -> bool:
    """Whether ``func`` is DEFINED in any of ``files``."""
    pat = re.compile(r"^\s*(?:async\s+)?def\s+" + re.escape(func) + r"\s*\(",
                     re.MULTILINE)
    for f in files:
        try:
            if pat.search(f.read_text(encoding="utf-8", errors="replace")):
                return True
        except OSError:
            continue
    return False


def stop_if_pinned(stop_if: Any, verify: Any,
                   root: Optional[Path] = None) -> str:
    """Refusal string when task.stop_if names a test that task.verify does
    not pin. Accepts a stop_if clause that names no test."""
    clauses = [str(c) for c in (stop_if or []) if str(c).strip()]
    blob = " ".join(str(v) for v in (verify or [])).lower()
    files = _pinned_files(verify, root)
    missing: list[str] = []
    for clause in clauses:
        for tok in _test_tokens(clause):
            low = tok.lower()
            if low in blob or low in missing:
                continue
            # A stop_if may name a test FUNCTION while verify pins the FILE
            # that DEFINES it. Task 72ccaf94 named
            # test_checkpoint_db_truncates_wal and verify pinned
            # tests/unit/test_sqlite_maint.py, which holds it at line 34 --
            # correctly pinned, and comparing a function name against a
            # file stem refused it anyway. Measured live on 7.13.165.
            # Resolution can only ever ADD a way to pass: with no root, or
            # a verify naming no .py file, `files` is empty and the textual
            # verdict stands unchanged.
            if tok.startswith("test_") and _func_defined_in(tok, files):
                continue
            missing.append(low)
    if not missing:
        return ""
    return ("plan_checks: stop_if names test(s) that task.verify does not "
            "pin: " + ", ".join(sorted(missing))
            + " -- the slice's own named risk must be pinned by its own gate")


# ----------------------------------------------------------------------
# 3. already_green_ac
# ----------------------------------------------------------------------
# TWO TIERS, calibrated against the FOUR real plan revisions of task
# 72ccaf94 (.prism/evidence/72ccaf94-.../plan-r2..r5.md):
#   Tier 1 (pure, plan-level): among the ACs carrying an `oracle:`, at least
#     one must state that it is RED at the base commit. Rounds 2 and 3, both
#     REJECTED, have zero such ACs -- every criterion they offered was a
#     green one. Rounds 4 and 5, the revision the human approved, mark
#     "RED at HEAD" on the criteria that observe the fix. A plan in which
#     nothing goes red before the fix is not observing a fix.
#   Tier 2 (measured, per-AC): an AC that is neither a declared guard nor
#     already excused runs its own pytest target at the base commit and must
#     come back non-zero. rc == 0 is the round-2 AC-5 defect exactly.
#     Anything that cannot be resolved or run degrades to PASS.
# A GUARD is never refused, individually or collectively -- "the existing
# suite stays green", "both verify files run clean", "the build is
# importable" are legitimate ship-hygiene criteria (round 5's AC-8 and AC-9,
# approved). The guard vocabulary is deliberately generous for that reason:
# the plan-level tooth is what stops a plan made ENTIRELY of guards.
_ORACLE_RE = re.compile(r"\boracle\s*[:=]", re.IGNORECASE)
_GUARD_RE = re.compile(
    r"regression\s+guard|stays?\s+green|remains?\s+green|already\s+green"
    r"|green\s+at\s+(?:head|base)|must\s+keep\s+passing|keeps?\s+passing"
    r"|is\s+green|are\s+green|runs?\s+clean|still\s+holds?|still\s+passes"
    r"|unedited|unchanged|is\s+importable|no\s+regression",
    re.IGNORECASE)
_RED_RE = re.compile(
    r"\bred\s+at\s+(?:head|base|[0-9a-f]{7,40})|\bred\s+before\b"
    r"|\bfails?\s+at\s+(?:head|base|the\s+base)"
    r"|\bfails?\s+before\s+the\s+fix|currently\s+fails"
    r"|rc\s*=+\s*1\b|\bred\s+at\s+the\s+base",
    re.IGNORECASE)
_TARGET_RE = re.compile(
    r"[A-Za-z0-9_./\\-]*tests?/[A-Za-z0-9_./\\-]+\.py(?:::[A-Za-z0-9_]+)*")


def _ac_entries(plan_doc: str) -> list[tuple[str, str]]:
    """[(ac_id, folded line)] -- the SAME parser the plan_coverage rubric
    uses, imported rather than reimplemented so the two can never disagree
    about what an AC entry is. arc_governance is gate policy: this module
    reads it, never edits it."""
    try:
        from prism_service.services.arc_governance import _ac_lines
    except Exception:
        return []
    try:
        return list(_ac_lines(str(plan_doc or "")))
    except Exception:
        return []


def _run_at_rev(root: Path, rev: str, targets: list[str],
                timeout_s: float = 180.0) -> Optional[int]:
    """pytest return code for ``targets`` at ``rev``, via a throwaway detached
    worktree (same shape as verifier_service._pytest_run_at_rev). None when
    the measurement could not be taken at all -- the caller degrades to PASS.
    rc 4 (usage) and rc 5 (no tests ran) are also unmeasurable, not green."""
    if not targets or not rev:
        return None
    import sys as _sys
    tmp = tempfile.mkdtemp(prefix="prism-plan-base-")
    wt = str(Path(tmp) / "wt")
    try:
        add = subprocess.run(["git", "worktree", "add", "--detach", wt, rev],
                             cwd=str(root), capture_output=True, text=True,
                             timeout=120)
        if add.returncode != 0:
            return None
        env = dict(os.environ)
        env["PRISM_DATA_DIR"] = str(Path(tmp) / "data")
        r = subprocess.run(
            [_sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             "-o", "faulthandler_timeout=120", *targets],
            cwd=wt, capture_output=True, text=True, env=env, timeout=timeout_s)
        return None if r.returncode in (4, 5) else r.returncode
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    finally:
        try:
            subprocess.run(["git", "worktree", "remove", "--force", wt],
                           cwd=str(root), capture_output=True, text=True,
                           timeout=60)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


def already_green_ac(plan_doc: str, root: Optional[Path], base_ref: str,
                     *, measure: Optional[bool] = None,
                     runner: Optional[Callable[..., Optional[int]]] = None
                     ) -> str:
    """Refusal string when no AC observes the fix -- either because not one
    of them is red at the base commit, or because one that claims to be
    measurably passes there. A declared guard is never refused."""
    entries = _ac_entries(plan_doc)
    if not entries:
        return ""
    do_measure = measurement_enabled() if measure is None else bool(measure)
    run = runner or _run_at_rev
    can_measure = do_measure and root is not None and bool(base_ref)
    # ONE budget for the whole plan, not one per AC. This runs on the
    # adjudicator's sweep thread; an unbounded per-AC timeout would let a
    # 13-AC plan hold that thread for half an hour. Whatever the budget does
    # not reach is simply not measured -- which degrades to PASS, never to a
    # refusal (the tier-1 tooth above still stands on its own).
    deadline = time.monotonic() + _measure_timeout_s()
    with_oracle = 0
    declared_red: list[str] = []
    green: list[str] = []
    for ac_id, line in entries:
        if not _ORACLE_RE.search(line):
            continue
        with_oracle += 1
        if _GUARD_RE.search(line):
            continue
        if _RED_RE.search(line):
            declared_red.append(ac_id)
        if not can_measure:
            continue
        left = deadline - time.monotonic()
        if left < 10.0:
            continue
        targets = _TARGET_RE.findall(line)
        if not targets:
            continue
        if run(root, base_ref, targets, left) == 0:
            green.append(f"{ac_id} ({' '.join(targets)})")
    problems: list[str] = []
    if with_oracle and not declared_red:
        problems.append(
            "no acceptance criterion is shown to FAIL at the base commit -- "
            f"{with_oracle} AC(s) carry an oracle and every one of them is a "
            "guard or states no colour. An AC that is already true observes "
            "nothing; say which criterion is RED at the base commit, and "
            "measure it there")
    if green:
        problems.append(
            "AC(s) that already PASS at the base commit "
            f"{(base_ref or '')[:12]} and so observe nothing: "
            + ", ".join(green))
    return ("plan_checks: " + "; ".join(problems)) if problems else ""


# ----------------------------------------------------------------------
# Task-facing surface
# ----------------------------------------------------------------------
def repo_root_for(task, project: str) -> Optional[Path]:
    """The checkout to resolve paths and revisions against: the task's own
    workspace record first (what the gate verifier reads), then the
    project's configured source path. None when neither exists."""
    tid = str(getattr(task, "id", "") or "")
    if tid:
        try:
            from prism_service.services import task_workspace as tw
            rec = tw.workspace_record(tid) or {}
            root = str(rec.get("repo_root") or "")
            if root and Path(root).exists():
                return Path(root)
        except Exception:
            pass
    try:
        from prism_service.services.claude_transcripts import _project_source_path
        cand = Path(_project_source_path(project))
        if cand.is_absolute() and cand.exists():
            return cand
    except Exception:
        pass
    fallback = Path.home() / "projects" / str(project or "")
    return fallback if fallback.exists() else None


def base_ref_for(task, root: Optional[Path]) -> str:
    """The commit the plan's ACs must be red against: the task workspace's
    recorded baseline, else the checkout's current HEAD. "" degrades the
    measured tier to PASS."""
    tid = str(getattr(task, "id", "") or "")
    if tid:
        try:
            from prism_service.services import task_workspace as tw
            rec = tw.workspace_record(tid) or {}
            base = str(rec.get("baseline") or "").strip()
            if base:
                return base
        except Exception:
            pass
    if root is None:
        return ""
    return _git_out(root, "rev-parse", "HEAD").strip()


# One verdict per (task, plan content, base commit). The adjudicator
# re-sweeps a PENDING plan_gate every interval; without this the measured
# tier would spawn a worktree + pytest run every sweep, forever.
_CACHE: dict[str, tuple[str, list[dict]]] = {}


def _fingerprint(task, base_ref: str) -> str:
    h = hashlib.sha1()
    for part in (str(getattr(task, "plan_doc", "") or ""),
                 repr(list(getattr(task, "stop_if", None) or [])),
                 repr(list(getattr(task, "verify", None) or [])),
                 str(base_ref or "")):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def clear_cache() -> None:
    """Drop every memoised verdict (tests, and any caller that wants a
    forced re-measurement)."""
    _CACHE.clear()


def run_all(task, project: str = "default", *,
            measure: Optional[bool] = None,
            runner: Optional[Callable[..., Optional[int]]] = None,
            use_cache: bool = True) -> list[dict]:
    """[{id, label, ok, reason}] in CHECKS order. Never raises: a tooth that
    cannot be evaluated reports ok=True with an empty reason, matching the
    green-gate registry's own per-tooth convention."""
    if task is None:
        return [{"id": c, "label": LABELS[c], "ok": True, "reason": ""}
                for c in CHECKS]
    root = repo_root_for(task, project)
    base = base_ref_for(task, root)
    tid = str(getattr(task, "id", "") or "")
    fp = _fingerprint(task, base)
    if use_cache and tid:
        hit = _CACHE.get(tid)
        if hit and hit[0] == fp:
            return [dict(r) for r in hit[1]]
    plan = str(getattr(task, "plan_doc", "") or "")
    out: list[dict] = []
    for check_id in CHECKS:
        try:
            if check_id == "absent_file_claim":
                reason = absent_file_claim(plan, root)
            elif check_id == "stop_if_pinned":
                reason = stop_if_pinned(getattr(task, "stop_if", None),
                                        getattr(task, "verify", None), root)
            else:
                reason = already_green_ac(plan, root, base, measure=measure,
                                          runner=runner)
        except Exception:
            reason = ""
        out.append({"id": check_id, "label": LABELS[check_id],
                    "ok": not reason, "reason": reason})
    if use_cache and tid:
        _CACHE[tid] = (fp, [dict(r) for r in out])
    return out


def run_check(check_id: str, task, project: str = "default", **kw) -> dict:
    """One named tooth, same shape as run_all's entries. An unknown id
    reports ok=True (a name this build does not know is not a refusal)."""
    if check_id not in CHECKS:
        return {"id": check_id, "label": f"unknown check {check_id!r}",
                "ok": True, "reason": ""}
    for entry in run_all(task, project, **kw):
        if entry["id"] == check_id:
            return entry
    return {"id": check_id, "label": LABELS.get(check_id, check_id),
            "ok": True, "reason": ""}


def refusal(task, project: str = "default", **kw) -> str:
    """"" when every tooth passes, else the joined refusal a machine seat
    stamps on task.gate_reason instead of approving. A human's own Approve
    click never reaches this -- it goes through gate_decide directly."""
    reasons = [e["reason"] for e in run_all(task, project, **kw)
               if e.get("reason")]
    return " | ".join(reasons)
