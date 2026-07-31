"""Properly-powered A/B harness for PRISM retrieval changes.

WHY THIS EXISTS: the 5-PR graft_parity set is too small to decide anything.
It once showed a candidate DOUBLING r@1; the same candidate measured worse
on every cut-off once there were 115 cases. Five samples cannot tell an
improvement from noise, and a plausible fix to a real bug is still a
regression until something says otherwise.

GROUND TRUTH IS FREE AND OBJECTIVE: every commit in any repo is a
(what-was-wanted, which-files-changed) pair. Use the commit subject as the
query and the files that commit touched as the gold set. Hundreds of cases,
nobody's opinion involved.

Both arms run against the SAME index in ONE process, so the only difference
is the code under test. The candidate arm is supplied as an import path to a
function that replaces Brain._graph_search (or any other hook you name), so
product code is never edited to run an experiment.

Verdict is a paired McNemar exact test on "did any gold file land in the
top-5", plus recall deltas at every cut-off. Both are reported: a delta
without a p-value invites reading noise as a win.

Usage:
    # 1. build cases from a repo's history
    python ab_retrieval.py cases --repo <path> --out cases.json --limit 400

    # 2. baseline only
    python ab_retrieval.py run --repo <path> --cases cases.json

    # 3. A/B a candidate
    python ab_retrieval.py run --repo <path> --cases cases.json \
        --candidate mymodule:my_graph_search
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import subprocess
import sys
import time
from math import comb
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent.parent
SERVICE_ROOT = REPO_ROOT / "services" / "prism-service"
RESULTS_DIR = BENCH_DIR.parent / "results" / "graft_parity"

K_VALUES = (1, 3, 5, 10, 20)
SKIP_PARTS = {".git", "node_modules", "vendor", "testdata", "dist", "build"}
MAX_FILE_BYTES = 300_000

# Commit subjects that describe bookkeeping, not a change to locate.
NOISE_PREFIXES = ("merge", "bump", "release", "updated ui/dist",
                  "update changelog", "v0.", "v1.")


def build_cases(repo: Path, suffix: str, limit: int,
                max_files: int) -> list[dict]:
    """(commit subject -> files that commit changed), filtered to be scoreable."""
    out = subprocess.run(
        ["git", "log", f"--format=%H%x1f%s", "--name-only", "-n", str(limit)],
        cwd=str(repo), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout

    commits, cur = [], None
    for line in out.splitlines():
        if "\x1f" in line:
            if cur:
                commits.append(cur)
            sha, subj = line.split("\x1f", 1)
            cur = {"sha": sha[:10], "query": subj.strip(), "files": []}
        elif line.strip() and cur is not None:
            cur["files"].append(line.strip())
    if cur:
        commits.append(cur)

    cases, seen = [], set()
    for c in commits:
        gold = [
            f for f in c["files"]
            if f.endswith(suffix) and not f.endswith("_test" + suffix)
            and (repo / f).exists()
        ]
        # >max_files means a sweeping refactor: no single query locates it.
        if not gold or len(gold) > max_files:
            continue
        q = c["query"]
        if len(q) < 15 or q.lower().startswith(NOISE_PREFIXES):
            continue
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        cases.append({"sha": c["sha"], "query": q, "gold_files": gold})
    return cases


class Harness:
    def __init__(self, project_id: str, projects_dir: Path) -> None:
        if str(SERVICE_ROOT) not in sys.path:
            sys.path.insert(0, str(SERVICE_ROOT))
        from prism_service import config as cfg
        from prism_service import project_context as pc

        cfg.PROJECTS_DIR = projects_dir
        cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        pc._contexts.clear()
        self.project_id = project_id

    def call(self, tool: str, args: dict | None = None):
        from prism_service.mcp.tools import handle_tool

        r = asyncio.run(handle_tool(tool, args or {},
                                    project_id=self.project_id))
        if not r:
            return None
        try:
            return json.loads(r[0].text)
        except json.JSONDecodeError:
            return r[0].text

    def index(self, repo: Path, suffix: str, domain: str) -> dict:
        self.call("project_create", {"project_id": self.project_id})
        n = 0
        for p in sorted(repo.rglob("*" + suffix)):
            rel = p.relative_to(repo)
            if SKIP_PARTS & set(rel.parts):
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    continue
                content = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            self.call("brain_index_doc", {"path": rel.as_posix(),
                                          "content": content,
                                          "domain": domain})
            n += 1
        graph = self.call("graph_rebuild", {}) or {}
        return {"indexed": n, "graph": graph}

    def arm(self, cases: list[dict], limit: int) -> list[dict]:
        per = []
        for c in cases:
            res = self.call("brain_search",
                            {"query": c["query"], "limit": limit})
            ranked = []
            if isinstance(res, list):
                for r in res:
                    if isinstance(r, dict):
                        sf = (r.get("source_file") or "").replace("\\", "/")
                        if sf and sf not in ranked:
                            ranked.append(sf)
            ranks = [ranked.index(g) + 1
                     for g in c["gold_files"] if g in ranked]
            per.append({
                "query": c["query"],
                "gold": c["gold_files"],
                "first": min(ranks) if ranks else None,
                "recall": {k: sum(1 for r in ranks if r <= k)
                           / len(c["gold_files"]) for k in K_VALUES},
            })
        return per


def summarize(per: list[dict]) -> dict:
    n = len(per) or 1
    firsts = [p["first"] for p in per if p["first"]]
    return {
        "n": len(per),
        "recall": {f"r@{k}": round(sum(p["recall"][k] for p in per) / n, 4)
                   for k in K_VALUES},
        "any_gold_top5": sum(1 for p in per if p["first"] and p["first"] <= 5),
        "any_gold_top20": len(firsts),
        "mean_first_rank": round(sum(firsts) / len(firsts), 2)
        if firsts else None,
    }


def mcnemar(a: list[bool], b: list[bool]) -> dict:
    """Exact paired test on the DISCORDANT cases only.

    Reported alongside the deltas on purpose: with the handful of discordant
    pairs a small case-set produces, no split can reach p<0.05, and a delta
    read without that context is how noise gets shipped as a win.
    """
    a_only = sum(1 for x, y in zip(a, b) if x and not y)
    b_only = sum(1 for x, y in zip(a, b) if not x and y)
    n = a_only + b_only
    if n == 0:
        return {"discordant": 0, "favours_baseline": 0, "favours_candidate": 0,
                "p": 1.0, "significant": False}
    hi = max(a_only, b_only)
    p = min(1.0, 2 * sum(comb(n, k) for k in range(hi, n + 1)) / 2 ** n)
    return {"discordant": n, "favours_baseline": a_only,
            "favours_candidate": b_only, "p": round(p, 4),
            "significant": p < 0.05}


def parse_env(pairs: list[str]) -> list[tuple[str, str]]:
    """['PRISM_RERANK=bge-v2'] -> [('PRISM_RERANK', 'bge-v2')]."""
    out = []
    for raw in pairs:
        name, sep, value = raw.partition("=")
        if not sep or not name.strip():
            raise SystemExit(f"--env must be NAME=VALUE, got {raw!r}")
        out.append((name.strip(), value))
    return out


def load_candidate(spec: str):
    """'package.module:function' -> the function replacing Brain._graph_search."""
    mod_name, _, fn_name = spec.partition(":")
    if not fn_name:
        raise SystemExit(f"--candidate must be 'module:function', got {spec!r}")
    return getattr(importlib.import_module(mod_name), fn_name)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("cases", help="build ground truth from git history")
    c.add_argument("--repo", type=Path, required=True)
    c.add_argument("--out", type=Path, required=True)
    c.add_argument("--limit", type=int, default=400)
    c.add_argument("--suffix", default=".go")
    c.add_argument("--max-files", type=int, default=3)

    r = sub.add_parser("run", help="baseline, or A/B against a candidate")
    r.add_argument("--repo", type=Path, required=True)
    r.add_argument("--cases", type=Path, required=True)
    # ENV-FLIP A/B (task 19e4e7f7). --candidate can only swap a Brain method,
    # which cannot reach a feature selected by an environment variable
    # (PRISM_QUERY_DECOMP, PRISM_RERANK). Both are read PER SEARCH CALL inside
    # Brain.search (brain_engine.py:2841, :2903), so flipping os.environ
    # between the two arms of ONE process is a true paired comparison over the
    # SAME index -- the arms differ only in the flag under test.
    r.add_argument("--env", action="append", default=[], metavar="NAME=VALUE",
                   help="env var applied to the CANDIDATE arm only; repeatable")
    r.add_argument("--candidate", default="",
                   help="module:function replacing Brain._graph_search")
    r.add_argument("--suffix", default=".go")
    r.add_argument("--domain", default="go")
    r.add_argument("--limit", type=int, default=max(K_VALUES))
    r.add_argument("--output", type=Path, default=None)

    args = ap.parse_args()

    if args.cmd == "cases":
        cases = build_cases(args.repo, args.suffix, args.limit,
                            args.max_files)
        args.out.write_text(json.dumps(cases, indent=1), encoding="utf-8")
        print(f"built {len(cases)} cases -> {args.out}")
        if len(cases) < 50:
            print("WARNING: under 50 cases cannot separate a real change "
                  "from noise. Deepen the clone (git fetch --depth N).")
        return 0

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    project = f"bench-ab-{int(time.time())}"
    h = Harness(project, RESULTS_DIR / "_work" / project / "projects")

    t0 = time.perf_counter()
    idx = h.index(args.repo, args.suffix, args.domain)
    print(f"indexed {idx['indexed']} files, "
          f"entities={idx['graph'].get('imported_entities')} "
          f"edges={idx['graph'].get('imported_relationships')} "
          f"({time.perf_counter() - t0:.1f}s); {len(cases)} cases")

    base = summarize(h.arm(cases, args.limit))
    payload = {"benchmark": "graft_parity_ab",
               "schema": "prism.graft_parity_ab.v1",
               "project": project, "repo": str(args.repo),
               "cases": len(cases), "index": idx, "baseline": base}
    print("BASELINE " + json.dumps(base["recall"]) +
          f" top5={base['any_gold_top5']}/{base['n']}")

    env_overrides = dict(parse_env(args.env))
    if args.candidate or env_overrides:
        per_base = h.arm(cases, args.limit)  # re-run under identical state
        original = None
        if args.candidate:
            from prism_service.engines.brain_engine import Brain
            original = Brain._graph_search
            Brain._graph_search = load_candidate(args.candidate)
        prior = {k: os.environ.get(k) for k in env_overrides}
        os.environ.update(env_overrides)
        try:
            per_cand = h.arm(cases, args.limit)
        finally:
            if original is not None:
                from prism_service.engines.brain_engine import Brain
                Brain._graph_search = original
            for k, v in prior.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        if env_overrides:
            payload["candidate_env"] = env_overrides
        cand = summarize(per_cand)
        test = mcnemar(
            [bool(p["first"] and p["first"] <= 5) for p in per_base],
            [bool(p["first"] and p["first"] <= 5) for p in per_cand],
        )
        payload.update({"candidate_spec": args.candidate,
                        "candidate": cand, "mcnemar": test,
                        "deltas": {f"r@{k}": round(
                            cand["recall"][f"r@{k}"]
                            - base["recall"][f"r@{k}"], 4)
                            for k in K_VALUES}})
        print("CANDIDATE " + json.dumps(cand["recall"]) +
              f" top5={cand['any_gold_top5']}/{cand['n']}")
        for k in K_VALUES:
            print(f"  r@{k:<2} delta={payload['deltas'][f'r@{k}']:+.4f}")
        print("MCNEMAR " + json.dumps(test))
        better = (cand["recall"]["r@5"] >= base["recall"]["r@5"]
                  and cand["recall"]["r@10"] >= base["recall"]["r@10"]
                  and cand["any_gold_top5"] >= base["any_gold_top5"])
        payload["candidate_is_better"] = better
        print("VERDICT: candidate is "
              + ("NOT worse on the headline metrics" if better
                 else "WORSE -- do not ship"))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = args.output or (RESULTS_DIR / "ab_latest.json")
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
