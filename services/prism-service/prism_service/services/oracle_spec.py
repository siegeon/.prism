"""Proof-carrying green-gate oracle (inverted-flow soundness #2).

The old green_gate oracle tooth (arc_governance.score_green_outcome) validated
PROSE SHAPE, not the customer observable: it token-matched the stored
completion_proof against words scraped from task.oracle. That is gameable
(paste a URL, never run it), polarity-blind ("crashed at :8888" satisfied the
URL citation), and failed OPEN when the oracle carried no URL/port/image/
snake_case token. This module replaces prose-shape scoring with a REAL run.

Three pieces (per the Codex punch-list):

  * ``OracleSpec`` — a frozen, versioned, content-hashed spec. It names an
    ADAPTER (http_probe | pytest_ids | browser), a TARGET, POSITIVE assertions
    (must hold) and NEGATIVE assertions (must NOT hold — the polarity fix:
    "status < 400", "body has no error token"), thresholds + a timeout.
    ``spec_hash()`` is a stable content hash. ``from_task`` best-effort derives
    a spec from a task.oracle string (marked ``derived=True`` — a weaker,
    inferred spec).
  * ``EvidenceReceipt`` — an APPEND-ONLY record of one real run: the spec_hash,
    the ``tree_sha`` the run happened at, runner/policy versions, an env
    fingerprint, per-assertion observations, artifact content-hashes, and an
    overall ``passed`` bool. Persisted under
    ``<data>/projects/<project>/receipts/<task_id>.jsonl`` — never mutated.
  * ``run_oracle(spec, task, ctx)`` — the trusted runner. Executes the spec's
    adapter FOR REAL (http_probe actually GETs the surface; pytest_ids actually
    runs the named tests) and records what it observed. The ``browser`` adapter
    is an honest boundary: with no Playwright/agent-browser runner wired it
    records ``manual_evidence_required`` (passed=False) — NOT a silent pass.

The green_gate is claimable ONLY when a FRESH passing receipt exists — fresh =
its ``tree_sha`` matches the task's current workspace commit AND its
``spec_hash`` matches the task's current OracleSpec. A new commit or an oracle
edit invalidates prior receipts. See ``fresh_passing_receipt``.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

RUNNER_VERSION = "oracle-runner/1"
POLICY_VERSION = "green-gate-receipt/1"
SPEC_VERSION = 1

# Adapters
ADAPTER_HTTP = "http_probe"
ADAPTER_PYTEST = "pytest_ids"
ADAPTER_BROWSER = "browser"

# Statuses
ST_PASSED = "passed"
ST_FAILED = "failed"
ST_MANUAL = "manual_evidence_required"
ST_ERROR = "error"
# RED demonstration (task a5e8d877): the trusted runner observed the task's
# pinned tests FAILING at the red-step commit. Receipts with this status
# always carry passed=False, so no red receipt can ever satisfy
# fresh_passing_receipt / green_gate.
ST_RED = "red_demonstrated"

# Body tokens that, if present in an HTTP response, evidence a broken surface
# (the polarity fix: a page that "crashed" no longer satisfies a URL citation).
_ERROR_BODY_TOKENS = (
    "traceback (most recent call last)", "internal server error",
    "uncaught exception", "referenceerror", "typeerror:", "is not defined",
)

_URL_RE = re.compile(r"https?://[^\s)\"'<>]+")

# Vocabulary naming a VISUAL/RENDERED observable (task ee5b226d): a human (or
# a real browser render) has to LOOK at the screen to check these — an
# http_probe's assertions (status<400, non-empty body, no error token,
# bounded latency) cannot observe a skeleton, a tint, a colour, an absent
# row, or a screenshot. Presence of ANY of these keys the oracle to the
# browser adapter EVEN WHEN a URL is also present (the bug this module
# fixes: the URL branch used to win unconditionally).
_VISUAL_CUE_WORDS = (
    "screenshot", "skeleton", "placeholder", "tint", "colour", "color",
    "layout", "pixel", "looks like", "look like", "kpi tile", "0-valued",
    "0 valued", "renders", "rendered", "visible", "visually",
    "no row", "no entry", "no entries", "loading tint",
)
# "no PR title appears as a task", "the row does not appear", "must not
# appear" — an assertion that some UI element's PRESENCE/ABSENCE is what
# proves the oracle. A bare http_probe (status/body/latency) cannot observe
# whether a specific row/entry appears or not.
_NEGATED_APPEAR_RE = re.compile(
    r"\b(no|not|never|n't|without|nor)\b[^.;]{0,60}\bappear", re.IGNORECASE)
_APPEAR_AS_RE = re.compile(r"\bappears?\b[^.;]{0,20}\bas a\b", re.IGNORECASE)


def _names_visual_observable(oracle: str) -> bool:
    """True iff ``oracle`` demands a VISUAL/rendered check — the customer
    observable is something a machine http_probe cannot see (a skeleton, a
    tint, an absent row) even when the oracle also cites a URL. Keys on the
    OBSERVABLE named, not on whether a URL is present (the over-correction
    this ticket's likely_misfire warns against: a genuine reachability
    oracle — 'returns 200', 'the health endpoint is reachable' — names none
    of these and must still derive http_probe)."""
    low = oracle.lower()
    if any(cue in low for cue in _VISUAL_CUE_WORDS):
        return True
    if _NEGATED_APPEAR_RE.search(low) or _APPEAR_AS_RE.search(low):
        return True
    return False


def is_human_judgment(spec) -> bool:
    """Owner rule (2026-07-18, task eaafdf75): a MACHINE seat may sign off a
    gate only when its oracle signal is OBJECTIVE-OBSERVABLE — a test suite
    passes (``pytest_ids``) or an http probe returns ok (``http_probe``). A
    SUBJECTIVE HUMAN-JUDGMENT oracle stays pending for the human: a
    browser/render check (a render receipt proves the pixels exist, NOT that
    the owner approves the direction) or any positive assertion of kind
    ``manual``. Returns True iff the oracle is subjective (human-only)."""
    # A spec with NO target (the task declared no oracle) is NOT human-judgment:
    # a bare task defaults to a browser/manual shape but has nothing for a
    # person to judge, so it takes the normal (verifier) path, not the sign-off.
    if not str(getattr(spec, "target", "") or "").strip():
        return False
    if getattr(spec, "adapter", "") == ADAPTER_BROWSER:
        return True
    for a in getattr(spec, "positive", ()) or ():
        if str(getattr(a, "kind", "") or "").lower() == "manual":
            return True
    return False


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# OracleSpec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Assertion:
    """One checkable claim. ``polarity`` is 'positive' (must hold) or
    'negative' (must NOT hold). ``kind`` names the check the adapter runs;
    ``value`` is its parameter (a status bound, a token, a threshold)."""
    name: str
    polarity: str
    kind: str
    value: Any = None

    def as_dict(self) -> dict:
        return {"name": self.name, "polarity": self.polarity,
                "kind": self.kind, "value": self.value}


@dataclass(frozen=True)
class OracleSpec:
    """A frozen, versioned, content-hashed proof spec."""
    adapter: str
    target: str
    positive: tuple[Assertion, ...] = ()
    negative: tuple[Assertion, ...] = ()
    thresholds: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 10.0
    risk_class: str = "standard"
    spec_version: int = SPEC_VERSION
    derived: bool = False

    def _canonical(self) -> dict:
        """The content used for the hash. ``derived`` (provenance) is
        DELIBERATELY excluded so re-deriving the same oracle string yields a
        stable hash."""
        return {
            "spec_version": self.spec_version,
            "adapter": self.adapter,
            "target": self.target,
            "positive": [a.as_dict() for a in self.positive],
            "negative": [a.as_dict() for a in self.negative],
            "thresholds": {k: self.thresholds[k]
                           for k in sorted(self.thresholds)},
            "timeout_s": self.timeout_s,
            "risk_class": self.risk_class,
        }

    def spec_hash(self) -> str:
        blob = json.dumps(self._canonical(), sort_keys=True,
                          separators=(",", ":"))
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict:
        out = self._canonical()
        out["derived"] = self.derived
        out["spec_hash"] = self.spec_hash()
        return out

    @classmethod
    def from_task(cls, task: Any) -> "OracleSpec":
        """Best-effort DERIVED spec from a task's oracle string.

        A URL in the oracle -> an http_probe (GET the surface; require
        status<400, a non-empty body, no error token, bounded latency) —
        UNLESS the oracle also names a visual observable (a screenshot, a
        skeleton, a tint, an absent row: see ``_names_visual_observable``),
        in which case it derives a ``browser`` spec even with the URL
        present (task ee5b226d: an http_probe cannot see any of those). No
        URL (or a visual observable) -> a ``browser`` spec, which the
        runner reports as ``manual_evidence_required`` — the honest
        boundary for an oracle we cannot auto-run (this REPLACES the old
        fail-OPEN behavior). Always ``derived=True`` so a caller knows it
        was inferred, not authored."""
        oracle = str(getattr(task, "oracle", "") or "")
        misfire = str(getattr(task, "likely_misfire", "") or "")
        # PYTEST RUNG (task 68e5c699): a test-proofed task naming pytest
        # material in verify[] derives a REAL pytest_ids spec — the adapter
        # existed but nothing ever derived it, so test-proofed tasks could
        # never be machine-evidenced and their green_gate was override-only.
        proof_type = str(getattr(task, "proof_type", "") or "").strip().lower()
        pytest_ids = _pytest_ids_from_task(task)
        if pytest_ids and proof_type == "test":
            return cls._pytest_spec(pytest_ids)
        m = _URL_RE.search(oracle)
        # VISUAL-OBSERVABLE FLOOR (task ee5b226d): a URL match used to win
        # unconditionally here, so an oracle naming a screenshot/skeleton/
        # tint/absent-row degraded to a shallow http_probe that cannot see
        # any of it. When the oracle text demands a visual check, route to
        # the browser adapter (manual_evidence_required) EVEN THOUGH a URL
        # is present — the browser runner still finds that URL itself
        # (target carries the full oracle text, see _extract_url).
        if m and not _names_visual_observable(oracle):
            url = m.group(0).rstrip(".,;)!?\"'")
            negative = [
                Assertion("status_under_400", "negative", "status_ge", 400),
                Assertion("no_error_token", "negative", "body_contains_any",
                          list(_ERROR_BODY_TOKENS)),
            ]
            # Fold misfire tokens into negative body assertions (polarity fix:
            # the pre-declared pass-but-wrong signal must NOT appear).
            for tok in _misfire_body_tokens(misfire):
                negative.append(Assertion(
                    f"no_misfire_{tok}", "negative", "body_contains", tok))
            positive = [Assertion("reachable", "positive", "reachable", True),
                        Assertion("nonempty_body", "positive",
                                  "body_nonempty", True)]
            return cls(adapter=ADAPTER_HTTP, target=url,
                       positive=tuple(positive), negative=tuple(negative),
                       thresholds={"max_ms": 10000, "max_payload_bytes":
                                   8_000_000}, timeout_s=10.0, derived=True)
        if pytest_ids and proof_type != "demo":
            # No URL to probe but real pytest material on file: machine
            # evidence beats the manual floor (demo stays demo — it wants a
            # UI artifact, not a test run).
            return cls._pytest_spec(pytest_ids)
        return cls(adapter=ADAPTER_BROWSER, target=oracle.strip(),
                   positive=(Assertion("customer_observable_holds", "positive",
                                       "manual", True),),
                   derived=True)

    @classmethod
    def _pytest_spec(cls, ids: list) -> "OracleSpec":
        return cls(adapter=ADAPTER_PYTEST, target=" ".join(ids),
                   positive=(Assertion("pinned_tests_pass", "positive",
                                       "pytest_pass", True),),
                   thresholds={}, timeout_s=600.0, derived=True)


def _pytest_ids_from_task(task: Any) -> list:
    """Pytest node ids / test paths named by task.verify[] entries. An entry
    contributes when it invokes pytest (a token is/ends with 'pytest') or IS a
    bare test path / node id. Flags and interpreters are dropped; order kept,
    deduped — the derivation must be deterministic (stable spec_hash)."""
    out: list = []
    seen: set = set()
    for raw in (getattr(task, "verify", None) or []):
        s = str(raw or "").strip()
        if not s:
            continue
        toks = s.split()
        invoked = any(t == "pytest" or t.endswith("/pytest")
                      or t.endswith("pytest.exe") for t in toks)
        cands = toks if invoked else (
            [s] if (".py" in s or "::" in s) and " " not in s else [])
        for t in cands:
            if (not t or t.startswith("-") or "=" in t.split("::", 1)[0]
                    or t in ("pytest", "python", "python3", "-m")
                    or t.endswith("python.exe") or t.endswith("pytest.exe")
                    or t.endswith("/pytest")):
                continue
            if (".py" in t or "::" in t) and t not in seen:
                seen.add(t)
                out.append(t)
    return out


_MISFIRE_TOKENS = ("blank", "white", "crash", "error", "500", "undefined")


def _misfire_body_tokens(misfire: str) -> list[str]:
    low = (misfire or "").lower()
    return [t for t in _MISFIRE_TOKENS if t in low]


# ---------------------------------------------------------------------------
# EvidenceReceipt
# ---------------------------------------------------------------------------


@dataclass
class EvidenceReceipt:
    """Append-only record of one trusted oracle run."""
    task_id: str
    job_id: str
    spec_hash: str
    tree_sha: str
    adapter: str
    passed: bool
    status: str
    runner_version: str = RUNNER_VERSION
    policy_version: str = POLICY_VERSION
    # Pinned control-plane provenance (inverted-flow #3): the ref the gate
    # policy was pinned to for this run + a content hash over that policy set,
    # so every pass is attributable to a specific, un-candidate-editable policy.
    control_ref: str = ""
    policy_hash: str = ""
    env: dict = field(default_factory=dict)
    started_at: str = ""
    ended_at: str = ""
    observations: list = field(default_factory=list)
    artifacts: list = field(default_factory=list)
    reason: str = ""
    derived_spec: bool = False

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id, "job_id": self.job_id,
            "spec_hash": self.spec_hash, "tree_sha": self.tree_sha,
            "adapter": self.adapter, "passed": self.passed,
            "status": self.status, "runner_version": self.runner_version,
            "policy_version": self.policy_version,
            "control_ref": self.control_ref, "policy_hash": self.policy_hash,
            "env": self.env,
            "started_at": self.started_at, "ended_at": self.ended_at,
            "observations": self.observations, "artifacts": self.artifacts,
            "reason": self.reason, "derived_spec": self.derived_spec,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EvidenceReceipt":
        return cls(
            task_id=d.get("task_id", ""), job_id=d.get("job_id", ""),
            spec_hash=d.get("spec_hash", ""), tree_sha=d.get("tree_sha", ""),
            adapter=d.get("adapter", ""), passed=bool(d.get("passed")),
            status=d.get("status", ""),
            runner_version=d.get("runner_version", ""),
            policy_version=d.get("policy_version", ""),
            control_ref=d.get("control_ref", "") or "",
            policy_hash=d.get("policy_hash", "") or "",
            env=d.get("env", {}) or {}, started_at=d.get("started_at", ""),
            ended_at=d.get("ended_at", ""),
            observations=d.get("observations", []) or [],
            artifacts=d.get("artifacts", []) or [],
            reason=d.get("reason", ""),
            derived_spec=bool(d.get("derived_spec")))


def _env_fingerprint() -> dict:
    return {"python": sys.version.split()[0], "platform": sys.platform,
            "runner": RUNNER_VERSION}


# ---------------------------------------------------------------------------
# Append-only persistence
# ---------------------------------------------------------------------------


def _receipts_path(project: str, task_id: str) -> Path:
    from prism_service.config import project_data_dir
    d = project_data_dir(project or "default") / "receipts"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{task_id}.jsonl"


def append_receipt(project: str, receipt: EvidenceReceipt) -> Path:
    """APPEND a receipt as one JSONL line. Never mutates prior lines — each
    run adds a row so the full run history for a task survives."""
    path = _receipts_path(project, receipt.task_id)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(receipt.as_dict(), separators=(",", ":")) + "\n")
    return path


def read_receipts(project: str, task_id: str) -> list[EvidenceReceipt]:
    path = _receipts_path(project, task_id)
    if not path.exists():
        return []
    out: list[EvidenceReceipt] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(EvidenceReceipt.from_dict(json.loads(line)))
        except (json.JSONDecodeError, TypeError):
            continue
    return out


def latest_receipt(project: str, task_id: str) -> Optional[EvidenceReceipt]:
    receipts = read_receipts(project, task_id)
    return receipts[-1] if receipts else None


def fresh_passing_receipt(project: str, task_id: str, tree_sha: str,
                          spec_hash: str,
                          policy_hash: Optional[str] = None
                          ) -> Optional[EvidenceReceipt]:
    """The most-recent receipt that PASSED at THIS ``tree_sha`` AND THIS
    ``spec_hash``. A new commit (different tree_sha) or an oracle edit
    (different spec_hash) makes every prior receipt STALE — so this returns
    None and the gate refuses until a fresh run lands.

    POLICY DRIFT (inverted-flow #3): when a current pinned ``policy_hash`` is
    supplied AND the receipt carries one, they must match — a receipt minted
    under a different pinned policy is stale, exactly as a spec edit is stale.
    Enforced only when BOTH are present so an unpinnable environment (both "")
    keeps the prior tree+spec behavior."""
    for r in reversed(read_receipts(project, task_id)):
        if not (r.passed and r.tree_sha == tree_sha and r.spec_hash == spec_hash):
            continue
        if policy_hash and r.policy_hash and r.policy_hash != policy_hash:
            continue
        return r
    return None


def fresh_red_receipt(project: str, task_id: str, red_sha: str,
                      spec_hash: str) -> Optional[EvidenceReceipt]:
    """The most-recent receipt DEMONSTRATING RED for THIS ``red_sha`` AND
    THIS ``spec_hash`` (task a5e8d877): the trusted runner executed the
    spec's pytest ids in a disposable worktree at the red-step commit and
    observed them FAILING. Red receipts always carry ``passed=False`` —
    status ST_RED is the demonstration — so they are structurally invisible
    to ``fresh_passing_receipt`` and can never soften green_gate."""
    if not (red_sha and spec_hash):
        return None
    for r in reversed(read_receipts(project, task_id)):
        if (r.status == ST_RED and r.tree_sha == red_sha
                and r.spec_hash == spec_hash):
            return r
    return None


# ---------------------------------------------------------------------------
# tree_sha resolution
# ---------------------------------------------------------------------------


def current_tree_sha(workspace: Optional[str]) -> str:
    """HEAD commit of the task's workspace checkout, or '' when there is no
    workspace / git is unavailable. This is the candidate commit a receipt is
    bound to — the freshness key."""
    if not workspace:
        return ""
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(workspace),
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return ""
    return ""


# ---------------------------------------------------------------------------
# Adapters
# ---------------------------------------------------------------------------


def _sha256_file(path: str) -> str:
    try:
        return "sha256:" + hashlib.sha256(
            Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _obs(name: str, polarity: str, expected: Any, observed: Any,
         passed: bool) -> dict:
    return {"name": name, "polarity": polarity, "expected": expected,
            "observed": observed, "passed": bool(passed)}


def _run_http_probe(spec: OracleSpec, ctx: dict) -> tuple[list, list, bool, str, str]:
    """Actually GET the target. Evaluates the spec's positive + negative
    assertions against the real (status, latency, body)."""
    import urllib.error
    import urllib.request

    observations: list = []
    status: Optional[int] = None
    body = ""
    reachable = False
    started = time.time()
    try:
        req = urllib.request.Request(spec.target, method="GET")
        with urllib.request.urlopen(req, timeout=spec.timeout_s) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            body = resp.read(spec.thresholds.get(
                "max_payload_bytes", 8_000_000) + 1).decode(
                "utf-8", errors="replace")
            reachable = True
    except urllib.error.HTTPError as exc:
        status = exc.code
        reachable = True
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
    except (urllib.error.URLError, OSError, ValueError) as exc:
        observations.append(_obs("reachable", "positive", True,
                                 f"unreachable: {exc}", False))
        elapsed_ms = (time.time() - started) * 1000
        return observations, [], False, ST_FAILED, (
            f"http_probe: {spec.target} unreachable ({exc})")
    elapsed_ms = (time.time() - started) * 1000
    low_body = body.lower()

    observations.append(_obs("reachable", "positive", True, reachable, reachable))
    observations.append(_obs("nonempty_body", "positive", True,
                             len(body) > 0, len(body) > 0))
    # Negative assertions
    for a in spec.negative:
        if a.kind == "status_ge":
            bad = status is not None and status >= int(a.value)
            observations.append(_obs(a.name, "negative",
                                     f"status<{a.value}", status, not bad))
        elif a.kind == "body_contains":
            bad = str(a.value).lower() in low_body
            observations.append(_obs(a.name, "negative",
                                     f"body has no '{a.value}'",
                                     bad, not bad))
        elif a.kind == "body_contains_any":
            hit = next((t for t in (a.value or []) if str(t).lower() in low_body),
                       None)
            observations.append(_obs(a.name, "negative",
                                     "no error token", hit, hit is None))
    # Thresholds
    max_ms = spec.thresholds.get("max_ms")
    if max_ms is not None:
        observations.append(_obs("latency", "positive", f"<{max_ms}ms",
                                 round(elapsed_ms, 1), elapsed_ms <= max_ms))
    max_bytes = spec.thresholds.get("max_payload_bytes")
    if max_bytes is not None:
        observations.append(_obs("payload_bytes", "positive",
                                 f"<={max_bytes}", len(body.encode("utf-8")),
                                 len(body.encode("utf-8")) <= max_bytes))
    passed = all(o["passed"] for o in observations)
    reason = ("http_probe: all assertions held" if passed else
              "http_probe: " + "; ".join(
                  f"{o['name']} FAILED (observed={o['observed']})"
                  for o in observations if not o["passed"]))
    return observations, [], passed, (ST_PASSED if passed else ST_FAILED), reason


def _run_pytest_ids(spec: OracleSpec, ctx: dict) -> tuple[list, list, bool, str, str]:
    """Run the named pytest node ids and require them to PASS. Reuses the
    project's own runner via the (dev) interpreter — the same subprocess shape
    VerifierService uses for the full-suite gate."""
    ids = [i for i in re.split(r"[\s,]+", spec.target.strip()) if i]
    if not ids:
        return ([], [], False, ST_ERROR,
                "pytest_ids: no test node ids in target")
    python = ctx.get("pytest_python") or sys.executable
    cwd = ctx.get("workspace") or "."
    cmd = [python, "-m", "pytest", *ids, "-q", "-p", "no:cacheprovider"]
    try:
        r = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True,
                           timeout=float(ctx.get("pytest_timeout_s", 600)))
        rc, out, err = r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return ([_obs("pytest_pass", "positive", "rc==0", "timeout", False)],
                [], False, ST_FAILED, "pytest_ids: timed out")
    except (FileNotFoundError, OSError) as exc:
        return ([], [], False, ST_ERROR, f"pytest_ids: could not run ({exc})")
    passed = rc == 0
    obs = [_obs("pytest_pass", "positive", "rc==0", rc, passed)]
    tail = (out or err or "").strip().splitlines()[-1:] or [""]
    reason = f"pytest_ids: {' '.join(ids)} -> rc={rc} ({tail[0][:160]})"
    return obs, [], passed, (ST_PASSED if passed else ST_FAILED), reason


def _run_browser(spec: OracleSpec, ctx: dict) -> tuple[list, list, bool, str, str]:
    """Browser adapter INTERFACE. When a real runner is wired via
    ``ctx['browser_runner']`` (a callable spec,ctx -> dict) it is used;
    otherwise the run is INCONCLUSIVE and recorded as
    ``manual_evidence_required`` (passed=False) — an honest boundary, NOT a
    silent pass."""
    runner = ctx.get("browser_runner")
    if not callable(runner):
        # WIRED BY DEFAULT (task d30c9a75): a browser oracle now EARNS its
        # receipt via a real headless Playwright run instead of falling back
        # to manual_evidence_required (which forced a human override). An
        # explicit ctx['browser_runner'] still wins (tests inject a fake).
        try:
            from prism_service.services import browser_oracle_runner as _bor
            runner = _bor.run
        except Exception:
            runner = None
    if callable(runner):
        res = runner(spec, ctx) or {}
        obs = res.get("observations", [])
        artifacts = []
        for p in res.get("artifacts", []) or []:
            artifacts.append({"path": p, "sha256": _sha256_file(p)})
        # INCONCLUSIVE (runner could not capture: no URL, no browser engine,
        # subprocess/verdict failure, surface unreachable) is NOT a failure —
        # it is the honest manual-evidence boundary (a human attaches the
        # screenshot), never a silent pass and never a hard fail.
        if res.get("inconclusive"):
            return (obs or [_obs("customer_observable_holds", "positive",
                                 "human/agent-browser evidence",
                                 "runner inconclusive", False)],
                    artifacts, False, ST_MANUAL,
                    res.get("reason", "browser: inconclusive — manual "
                            "evidence required"))
        passed = bool(res.get("passed"))
        return (obs, artifacts, passed,
                (ST_PASSED if passed else ST_FAILED),
                res.get("reason", "browser: ran via wired runner"))
    return ([_obs("customer_observable_holds", "positive",
                  "human/agent-browser evidence", "no runner wired", False)],
            [], False, ST_MANUAL,
            "browser: no Playwright/agent-browser runner wired — manual "
            "evidence required (NOT auto-passed)")


_ADAPTERS = {
    ADAPTER_HTTP: _run_http_probe,
    ADAPTER_PYTEST: _run_pytest_ids,
    ADAPTER_BROWSER: _run_browser,
}


# ---------------------------------------------------------------------------
# Trusted runner
# ---------------------------------------------------------------------------


def run_oracle(spec: OracleSpec, task: Any, ctx: Optional[dict] = None,
               persist: bool = True) -> EvidenceReceipt:
    """Execute ``spec``'s adapter FOR REAL and return an EvidenceReceipt.

    ctx keys (all optional): ``project`` (for persistence), ``tree_sha`` (else
    resolved from ``workspace``), ``workspace`` (checkout path), ``pytest_python``
    / ``pytest_timeout_s`` (pytest_ids), ``browser_runner`` (browser). The
    receipt is APPENDED to the task's receipts.jsonl unless ``persist=False``."""
    ctx = dict(ctx or {})
    task_id = getattr(task, "id", "") or ctx.get("task_id", "")
    ctx["task_id"] = task_id  # available to adapters (browser runner -> evidence path)
    project = ctx.get("project") or ""
    tree_sha = ctx.get("tree_sha")
    if tree_sha is None:
        tree_sha = current_tree_sha(ctx.get("workspace"))
    started = _now_iso()
    runner = _ADAPTERS.get(spec.adapter)
    if runner is None:
        observations, artifacts, passed, status, reason = (
            [], [], False, ST_ERROR, f"unknown adapter {spec.adapter!r}")
    else:
        observations, artifacts, passed, status, reason = runner(spec, ctx)
    # EVIDENCE CAPTURE for ui/demo tasks (task 9afd1b72): layer a richer
    # walkthrough (screenshot + video) plus verbatim per-pytest-id assertion
    # source onto the receipt's artifacts[], ON TOP of whatever the adapter
    # itself already produced. ALL best-effort: evidence_capture never raises
    # on its own, and this whole block is additionally guarded so a capture
    # failure (no Playwright, unreachable app, unresolvable node id) yields
    # an artifact-less receipt instead of failing the mint.
    try:
        _proof_type = str(getattr(task, "proof_type", "") or "").strip().lower()
        _tag_set = {str(t).strip().lower()
                   for t in (getattr(task, "tags", None) or [])}
        if task_id and (_proof_type == "demo" or "ui" in _tag_set):
            from prism_service.services import evidence_capture as _ec
            from prism_service.services import browser_oracle_runner as _bor
            from prism_service.data_dir import evidence_dir as _evidence_dir
            _url = _bor._extract_url(spec)
            if _url:
                _cap = _ec.capture_walkthrough(_url, str(_evidence_dir(task_id)))
                if not _cap.get("error"):
                    _prov = _ec.provenance(RUNNER_VERSION, tree_sha or "")
                    if _cap.get("screenshot"):
                        artifacts.append({"kind": "screenshot",
                                          "path": _cap["screenshot"],
                                          "provenance": _prov})
                    if _cap.get("video"):
                        artifacts.append({"kind": "video",
                                          "path": _cap["video"],
                                          "provenance": _prov})
            for _node_id in _pytest_ids_from_task(task):
                _src = _ec.assertion_source_for(_node_id,
                                                ctx.get("workspace") or "")
                if _src:
                    artifacts.append({"kind": "assertion_source",
                                      "path": _node_id,
                                      "provenance": {"source": _src}})
    except Exception:
        pass
    # PINNED CONTROL-PLANE PROVENANCE (inverted-flow #3): stamp the ref the
    # gate policy is pinned to + a hash over that policy set. Best-effort —
    # an unpinnable environment stamps "" (the gate then skips the policy tooth
    # rather than false-refusing), never raises.
    control_ref = str(ctx.get("control_ref") or "")
    pol_hash = str(ctx.get("policy_hash") or "")
    if not control_ref or not pol_hash:
        try:
            from prism_service.services import control_plane as _cp
            # ONE resolution path (task 68e5c699): the stamp falls back to
            # the same workspace-anchored task_pin the gate check resolves,
            # so mint-then-check agree by construction. An explicit
            # ctx['control_ref']/['policy_hash'] from a runner still wins
            # (handled above); only the FALLBACK is unified.
            pin = _cp.task_pin(task_id)
            control_ref = control_ref or pin.get("control_ref", "")
            pol_hash = pol_hash or pin.get("policy_hash", "")
        except Exception:
            pass
    receipt = EvidenceReceipt(
        task_id=task_id, job_id=str(uuid.uuid4()), spec_hash=spec.spec_hash(),
        tree_sha=tree_sha or "", adapter=spec.adapter, passed=bool(passed),
        status=status, control_ref=control_ref, policy_hash=pol_hash,
        env=_env_fingerprint(), started_at=started,
        ended_at=_now_iso(), observations=observations, artifacts=artifacts,
        reason=reason, derived_spec=spec.derived)
    if persist:
        append_receipt(project, receipt)
    return receipt


def _red_worktree_run(spec: OracleSpec, workspace: str, red_sha: str,
                      ctx: dict) -> tuple[list, str, str]:
    """Check out ``red_sha`` in a throwaway worktree and run the spec's
    pytest adapter there. Red is demonstrated iff the run yields pytest
    rc==1 (tests ran and failed) — a pass, a timeout, a collection error or
    an unrunnable environment is honestly NOT a demonstration."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="prism-red-")
    wt = str(Path(tmp) / "wt")
    try:
        add = subprocess.run(
            ["git", "worktree", "add", "--detach", wt, red_sha],
            cwd=workspace, capture_output=True, text=True, timeout=120)
        if add.returncode != 0:
            return [], ST_ERROR, (
                f"red oracle: could not check out red-step commit "
                f"{red_sha[:12]} ({(add.stderr or '').strip()[:160]})")
        runner = _ADAPTERS.get(ADAPTER_PYTEST)
        obs, _arts, passed, _st, run_reason = runner(
            spec, dict(ctx, workspace=wt))
        rc = next((o.get("observed") for o in obs
                   if o.get("name") == "pytest_pass"), None)
        if rc == 1:
            return obs, ST_RED, (
                f"red demonstrated at {red_sha[:12]}: {run_reason}")
        if passed:
            return obs, ST_FAILED, (
                f"NOT red: the spec's tests PASS at the red-step commit "
                f"{red_sha[:12]} ({run_reason})")
        return obs, ST_FAILED, (
            f"red not demonstrated at {red_sha[:12]} (rc={rc!r}, wanted "
            f"rc==1 test failures): {run_reason}")
    except subprocess.TimeoutExpired:
        return [], ST_ERROR, "red oracle: git worktree add timed out"
    except (OSError, FileNotFoundError) as exc:
        return [], ST_ERROR, f"red oracle: could not run ({exc})"
    finally:
        try:
            subprocess.run(["git", "worktree", "remove", "--force", wt],
                           cwd=workspace, capture_output=True, text=True,
                           timeout=60)
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


def run_red_oracle(spec: OracleSpec, task: Any, red_sha: str,
                   ctx: Optional[dict] = None,
                   persist: bool = True) -> EvidenceReceipt:
    """Demonstrate RED for real (task a5e8d877): check out ``red_sha`` — the
    red-step commit — in a DISPOSABLE git worktree and run the spec's pytest
    ids there, expecting failure (pytest rc==1: tests ran and FAILED).

    Anchored to the red-step commit because once the implementation lands in
    the workspace a fresh-HEAD run passes and can no longer demonstrate red.
    pytest_ids only — every other adapter refuses (a red demonstration is a
    real failing run, never prose). The receipt carries tree_sha=red_sha and
    status=ST_RED on a genuine demonstration; ``passed`` is ALWAYS False so
    green_gate's fresh_passing_receipt can never match a red receipt."""
    ctx = dict(ctx or {})
    task_id = getattr(task, "id", "") or ctx.get("task_id", "")
    project = ctx.get("project") or ""
    workspace = ctx.get("workspace") or ""
    started = _now_iso()
    observations: list = []
    status, reason = ST_ERROR, ""
    if spec.adapter != ADAPTER_PYTEST:
        reason = (f"red oracle needs a pytest_ids spec, got {spec.adapter!r}"
                  " — a red demonstration is a real failing run, not prose")
    elif not (workspace and red_sha):
        reason = "red oracle needs a workspace and a red-step commit sha"
    else:
        observations, status, reason = _red_worktree_run(
            spec, str(workspace), red_sha, ctx)
    control_ref = str(ctx.get("control_ref") or "")
    pol_hash = str(ctx.get("policy_hash") or "")
    if not control_ref or not pol_hash:
        try:
            from prism_service.services import control_plane as _cp
            pin = _cp.task_pin(task_id)
            control_ref = control_ref or pin.get("control_ref", "")
            pol_hash = pol_hash or pin.get("policy_hash", "")
        except Exception:
            pass
    receipt = EvidenceReceipt(
        task_id=task_id, job_id=str(uuid.uuid4()), spec_hash=spec.spec_hash(),
        tree_sha=red_sha or "", adapter=spec.adapter, passed=False,
        status=status, control_ref=control_ref, policy_hash=pol_hash,
        env=_env_fingerprint(), started_at=started,
        ended_at=_now_iso(), observations=observations, artifacts=[],
        reason=reason, derived_spec=spec.derived)
    if persist:
        append_receipt(project, receipt)
    return receipt
