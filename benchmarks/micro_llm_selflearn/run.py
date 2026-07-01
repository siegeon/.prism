"""Micro-LLM self-learning benchmark (task ecb90f7e, feeds G5 9f8e7bf7).

Ranks FREE local models (OpenAI-compatible endpoint, default Ollama
:11434) on the three payload shapes PRISM's self-learning loop actually
needs, measuring wall latency + tokens/s + task success:

  T1 reflect_verdict — strict JSON {worth_learning, new_memories[]} from a
     session brief (the reflection_runner shape; the load-bearing task).
  T2 distill         — one-line memory summary carrying the key fact
     (the memory-summary worker shape).
  T3 tool_call       — emit the right tool call for a question, native
     tool_calls OR bare-JSON text (the PI panel / micro-model reality).

Winner rule: fastest median latency among models with T1 success >= 0.8.
Writes benchmarks/results/micro_llm_selflearn/latest.json. Stdlib only.

Usage:
  python benchmarks/micro_llm_selflearn/run.py                # full ladder
  python benchmarks/micro_llm_selflearn/run.py --models qwen3:0.6b,gemma3:1b
  python benchmarks/micro_llm_selflearn/run.py --check        # no network
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ENDPOINT = "http://localhost:11434"
LADDER = [
    "qwen3:0.6b", "gemma3:1b", "llama3.2:1b", "qwen3:1.7b",
    "llama3.2:3b", "qwen3:4b", "qwen2.5-coder:7b", "dolphin-llama3:8b",
]
REPS = 3
RESULTS = Path(__file__).resolve().parent.parent / "results" / "micro_llm_selflearn"
RULE = ("fastest reflect_verdict p50 among models with reflect_verdict "
        "success >= 0.9 (unattended memory writes need reliability first)")

SESSION_BRIEF = (
    "Session brief: the PRISM daemon spawned with DETACHED_PROCESS made every "
    "worker subprocess (claude -p, git) flash a visible conhost window on "
    "Windows; the fix switched the daemon + supervisor spawn flags to "
    "CREATE_NO_WINDOW (hidden console inherited by all children) in "
    "cli/prism_cli.py and services/supervisor.py, keeping "
    "CREATE_BREAKAWAY_FROM_JOB for durability. Shipped as v6.7.25."
)

LONG_MEMORY = (
    "The PRISM daemon and its supervisor previously used DETACHED_PROCESS "
    "(0x00000008) when spawning on Windows. A detached process has no console, "
    "so every console-subprocess it launched (claude -p runs from the "
    "reflection loop, git calls from drift checks) allocated a brand-new "
    "visible conhost window, which users experienced as terminal windows "
    "spamming the machine whenever background workers ran. The fix replaced "
    "DETACHED_PROCESS with CREATE_NO_WINDOW (0x08000000) in both "
    "cli/prism_cli.py _spawn and services/supervisor.py _spawn_server "
    "including the breakaway fallbacks: the daemon gets a hidden console that "
    "all children inherit silently, while CREATE_BREAKAWAY_FROM_JOB is kept "
    "so the daemon still escapes kill-on-close job objects."
)

TOOLS = [{
    "type": "function",
    "function": {
        "name": "task_list",
        "description": "List tasks in the PRISM tracker, optionally filtered by status.",
        "parameters": {
            "type": "object",
            "properties": {"status": {"type": "string", "description": "pending|in_progress|done|blocked"}},
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "brain_search",
        "description": "Search the project's code and knowledge base.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}]


# ---------------------------------------------------------------- scoring

def strip_json(text: str) -> str:
    t = (text or "").strip()
    if t.startswith("```"):
        lines = [l for l in t.splitlines() if not l.strip().startswith("```")]
        t = "\n".join(lines).strip()
    # tolerate prose-before-JSON (the reflection parser lesson)
    if t[:1] in ("{", "["):
        return t
    starts = [i for i in (t.find("{"), t.find("[")) if i >= 0]
    return t[min(starts):] if starts else t


def score_t1(text: str) -> bool:
    """Valid reflection verdict: parses, has worth_learning + new_memories;
    a claimed memory must carry the key fact."""
    try:
        obj = json.loads(strip_json(text))
    except ValueError:
        return False
    if not isinstance(obj, dict) or "worth_learning" not in obj:
        return False
    mems = obj.get("new_memories")
    if not isinstance(mems, list):
        return False
    if obj.get("worth_learning") and mems:
        blob = json.dumps(mems).lower()
        return "create_no_window" in blob or "no_window" in blob or "hidden console" in blob
    return True


def score_t2(text: str) -> bool:
    t = (text or "").strip().strip('"')
    return 0 < len(t) <= 260 and ("create_no_window" in t.lower() or "hidden console" in t.lower())


def score_t3(text: str, tool_calls: list | None) -> bool:
    if tool_calls:
        for tc in tool_calls:
            fn = (tc.get("function") or {}) if isinstance(tc, dict) else {}
            if fn.get("name") == "task_list":
                return True
        return False
    try:
        obj = json.loads(strip_json(text))
    except ValueError:
        return False
    if not isinstance(obj, dict):
        return False
    name = obj.get("name") or (obj.get("function") or {}).get("name") if isinstance(obj.get("function"), dict) else obj.get("name")
    return name == "task_list"


TASKS = {
    "reflect_verdict": {
        "system": (
            "You are PRISM's reflection engine. Decide whether the session brief "
            "contains durable engineering knowledge. Respond with ONLY a JSON "
            "object: {\"worth_learning\": bool, \"new_memories\": [{\"domain\": str, "
            "\"name\": str, \"description\": str}]}. No prose, no markdown."
        ),
        "prompt": SESSION_BRIEF,
        "score": score_t1,
        "json_mode": True,
    },
    "distill": {
        "system": (
            "Summarize the memory entry in ONE line (max 200 characters) that "
            "keeps the load-bearing technical fact. Output only the line."
        ),
        "prompt": LONG_MEMORY,
        "score": score_t2,
        "json_mode": False,
    },
    "tool_call": {
        "system": (
            "You answer using tools. Call the appropriate tool; do not answer "
            "from memory."
        ),
        "prompt": "What tasks are pending?",
        "score": None,  # scored via score_t3 (needs tool_calls)
        "json_mode": False,
        "tools": TOOLS,
    },
}


# ------------------------------------------------------------------ engine

def chat(endpoint: str, model: str, task: dict, timeout: float = 240.0) -> dict:
    system = task["system"]
    if model.startswith("qwen3"):
        system += " /no_think"  # disable thinking for speed on qwen3
    body: dict = {
        "model": model,
        "stream": False,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": task["prompt"]},
        ],
        "max_tokens": 512,
        "temperature": 0,
    }
    if task.get("json_mode"):
        body["response_format"] = {"type": "json_object"}
    if task.get("tools"):
        body["tools"] = task["tools"]
    req = urllib.request.Request(
        f"{endpoint}/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as res:
        payload = json.loads(res.read())
    ms = (time.perf_counter() - t0) * 1000.0
    msg = (payload.get("choices") or [{}])[0].get("message") or {}
    usage = payload.get("usage") or {}
    return {
        "text": msg.get("content") or "",
        "tool_calls": msg.get("tool_calls"),
        "ms": ms,
        "tokens": usage.get("completion_tokens") or 0,
    }


def installed_models(endpoint: str) -> dict[str, int]:
    """name -> size_mb for every installed model ('' size when unknown)."""
    try:
        with urllib.request.urlopen(f"{endpoint}/api/tags", timeout=10) as res:
            data = json.loads(res.read())
        return {
            m.get("name", ""): int((m.get("size") or 0) / 1_000_000)
            for m in data.get("models", [])
        }
    except (urllib.error.URLError, OSError):
        return {}


def bench_model(endpoint: str, model: str, reps: int) -> dict:
    out: dict = {"model": model, "tasks": {}, "all_ms": []}
    # one throwaway warm call so load time doesn't pollute latency
    try:
        chat(endpoint, model, {"system": "Reply with: ok", "prompt": "ok?"})
    except Exception as exc:  # noqa: BLE001
        return {"model": model, "error": f"warm-up failed: {exc}"}
    for name, task in TASKS.items():
        oks, mss, toks = [], [], []
        for _ in range(reps):
            try:
                r = chat(endpoint, model, task)
            except Exception as exc:  # noqa: BLE001
                oks.append(False)
                print(f"    {model} {name}: ERROR {exc}", file=sys.stderr)
                continue
            ok = score_t3(r["text"], r["tool_calls"]) if name == "tool_call" else task["score"](r["text"])
            oks.append(bool(ok))
            mss.append(r["ms"])
            toks.append((r["tokens"], r["ms"]))
        out["tasks"][name] = {
            "success": round(sum(oks) / max(1, len(oks)), 3),
            "p50_ms": round(statistics.median(mss), 1) if mss else None,
            "tok_s": round(
                sum(t for t, _ in toks) / max(0.001, sum(m for _, m in toks) / 1000.0), 1,
            ) if toks else None,
        }
        out["all_ms"].extend(mss)
    out["p50_ms"] = round(statistics.median(out["all_ms"]), 1) if out["all_ms"] else None
    del out["all_ms"]
    return out


def pick_winner(rows: list[dict]) -> dict | None:
    """Rank on the LOAD-BEARING shape: reflection-verdict latency. The
    internal self-learning engine runs T1 continuously; tool_call latency
    is the panel's concern, and mixing it in crowned a bigger model whose
    T1 was actually slower (7b 4201ms vs 0.6b 3252ms, first ladder run).
    Eligibility is 0.9, not 0.8: this engine writes memories UNATTENDED,
    and a model that mis-verdicts 1-in-5 (gemma3:1b sat at exactly 4/5 in
    the confirmation run) poisons the store at scale — reliability first,
    then speed."""
    eligible = [
        r for r in rows
        if not r.get("error")
        and (r["tasks"].get("reflect_verdict") or {}).get("success", 0) >= 0.9
        and (r["tasks"].get("reflect_verdict") or {}).get("p50_ms")
    ]
    if not eligible:
        return None
    best = min(eligible, key=lambda r: r["tasks"]["reflect_verdict"]["p50_ms"])
    return {
        "model": best["model"],
        "t1_p50_ms": best["tasks"]["reflect_verdict"]["p50_ms"],
        "size_mb": best.get("size_mb"),
        "tasks": best["tasks"],
    }


# ------------------------------------------------------------------- main

def self_check() -> int:
    """Validate scoring on canned outputs — no network (used by pytest)."""
    assert score_t1('{"worth_learning": true, "new_memories": [{"domain": "d", "name": "n", "description": "use CREATE_NO_WINDOW"}]}')
    assert score_t1('```json\n{"worth_learning": false, "new_memories": []}\n```')
    assert score_t1('Sure! Here it is: {"worth_learning": false, "new_memories": []}')
    assert not score_t1("not json at all")
    assert not score_t1('{"new_memories": []}')
    assert score_t2("Daemon now spawns with CREATE_NO_WINDOW so children inherit a hidden console.")
    assert not score_t2("Too vague a summary." )
    assert score_t3("", [{"function": {"name": "task_list", "arguments": "{}"}}])
    assert score_t3('{"name": "task_list", "arguments": {"status": "pending"}}', None)
    assert not score_t3('{"name": "brain_search", "arguments": {}}', None)
    for t in TASKS.values():
        assert t["system"] and t["prompt"]
    print("self-check OK: 3 tasks defined, scoring validated")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--models", default=",".join(LADDER))
    ap.add_argument("--reps", type=int, default=REPS)
    ap.add_argument("--check", action="store_true", help="validate task defs + scoring, no network")
    ap.add_argument("--rerank", action="store_true",
                    help="recompute the winner from the stored latest.json (no inference)")
    args = ap.parse_args()
    if args.check:
        return self_check()
    if args.rerank:
        path = RESULTS / "latest.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["winner"] = pick_winner(data.get("models") or [])
        data["rule"] = RULE
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"winner: {json.dumps(data['winner'])}")
        return 0

    have = installed_models(args.endpoint)
    if not have:
        print(f"no models visible at {args.endpoint} — is the server up?", file=sys.stderr)
        return 2
    rows: list[dict] = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        if model not in have:
            rows.append({"model": model, "error": "not installed — skipped"})
            print(f"-- {model}: not installed, skipped")
            continue
        print(f"-- {model}")
        row = bench_model(args.endpoint, model, args.reps)
        row["size_mb"] = have.get(model)
        rows.append(row)
        if not row.get("error"):
            t = row["tasks"]
            print(
                f"   p50 {row['p50_ms']}ms | verdict {t['reflect_verdict']['success']:.0%}"
                f" | distill {t['distill']['success']:.0%} | tool {t['tool_call']['success']:.0%}"
            )
    winner = pick_winner(rows)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": args.endpoint,
        "reps": args.reps,
        "rule": RULE,
        "models": rows,
        "winner": winner,
    }
    (RESULTS / "latest.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwinner: {json.dumps(winner) if winner else 'NONE eligible'}")
    print(f"wrote {RESULTS / 'latest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
