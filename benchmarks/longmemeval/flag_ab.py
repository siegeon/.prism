"""LongMemEval, paired, for the ship-disabled retrieval flags (task 19e4e7f7).

WHY THIS AND NOT run.py. run.py drives an isolated HTTP bench service, so an
A/B over an environment flag would mean booting that service twice and
comparing two different indexes built in two different processes. Neither flag
is worth that much doubt. Both are read PER SEARCH CALL inside Brain.search
(brain_engine.py:2841 decomp, :2903 rerank), so this ingests each question's
haystack ONCE, in-process, and runs every arm against that SAME index --
flipping only os.environ between arms. Same index, same process, same order:
the arms differ in exactly one thing.

WHY LONGMEMEVAL DECIDES QUERY DECOMPOSITION. It already lost on code search
(r@5 -0.0014, McNemar p=1.0, memory mx-ff1f5b) -- but only 24 of those 115
queries decomposed at all, and every one was a terse commit subject. Query
decomposition is built for compound NATURAL-LANGUAGE questions, which is what
this dataset is made of, so this is the test that can fairly convict or acquit
it. The task says so explicitly, and the dataset costs no API budget.

Metric matches run.py: hit@5 on the gold session, plus gold_in_pool@50 so a
recall lift can be attributed to candidate generation rather than to rerank
reordering.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent.parent
SERVICE_ROOT = REPO_ROOT / "services" / "prism-service"
sys.path.insert(0, str(BENCH_DIR.parent / "graft_parity"))

from ab_retrieval import Harness, mcnemar  # noqa: E402

DATASET = REPO_ROOT / "benchmarks" / "data" / "longmemeval_s_cleaned.json"

ARMS: list[tuple[str, dict]] = [
    ("decomp", {"PRISM_QUERY_DECOMP": "on"}),
    ("rerank_minilm", {"PRISM_RERANK": "ms-marco-minilm"}),
    ("both", {"PRISM_QUERY_DECOMP": "on", "PRISM_RERANK": "ms-marco-minilm"}),
]


def stratified(data: list[dict], n: int, seed: int = 42) -> list[tuple[int, dict]]:
    """Balanced across question_type — the same sampling run.py uses, so a
    number here is comparable to a number there."""
    rng = random.Random(seed)
    by_type: dict[str, list[int]] = {}
    for i, entry in enumerate(data):
        by_type.setdefault(entry["question_type"], []).append(i)
    types = sorted(by_type)
    base, rem = divmod(n, len(types))
    picked: list[int] = []
    for i, t in enumerate(types):
        take = min(base + (1 if i < rem else 0), len(by_type[t]))
        picked.extend(rng.sample(by_type[t], take))
    picked.sort()
    return [(i, data[i]) for i in picked]


def session_text(turns: list[dict]) -> str:
    return "\n\n".join(f"{t.get('role', 'user')}: {t.get('content', '')}"
                       for t in turns)


def ingest(harness, sample: list[tuple[int, dict]]) -> float:
    """One document per haystack session, one domain per question — so a
    question can only ever retrieve from its own haystack."""
    t0 = time.perf_counter()
    for q_idx, entry in sample:
        domain = f"lme_q{q_idx:03d}"
        for sid, turns in zip(entry["haystack_session_ids"],
                              entry["haystack_sessions"]):
            harness.call("brain_index_doc", {
                "path": f"lme/q{q_idx:03d}/{sid}",
                "content": session_text(turns),
                "domain": domain})
    return round(time.perf_counter() - t0, 1)


def arm(harness, sample: list[tuple[int, dict]], pool_k: int = 50) -> list[dict]:
    per = []
    for q_idx, entry in sample:
        res = harness.call("brain_search", {
            "query": entry["question"], "domain": f"lme_q{q_idx:03d}",
            "limit": pool_k})
        rows = res if isinstance(res, list) else []
        ordered, pool = [], []
        for row in rows:
            if not isinstance(row, dict):
                continue
            did = str(row.get("doc_id") or "")
            if "::" in did:
                did = did.split("::", 1)[0]
            if not did:
                continue
            sid = did.rsplit("/", 1)[-1]
            pool.append(sid)
            if sid not in ordered:
                ordered.append(sid)
        gold = set(entry["answer_session_ids"])
        per.append({
            "q_idx": q_idx,
            "type": entry["question_type"],
            "hit@5": any(s in gold for s in ordered[:5]),
            "gold_in_pool@50": any(s in gold for s in pool),
        })
    return per


def summarize(per: list[dict]) -> dict:
    n = len(per) or 1
    by_type: dict[str, list[bool]] = {}
    for p in per:
        by_type.setdefault(p["type"], []).append(p["hit@5"])
    return {
        "n": len(per),
        "recall@5": round(sum(1 for p in per if p["hit@5"]) / n, 4),
        "pool_recall@50": round(
            sum(1 for p in per if p["gold_in_pool@50"]) / n, 4),
        "hits@5": sum(1 for p in per if p["hit@5"]),
        "by_type": {t: round(sum(v) / len(v), 3) for t, v in sorted(by_type.items())},
    }


def run_arm(harness, sample, overrides: dict):
    prior = {k: os.environ.get(k) for k in overrides}
    os.environ.update(overrides)
    t0 = time.perf_counter()
    try:
        per = arm(harness, sample)
    finally:
        for k, v in prior.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return per, round(time.perf_counter() - t0, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path, default=DATASET)
    ap.add_argument("--stratify", type=int, default=120)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--arms", default="")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    wanted = {a.strip() for a in args.arms.split(",") if a.strip()}
    arms = [a for a in ARMS if not wanted or a[0] in wanted]

    data = json.loads(args.dataset.read_text(encoding="utf-8"))
    sample = stratified(data, args.stratify, args.seed)
    project = f"lmeflag-{int(time.time())}"
    work = REPO_ROOT / "benchmarks" / "results" / "longmemeval" / "_work"
    harness = Harness(project, work / project / "projects")

    sessions = sum(len(e["haystack_sessions"]) for _, e in sample)
    print(f"ingesting {sessions} sessions for {len(sample)} questions…",
          flush=True)
    ingest_sec = ingest(harness, sample)
    print(f"ingested in {ingest_sec}s", flush=True)

    per_base, base_sec = run_arm(harness, sample, {})
    base = summarize(per_base)
    print(f"BASELINE recall@5={base['recall@5']} "
          f"pool@50={base['pool_recall@50']} "
          f"hits={base['hits@5']}/{base['n']} ({base_sec}s)", flush=True)

    payload = {"schema": "prism.lme_flag_ab.v1", "project": project,
               "questions": len(sample), "sessions": sessions,
               "ingest_sec": ingest_sec, "baseline": base,
               "baseline_sec": base_sec, "arms": {}}

    for name, overrides in arms:
        try:
            per, sec = run_arm(harness, sample, overrides)
        except Exception as exc:
            payload["arms"][name] = {"env": overrides,
                                     "error": f"{type(exc).__name__}: {exc}"}
            print(f"{name} FAILED: {exc}", flush=True)
            continue
        summary = summarize(per)
        test = mcnemar([p["hit@5"] for p in per_base], [p["hit@5"] for p in per])
        payload["arms"][name] = {
            "env": overrides, "summary": summary, "mcnemar": test, "sec": sec,
            "delta_recall@5": round(summary["recall@5"] - base["recall@5"], 4),
            "delta_pool@50": round(
                summary["pool_recall@50"] - base["pool_recall@50"], 4),
        }
        d = payload["arms"][name]
        print(f"{name} recall@5={summary['recall@5']} "
              f"pool@50={summary['pool_recall@50']} "
              f"hits={summary['hits@5']}/{summary['n']} "
              f"d={d['delta_recall@5']:+.4f} p={test['p']} ({sec}s)", flush=True)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
