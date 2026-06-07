"""ConvoMem recall benchmark for PRISM Brain (task e043f449, AC-2).

Ports MemPalace's ConvoMem metric methodology onto OUR retrieval seam: score
session recall across the 6 conversational categories, driving `brain_search`.
Reports aggregate recall plus per-category recall so a weakness in any one
conversational mode (e.g. multi-session, temporal-reasoning) is visible.

All numbers are MEASURED on PRISM Brain — never quoted from MemPalace's results
jsonl. Runs against the isolated bench service (port 18081).

Usage:
    python run.py --dataset ../data/convomem.json --output ../results/convomem/run.json --tag convomem-baseline

[Source: benchmarks/longmemeval/run.py — established harness pattern]
[Source: services/prism-service/prism_service/engines/brain_engine.py::Brain.search :2511]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

MCP_BASE = "http://localhost:18081/mcp/"

# The 6 ConvoMem evidence categories — the real dirs under
# Salesforce/ConvoMem core_benchmark/evidence_questions (verified against the
# HF tree @ 939a076-era), NOT the earlier guessed single-session-* names.
CATEGORIES: tuple[str, ...] = (
    "user_evidence",
    "assistant_facts_evidence",
    "changing_evidence",
    "abstention_evidence",
    "preference_evidence",
    "implicit_connection_evidence",
)

RESULT_KEYS: tuple[str, ...] = (
    "tag", "recall", "by_category", "n", "per_question",
)


def recall_any(retrieved: list[str], gold: list[str]) -> bool:
    rset = set(retrieved)
    return any(g in rset for g in gold)


# ---------------------------------------------------------------------------
# MCP client (stateless) — identical contract to longmemeval/run.py
# ---------------------------------------------------------------------------

def mcp_call(project: str, tool: str, arguments: dict[str, Any]) -> dict:
    url = f"{MCP_BASE}?project={project}&tool_profile=all"  # maintenance tools (brain_index_doc) need the admin profile
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": tool, "arguments": arguments}}
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read().decode()
        if "text/event-stream" in r.headers.get("Content-Type", ""):
            for line in raw.splitlines():
                if line.startswith("data: "):
                    return json.loads(line[6:])
        return json.loads(raw)


def parse_result(resp: dict) -> Any:
    if "error" in resp:
        raise RuntimeError(f"MCP error: {resp['error']}")
    content = resp.get("result", {}).get("content", [])
    if not content:
        return None
    text = content[0].get("text", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def format_session(turns: list[dict]) -> str:
    return "\n\n".join(f"{t.get('role', 'user')}: {t.get('content', '')}"
                       for t in turns)


def _session_id_of(doc_id: str) -> str:
    if not doc_id:
        return ""
    head = doc_id.split("::", 1)[0]
    return head.rsplit("/", 1)[-1]


def ingest_session(project: str, sid: str, turns: list[dict], domain: str) -> None:
    mcp_call(project, "brain_index_doc", {
        "path": f"convomem/{sid}", "content": format_session(turns), "domain": domain,
    })


def run_one(project: str, q_idx: int, entry: dict, k: int = 50,
            domain: str | None = None) -> dict:
    """Query the Brain search seam for one question against the (pre-ingested)
    shared corpus domain and score a session hit. Drives `brain_search`. If
    ``domain`` is None the haystack is ingested first (standalone/unit use);
    main() ingests the full corpus ONCE and passes the shared domain in — never
    once per question, which would overwrite shared paths (Brain dedupes by
    path) and break retrieval."""
    if domain is None:
        domain = f"convomem_q{q_idx:03d}"
        for sid, turns in zip(entry["haystack_session_ids"], entry["haystack_sessions"]):
            ingest_session(project, sid, turns, domain)

    resp = mcp_call(project, "brain_search",
                    {"query": entry["question"], "domain": domain, "limit": k})
    payload = parse_result(resp) or []
    if isinstance(payload, dict):
        payload = payload.get("results") or payload.get("matches") or []
    retrieved = [_session_id_of((item or {}).get("doc_id", "")) for item in payload]
    retrieved = [r for r in retrieved if r]

    gold = list(entry.get("answer_session_ids", []))
    return {
        "question_id": entry.get("question_id"),
        "category": entry.get("category", ""),
        "hit": recall_any(retrieved, gold),
        "retrieved_session_ids": retrieved,
        "gold_session_ids": gold,
    }


def summarize(per_q: list[dict]) -> dict:
    """Aggregate recall overall and per conversational category."""
    n = len(per_q)
    hits = sum(1 for r in per_q if r.get("hit"))

    by_category: dict[str, dict] = {}
    for r in per_q:
        cat = r.get("category", "unknown")
        b = by_category.setdefault(cat, {"n": 0, "hits": 0})
        b["n"] += 1
        if r.get("hit"):
            b["hits"] += 1
    for b in by_category.values():
        b["recall"] = b["hits"] / b["n"] if b["n"] else 0.0

    return {
        "recall": hits / n if n else 0.0,
        "by_category": by_category,
        "n": n,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path,
                    default=Path(__file__).resolve().parent.parent / "data" / "convomem.json")
    ap.add_argument("--project", default="bench-convomem")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--k", type=int, default=10,
                    help="retrieval pool size; keep below the corpus session "
                         "count or recall saturates to 1.0")
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    with open(args.dataset, encoding="utf-8") as f:
        data = json.load(f)
    questions = data[: args.limit] if args.limit else data

    try:
        mcp_call(args.project, "project_list", {})
    except Exception as e:
        print(f"ERROR: bench MCP not reachable at {MCP_BASE} ({e})", file=sys.stderr)
        return 2
    mcp_call(args.project, "project_create", {"project_id": args.project})

    # Ingest the FULL corpus once into one shared domain (see run_one docstring).
    t0 = time.perf_counter()
    domain = "convomem_all"
    seen: set[str] = set()
    for entry in data:
        for sid, turns in zip(entry["haystack_session_ids"], entry["haystack_sessions"]):
            if sid in seen:
                continue
            seen.add(sid)
            ingest_session(args.project, sid, turns, domain)
    per_q = [run_one(args.project, i, entry, k=args.k, domain=domain)
             for i, entry in enumerate(questions)]
    summary = summarize(per_q)
    summary["tag"] = args.tag
    summary["elapsed_sec"] = round(time.perf_counter() - t0, 1)
    summary["per_question"] = per_q

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"RESULT [{args.tag or 'untagged'}]: recall={summary['recall']:.4f} "
          f"({summary['n']} q across {len(summary['by_category'])} categories)",
          file=sys.stderr)
    print(f"Wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
