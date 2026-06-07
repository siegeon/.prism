"""LoCoMo recall benchmark for PRISM Brain (task e043f449, AC-1).

Ports MemPalace's LoCoMo metric methodology onto OUR retrieval seam: one
haystack session = one document, queried with the question, scored by whether
the gold session(s) appear in the top-k pool. Reports recall_any / recall_all
PLUS a temporal split (temporal_recall counts ONLY temporal questions) so a
ranking-stage temporal boost can be A/B'd on the split MemPalace exposes.

All numbers are MEASURED on PRISM Brain via `brain_search` — never quoted from
MemPalace's results jsonl. Runs against the isolated bench service (port 18081),
never the real PRISM service.

Usage:
    python run.py --dataset ../data/locomo.json --output ../results/locomo/run.json --tag locomo-baseline

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

# Result schema persisted per run. temporal_recall is the LoCoMo temporal
# split; recall_any/recall_all mirror MemPalace's session-recall metrics.
RESULT_KEYS: tuple[str, ...] = (
    "tag", "recall_any", "recall_all", "temporal_recall",
    "n", "n_temporal", "by_category", "per_question",
)

# LoCoMo question categories that count as the temporal split.
TEMPORAL_CATEGORIES: frozenset[str] = frozenset({"temporal", "temporal_reasoning"})


def recall_any(retrieved: list[str], gold: list[str]) -> bool:
    """Hit when ANY gold session is in the retrieved pool."""
    rset = set(retrieved)
    return any(g in rset for g in gold)


def recall_all(retrieved: list[str], gold: list[str]) -> bool:
    """Hit only when EVERY gold session is in the retrieved pool."""
    rset = set(retrieved)
    return bool(gold) and all(g in rset for g in gold)


# ---------------------------------------------------------------------------
# MCP client (stateless) — identical contract to longmemeval/run.py
# ---------------------------------------------------------------------------

def mcp_call(project: str, tool: str, arguments: dict[str, Any]) -> dict:
    url = f"{MCP_BASE}?project={project}"
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
    """Strip the ``locomo/`` prefix and any ``::chunk`` suffix so a pool entry
    matches a bare session id regardless of granularity (file/window/entity)."""
    if not doc_id:
        return ""
    head = doc_id.split("::", 1)[0]
    return head.rsplit("/", 1)[-1]


def run_one(project: str, q_idx: int, entry: dict, k: int = 50) -> dict:
    """Ingest a question's haystack, query the Brain search seam, and score
    recall_any/recall_all plus the temporal flag. Drives `brain_search`."""
    domain = f"locomo_q{q_idx:03d}"

    for sid, turns in zip(entry["haystack_session_ids"],
                          entry["haystack_sessions"]):
        mcp_call(project, "brain_index_doc", {
            "path": f"locomo/{sid}",
            "content": format_session(turns),
            "domain": domain,
        })

    resp = mcp_call(project, "brain_search",
                    {"query": entry["question"], "domain": domain, "limit": k})
    payload = parse_result(resp) or []
    if isinstance(payload, dict):
        payload = payload.get("results") or payload.get("matches") or []
    retrieved = [_session_id_of((item or {}).get("doc_id", "")) for item in payload]
    retrieved = [r for r in retrieved if r]

    gold = list(entry.get("answer_session_ids", []))
    is_temporal = entry.get("category", "") in TEMPORAL_CATEGORIES

    return {
        "question_id": entry.get("question_id"),
        "category": entry.get("category", ""),
        "is_temporal": is_temporal,
        "recall_any": recall_any(retrieved, gold),
        "recall_all": recall_all(retrieved, gold),
        "retrieved_session_ids": retrieved,
        "gold_session_ids": gold,
    }


def summarize(per_q: list[dict]) -> dict:
    """Aggregate recall_any / recall_all overall, and temporal_recall over ONLY
    the temporal questions, plus per-category recall_any."""
    n = len(per_q)
    n_any = sum(1 for r in per_q if r.get("recall_any"))
    n_all = sum(1 for r in per_q if r.get("recall_all"))
    temporal = [r for r in per_q if r.get("is_temporal")]
    n_temporal = len(temporal)
    t_hits = sum(1 for r in temporal if r.get("recall_any"))

    by_category: dict[str, dict] = {}
    for r in per_q:
        cat = r.get("category", "unknown")
        b = by_category.setdefault(cat, {"n": 0, "hits": 0})
        b["n"] += 1
        if r.get("recall_any"):
            b["hits"] += 1
    for b in by_category.values():
        b["recall"] = b["hits"] / b["n"] if b["n"] else 0.0

    return {
        "recall_any": n_any / n if n else 0.0,
        "recall_all": n_all / n if n else 0.0,
        "temporal_recall": t_hits / n_temporal if n_temporal else 0.0,
        "n": n,
        "n_temporal": n_temporal,
        "by_category": by_category,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", type=Path,
                    default=Path(__file__).resolve().parent.parent / "data" / "locomo.json")
    ap.add_argument("--project", default="bench-locomo")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    with open(args.dataset, encoding="utf-8") as f:
        data = json.load(f)
    if args.limit:
        data = data[: args.limit]

    try:
        mcp_call(args.project, "project_list", {})
    except Exception as e:
        print(f"ERROR: bench MCP not reachable at {MCP_BASE} ({e})", file=sys.stderr)
        return 2
    mcp_call(args.project, "project_create", {"project_id": args.project})

    t0 = time.perf_counter()
    per_q = [run_one(args.project, i, entry) for i, entry in enumerate(data)]
    summary = summarize(per_q)
    summary["tag"] = args.tag
    summary["elapsed_sec"] = round(time.perf_counter() - t0, 1)
    summary["per_question"] = per_q

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    print(f"RESULT [{args.tag or 'untagged'}]: recall_any={summary['recall_any']:.4f} "
          f"recall_all={summary['recall_all']:.4f} "
          f"temporal_recall={summary['temporal_recall']:.4f} "
          f"({summary['n_temporal']} temporal q)", file=sys.stderr)
    print(f"Wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
