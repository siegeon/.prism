"""Replay Graft's OWN published benchmark against PRISM retrieval.

WHY THIS EXISTS: NanoNets/Graft advertises large efficiency wins over a
plain coding agent, but ships NO harness -- the repo contains zero
benchmark/eval/harness files, so none of its figures can be reproduced or
audited. What Graft DOES publish is ground truth: five merged PocketBase
pull requests and, for each, the files the maintainers actually changed.
That is objective and public, so it can score anyone -- including us.

WHAT IS SCORED: file localization only. For each PR we hand PRISM the
PR's one-line intent and ask whether its retrieval surfaces the files the
maintainers really touched. Recall@k against the gold set, plus the rank
the first gold file appears at.

WHAT IS DELIBERATELY NOT SCORED: Graft's 10 orientation questions. Those
were graded by a human rubric Graft never published. Inventing our own
rubric and then declaring PRISM the winner against it is precisely the
candidate-controls-judge failure PRISM's own gate policy exists to stop.
They are carried in tasks.json for reference and left unscored.

HONEST LIMITATION, stated up front: this indexes the repo at ONE commit
(whatever is checked out), not at each PR's base commit. A gold file that
was renamed after its PR merged would be unfairly counted as a miss. All
six gold files were verified present at clone time; if you re-run much
later, check that first. This measures retrieval quality, NOT the
end-to-end agent win Graft's headline numbers claim -- no agent loop runs
here, so nothing in this file supports a cost or latency comparison.

Usage:
    python benchmarks/graft_parity/run.py --repo <path-to-pocketbase>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent.parent
SERVICE_ROOT = REPO_ROOT / "services" / "prism-service"
RESULTS_DIR = BENCH_DIR.parent / "results" / "graft_parity"

# Recall is reported at each of these cut-offs.
K_VALUES = (1, 3, 5, 10, 20)

# Only index source we can actually reason about, and skip vendored trees.
SOURCE_SUFFIXES = {".go"}
SKIP_PARTS = {".git", "node_modules", "vendor", "testdata", "dist", "build"}
MAX_FILE_BYTES = 300_000


class InProcessClient:
    """Drive the real MCP tool layer in-process against an isolated project."""

    def __init__(self, project_id: str, projects_dir: Path) -> None:
        if str(SERVICE_ROOT) not in sys.path:
            sys.path.insert(0, str(SERVICE_ROOT))
        from prism_service import config as cfg
        from prism_service import project_context as pc

        cfg.PROJECTS_DIR = projects_dir
        cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        pc._contexts.clear()
        self.project_id = project_id

    def call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        from prism_service.mcp.tools import handle_tool

        result = asyncio.run(
            handle_tool(tool, arguments or {}, project_id=self.project_id)
        )
        if not result:
            return None
        try:
            return json.loads(result[0].text)
        except json.JSONDecodeError:
            return result[0].text


def iter_source_files(repo: Path) -> list[Path]:
    out = []
    for p in repo.rglob("*"):
        if not p.is_file() or p.suffix not in SOURCE_SUFFIXES:
            continue
        if SKIP_PARTS & set(p.relative_to(repo).parts):
            continue
        try:
            if p.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        out.append(p)
    return sorted(out)


def index_repo(client: InProcessClient, repo: Path) -> dict[str, Any]:
    """Index every eligible source file into the isolated bench project."""
    client.call("project_create", {"project_id": client.project_id})
    files = iter_source_files(repo)
    indexed = 0
    for p in files:
        rel = p.relative_to(repo).as_posix()
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        client.call("brain_index_doc", {
            "path": rel, "content": content, "domain": "go",
        })
        indexed += 1

    # brain_search is hybrid BM25 + vector + GRAPH. Indexing docs alone
    # leaves entities/relationships at zero, so the graph leg contributes
    # nothing and the score understates PRISM. Build the code graph over
    # the files we just staged, exactly as a real project would.
    graph = client.call("graph_rebuild", {}) or {}
    return {
        "eligible": len(files),
        "indexed": indexed,
        "graph": graph if isinstance(graph, dict) else {"raw": str(graph)},
    }


def _hit_paths(results: Any) -> list[str]:
    """Ranked, de-duplicated source_file list from a brain_search payload."""
    if not isinstance(results, list):
        return []
    seen: list[str] = []
    for r in results:
        if not isinstance(r, dict):
            continue
        path = (r.get("source_file") or "").replace("\\", "/")
        if path and path not in seen:
            seen.append(path)
    return seen


def score_pr(client: InProcessClient, task: dict[str, Any],
             limit: int) -> dict[str, Any]:
    gold = [g.replace("\\", "/") for g in task["gold_files"]]
    query = task["what"]
    results = client.call("brain_search", {"query": query, "limit": limit})
    ranked = _hit_paths(results)

    found_at = {}
    for g in gold:
        found_at[g] = ranked.index(g) + 1 if g in ranked else None
    hit_ranks = [r for r in found_at.values() if r is not None]

    return {
        "pr": task["pr"],
        "type": task["type"],
        "what": query,
        "gold_files": gold,
        "gold_found_at_rank": found_at,
        "recall_at": {
            f"r@{k}": round(
                sum(1 for r in hit_ranks if r <= k) / len(gold), 3
            ) for k in K_VALUES
        },
        "first_gold_rank": min(hit_ranks) if hit_ranks else None,
        "any_gold_found": bool(hit_ranks),
        "results_returned": len(ranked),
    }


def summarize(per_pr: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(per_pr) or 1
    return {
        "pr_tasks": len(per_pr),
        "mean_recall_at": {
            f"r@{k}": round(
                sum(p["recall_at"][f"r@{k}"] for p in per_pr) / n, 3
            ) for k in K_VALUES
        },
        "prs_with_any_gold_file_found": sum(
            1 for p in per_pr if p["any_gold_found"]
        ),
        "median_first_gold_rank": _median(
            [p["first_gold_rank"] for p in per_pr
             if p["first_gold_rank"] is not None]
        ),
    }


def _median(xs: list[int]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    mid = len(s) // 2
    return float(s[mid]) if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True,
                    help="Path to a pocketbase checkout.")
    ap.add_argument("--target", default="pocketbase")
    ap.add_argument("--limit", type=int, default=max(K_VALUES),
                    help="brain_search result cap (also the largest k).")
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--no-fail", action="store_true")
    args = ap.parse_args()

    if not args.repo.exists():
        print(f"repo not found: {args.repo}", file=sys.stderr)
        return 2

    spec = json.loads((BENCH_DIR / "tasks.json").read_text(encoding="utf-8"))
    target = next(t for t in spec["targets"] if t["id"] == args.target)

    missing = [g for t in target["pr_tasks"] for g in t["gold_files"]
               if not (args.repo / g).exists()]

    project_id = f"bench-graftparity-{int(time.time())}"
    work = RESULTS_DIR / "_work" / project_id
    client = InProcessClient(project_id, work / "projects")

    t0 = time.perf_counter()
    index_stats = index_repo(client, args.repo)
    indexed_sec = round(time.perf_counter() - t0, 3)

    per_pr = [score_pr(client, t, args.limit) for t in target["pr_tasks"]]
    summary = summarize(per_pr)
    elapsed = round(time.perf_counter() - t0, 3)
    return _emit(args, spec, target, index_stats, indexed_sec, per_pr,
                 summary, elapsed, missing, project_id)


def _emit(args, spec, target, index_stats, indexed_sec, per_pr, summary,
          elapsed, missing, project_id) -> int:
    result = {
        "benchmark": "graft_parity",
        "schema": "prism.graft_parity.benchmark.v1",
        "target": target["id"],
        "project": project_id,
        "repo": str(args.repo),
        "elapsed_sec": elapsed,
        "index": {**index_stats, "elapsed_sec": indexed_sec},
        "search_limit": args.limit,
        "summary": summary,
        "per_pr": per_pr,
        "graft_reported": target.get("graft_reported"),
        "questions_scored": False,
        "caveats": [
            "File localization ONLY. No agent loop runs here, so nothing in "
            "this result supports a cost, latency or tool-call comparison "
            "against Graft's headline numbers.",
            "The repo is indexed at ONE checked-out commit, not at each PR's "
            "base commit; a gold file renamed after its PR merged would be "
            "counted as a miss.",
            "Graft's 10 orientation questions are NOT scored: they were "
            "graded by a rubric Graft never published, and authoring our own "
            "rubric to grade ourselves would be self-judging.",
            "Graft's own published figures remain unverified -- its repo "
            "ships no benchmark harness at all, so they cannot be reproduced "
            "here or anywhere.",
        ],
    }
    if missing:
        result["caveats"].insert(0, (
            "GOLD FILES MISSING FROM THIS CHECKOUT (scored as misses, which "
            "understates PRISM): " + ", ".join(sorted(set(missing)))
        ))
    result["gold_files_missing"] = sorted(set(missing))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = args.output or (RESULTS_DIR / "latest.json")
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    s = summary
    print(
        "RESULT graft_parity "
        f"prs={s['pr_tasks']} "
        f"r@1={s['mean_recall_at']['r@1']} "
        f"r@5={s['mean_recall_at']['r@5']} "
        f"r@10={s['mean_recall_at']['r@10']} "
        f"r@20={s['mean_recall_at']['r@20']} "
        f"any_gold={s['prs_with_any_gold_file_found']}/{s['pr_tasks']} "
        f"median_first_rank={s['median_first_gold_rank']} "
        f"indexed={index_stats['indexed']} elapsed={elapsed}s"
    )
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
