"""Measure the ship-disabled retrieval flags on real corpora (task 19e4e7f7).

Owner rule: a capability that ships disabled by default is not shipped. Every
flag either gets measured and flipped on, or its code path gets deleted.
Staying off unmeasured is not an outcome. PRISM_QUERY_DECOMP and PRISM_RERANK
are the only two violations in prism_service.

WHY NOT ab_retrieval.py's --candidate: that hook can only swap a Brain method,
and neither flag is a method -- both are read PER SEARCH CALL inside
Brain.search (brain_engine.py:2841 decomp, :2903 rerank). So this indexes a
corpus ONCE and runs every arm against that SAME index in the SAME process,
flipping only os.environ between arms. Nothing else can differ, which is what
makes the paired McNemar test legitimate.

Method is non-negotiable per the task: never a single corpus, and always the
p-value beside the delta. An early 5-case run once reported a candidate that
DOUBLED r@1; at 739 cases it lost on every metric.

Usage:
    python flag_ab.py --repo <path> --cases <cases.json> --suffix .go \
        --domain go --label pocketbase --out results.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ab_retrieval import Harness, K_VALUES, RESULTS_DIR, mcnemar, summarize  # noqa: E402


# Each arm is (name, {env overrides}). The baseline arm is the SHIPPED
# default -- literally no override -- so "did this beat the baseline" means
# "did it beat what a person who installs PRISM actually gets".
ARMS: list[tuple[str, dict]] = [
    ("decomp", {"PRISM_QUERY_DECOMP": "on"}),
    ("rerank_minilm", {"PRISM_RERANK": "ms-marco-minilm"}),
    ("rerank_bge", {"PRISM_RERANK": "bge-v2"}),
    ("both", {"PRISM_QUERY_DECOMP": "on", "PRISM_RERANK": "ms-marco-minilm"}),
    # Pool sweep (task 19e4e7f7). Reranking is the single most expensive step
    # in search, and its cost is linear in the pool. "Turn it on by default"
    # is only an honest proposal if there is a pool size that keeps most of
    # the recall win at a latency a person will accept, so measure the curve
    # instead of picking 50 because it was already the written default.
    ("rerank_top12", {"PRISM_RERANK": "ms-marco-minilm", "PRISM_RERANK_TOPN": "12"}),
    ("rerank_top25", {"PRISM_RERANK": "ms-marco-minilm", "PRISM_RERANK_TOPN": "25"}),
    ("rerank_top50", {"PRISM_RERANK": "ms-marco-minilm", "PRISM_RERANK_TOPN": "50"}),
]


def run_arm(harness, cases, limit, overrides: dict):
    """One measured pass with ``overrides`` applied, then restored exactly."""
    prior = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    t0 = time.perf_counter()
    try:
        per = harness.arm(cases, limit)
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return per, round(time.perf_counter() - t0, 1)


def top5(per: list[dict]) -> list[bool]:
    return [bool(p["first"] and p["first"] <= 5) for p in per]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", type=Path, required=True)
    ap.add_argument("--cases", type=Path, required=True)
    ap.add_argument("--suffix", default=".go")
    ap.add_argument("--domain", default="go")
    ap.add_argument("--label", required=True)
    ap.add_argument("--limit", type=int, default=max(K_VALUES))
    ap.add_argument("--arms", default="",
                    help="comma-separated arm names; default all")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    wanted = {a.strip() for a in args.arms.split(",") if a.strip()}
    arms = [a for a in ARMS if not wanted or a[0] in wanted]

    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    project = f"flagab-{args.label}-{int(time.time())}"
    harness = Harness(project, RESULTS_DIR / "_work" / project / "projects")

    t0 = time.perf_counter()
    idx = harness.index(args.repo, args.suffix, args.domain)
    print(f"[{args.label}] indexed {idx['indexed']} files "
          f"({time.perf_counter() - t0:.0f}s); {len(cases)} cases", flush=True)

    per_base, base_sec = run_arm(harness, cases, args.limit, {})
    base = summarize(per_base)
    print(f"[{args.label}] BASELINE {json.dumps(base['recall'])} "
          f"top5={base['any_gold_top5']}/{base['n']} ({base_sec}s)", flush=True)

    payload = {
        "schema": "prism.flag_ab.v1",
        "label": args.label,
        "repo": str(args.repo),
        "project": project,
        "cases": len(cases),
        "index": idx,
        "baseline": base,
        "baseline_sec": base_sec,
        "arms": {},
    }

    for name, overrides in arms:
        try:
            per, sec = run_arm(harness, cases, args.limit, overrides)
        except Exception as exc:  # a missing model must not lose the other arms
            payload["arms"][name] = {"env": overrides,
                                     "error": f"{type(exc).__name__}: {exc}"}
            print(f"[{args.label}] {name} FAILED: {exc}", flush=True)
            continue
        summary = summarize(per)
        test = mcnemar(top5(per_base), top5(per))
        deltas = {f"r@{k}": round(summary["recall"][f"r@{k}"]
                                  - base["recall"][f"r@{k}"], 4)
                  for k in K_VALUES}
        payload["arms"][name] = {
            "env": overrides, "summary": summary, "deltas": deltas,
            "mcnemar": test, "sec": sec,
            "sec_per_query": round(sec / max(1, len(cases)), 3),
        }
        print(f"[{args.label}] {name} {json.dumps(summary['recall'])} "
              f"top5={summary['any_gold_top5']}/{summary['n']} "
              f"d(r@5)={deltas['r@5']:+.4f} d(r@10)={deltas['r@10']:+.4f} "
              f"p={test['p']} ({sec}s, {payload['arms'][name]['sec_per_query']}s/q)",
              flush=True)

    out = args.out or (RESULTS_DIR / f"flag_ab_{args.label}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"[{args.label}] wrote {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
