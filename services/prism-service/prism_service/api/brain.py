"""Brain API — status, search, reindex, ask.

Thin read-through wrappers over BrainService (project_context.get_project(p).brain_svc).

`/ask` shells out to the `claude` CLI with retrieved Brain context so the
SPA can offer a Q&A surface on top of the hybrid retrieval index. The
retrieval step uses Brain's normal search() to pull the top-N hits; the
question + those hits are then formatted into a prompt and run through
claude_cli.invoke(). Allowed-tools is empty so claude doesn't run a
subagent loop — single-shot Q&A.

PRISM_BRAIN_ASK_BACKEND=local (task e1ebdd6e, claude-p-exit epic) routes
the SAME grounded prompt at the keyless local micro model
(inference.local_llm) as one completion — the prompt already forbids
tool calls, so single-shot semantics are preserved. The response
contract is unchanged; run_id + tokens come from the run-ledger seam so
the SPA ask panel keeps its receipts. Default stays the claude CLI.
"""

import os
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from prism_service.inference import claude_cli
from prism_service.project_context import get_project
from prism_service.services import source_service as ss
from prism_service.services import understand_view

router = APIRouter()


def _svc(project: str):
    try:
        return get_project(project).brain_svc
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"unknown project: {project}: {exc}")


@router.get("/status")
def status(project: str = Query("default")) -> dict:
    return _svc(project).status()


@router.get("/search")
def search(
    q: str = Query(..., min_length=1),
    project: str = Query("default"),
    domain: str | None = Query(None),
    limit: int = Query(20, ge=1, le=200),
) -> dict:
    results = _svc(project).search(q.strip(), domain=domain, limit=limit)
    return {"query": q, "domain": domain, "results": results}


@router.post("/reindex")
def reindex(project: str = Query("default")) -> dict:
    count = _svc(project).incremental_reindex()
    return {"reindexed": count}


class UnderstandBody(BaseModel):
    # Empty/omitted query => whole-graph overview ranked by centrality.
    query: str | None = None
    project: str = "default"
    limit: int = 20
    depth: int = 1
    # Cluster/selection click-through: when the canvas posts a clicked
    # community / super-node / node, its member files arrive here and
    # drive a focus payload seeded by them (label is the cluster name).
    seed_files: list[str] | None = None
    label: str | None = None
    # Filter the search to one slice of the brain (e.g. 'expertise', 'md')
    # to reach unstructured domain knowledge / docs, not just code.
    domain: str | None = None


@router.post("/understand")
def understand(body: UnderstandBody) -> dict:
    """Ultimate Graph merge (siegeon/.prism#50, slice 4).

    One retrieval that fuses the Brain search lens and the Graph
    spatial lens into a single ranked, graph-aware payload. Backs the
    /explore SPA page and mirrors the `brain_understand` MCP tool.
    """
    try:
        return understand_view.build_understanding(
            body.project,
            body.query,
            limit=max(1, min(int(body.limit), 200)),
            depth=max(0, min(int(body.depth), 3)),
            seed_files=body.seed_files or None,
            label=body.label,
            domain=(body.domain or None),
        )
    except Exception as exc:  # unknown project, empty graph, etc.
        raise HTTPException(404, f"understand failed for '{body.project}': {exc}")


class AskBody(BaseModel):
    q: str
    project: str = "default"
    # How many Brain hits to surface as context. 8 is a reasonable
    # balance — enough to ground claude on multiple files, few enough
    # that prompt tokens stay bounded.
    top_k: int = 8
    domain: str | None = None
    # Max turns claude is allowed. Default 1 = pure single-shot Q&A
    # from the retrieved context, no tool calls possible. Raising it
    # opens the door to claude calling Grep/Read on the source tree
    # (which claude does aggressively by default), which spikes cost
    # and frequently hits max_turns without a final answer. Cap at 5.
    max_turns: int = 1


@router.post("/ask")
def ask(body: AskBody) -> dict:
    """Ask claude a question grounded in the project's Brain index.

    1. Run Brain hybrid search (BM25 + vector + graph) for top-K hits.
    2. Format the hits + the question into a prompt.
    3. Shell out to `claude -p` via claude_cli.invoke().
    4. Return the answer + the sources we cited.

    Costs the user's Claude subscription one CLI invocation per call.
    """
    q = (body.q or "").strip()
    if not q:
        raise HTTPException(400, "q is required")
    if body.max_turns < 1 or body.max_turns > 5:
        raise HTTPException(400, "max_turns must be in [1, 5]")

    svc = _svc(body.project)
    results = svc.search(q, domain=body.domain, limit=body.top_k) or []

    # Format the retrieved chunks as a context block. Each hit gets a
    # path/score header so claude can cite by file. We trim the body
    # to ~500 chars per hit to keep prompt tokens reasonable; if the
    # caller wants more they can drop max_turns and let claude Read()
    # the file directly (separate flow).
    context_blocks: list[str] = []
    for i, r in enumerate(results, 1):
        path = r.get("source_file") or r.get("file") or r.get("source") or "—"
        ent = r.get("entity_name") or r.get("doc_id") or ""
        kind = r.get("entity_kind") or ""
        score = r.get("rrf_score") or r.get("score") or 0.0
        body_text = (r.get("content") or r.get("body") or "")[:800]
        header = f"### [{i}] {path}"
        if ent:
            header += f" · {ent}" + (f" ({kind})" if kind else "")
        header += f"  · score {score:.3f}"
        context_blocks.append(f"{header}\n{body_text}")
    context = "\n\n".join(context_blocks) or "(no matching context found)"

    prompt = (
        f"You are answering a question about the `{body.project}` codebase, "
        f"grounded in the retrieved Brain context below.\n\n"
        f"IMPORTANT — single-turn Q&A rules:\n"
        f"- Do NOT call any tools (no Grep, Read, Glob, Bash, etc.).\n"
        f"- Answer ONLY from the context block below.\n"
        f"- Cite source files by their bracketed index (e.g. [1], [3]) "
        f"when they back up a specific claim.\n"
        f"- If the context doesn't contain enough to answer, say so "
        f"plainly — don't invent details.\n"
        f"- Reply with the answer text directly, no preamble like "
        f"\"I'll analyze...\" or \"Let me look at...\".\n\n"
        f"## Retrieved context (top {len(results)} hits)\n\n"
        f"{context}\n\n"
        f"## Question\n\n"
        f"{q}\n\n"
        f"## Answer\n"
    )

    # Env-gated backend seam (task e1ebdd6e): the local micro model
    # serves the same grounded prompt as a single completion.
    backend = (os.environ.get("PRISM_BRAIN_ASK_BACKEND") or "").strip().lower()
    if backend == "local":
        return _ask_local(body, q, prompt, results)

    # cwd = the project's source dir, so if claude's allowed_tools
    # ever gets extended to include Read/Glob/Grep it can navigate
    # the tree. For v1 we leave allowed_tools empty (no tool calls,
    # pure text Q&A), which matches the user-subscribed Claude billing
    # model — no MCP roundtrips inside the answer.
    try:
        work_dir = ss.source_dir_for(body.project)
    except Exception:
        work_dir = Path.cwd()

    res = claude_cli.invoke(
        prompt,
        work_dir=work_dir,
        plugin_dir=str(work_dir),
        max_turns=body.max_turns,
        parse_events=True,
        project=body.project,
        purpose=f"brain_ask@{q[:40]}",
        allowed_tools=(),
    )

    answer = (res.final_text() or "").strip() or "(no answer returned)"
    return {
        "question": q,
        "project": body.project,
        "answer": answer,
        "sources": _sources_payload(results),
        "run_id": res.run_id,
        "exit_code": res.exit_code,
        "duration_s": res.duration_s,
        "tokens": {
            "input": res.usage.get("input_tokens", 0),
            "output": res.usage.get("output_tokens", 0),
        },
    }


def _sources_payload(results: list) -> list[dict]:
    """The retrieval receipts rendered by the SPA ask panel — identical
    for every backend (the sources come from Brain, not the model)."""
    return [
        {
            "index": i + 1,
            "file": (r.get("source_file") or r.get("file") or r.get("source") or ""),
            "entity_name": r.get("entity_name") or r.get("doc_id") or "",
            "entity_kind": r.get("entity_kind") or "",
            "score": r.get("rrf_score") or r.get("score") or 0.0,
        }
        for i, r in enumerate(results)
    ]


def _ask_local(body: AskBody, q: str, prompt: str, results: list) -> dict:
    """Local-backend leg of /ask (task e1ebdd6e): one keyless
    local_llm.complete completion over the SAME grounded prompt. The
    response contract matches the claude path — run_id comes from the
    run-ledger seam (record_local_run) and tokens from local_llm's
    additive input/output split, so the SPA keeps its telemetry.
    An unreachable endpoint is a 502, never a fabricated answer."""
    from prism_service.inference import local_llm

    purpose = f"brain_ask@{q[:40]}"
    mdl = local_llm.configured_model()
    ts_start = time.time()
    try:
        out = local_llm.complete(
            prompt,
            model=mdl,
            max_tokens=1024,
            purpose=purpose,
            project=body.project,
        )
    except Exception as exc:
        raise HTTPException(
            502, f"local ask backend failed: {type(exc).__name__}: {exc}")
    ts_end = time.time()
    text = (out.get("text") or "").strip()
    usage = {
        "input_tokens": int(out.get("input_tokens") or 0),
        "output_tokens": int(out.get("output_tokens")
                             or out.get("tokens") or 0),
    }
    run_id = ""
    try:
        from prism_service.services import claude_run_log

        run_id = claude_run_log.record_local_run(
            project=body.project, purpose=purpose,
            backend="local", model=mdl,
            final_text=text, usage=usage,
            ts_start=ts_start, ts_end=ts_end,
        )
    except Exception:
        pass  # ledger is best-effort — never fail the ask
    return {
        "question": q,
        "project": body.project,
        "answer": text or "(no answer returned)",
        "sources": _sources_payload(results),
        "run_id": run_id,
        "exit_code": 0,
        "duration_s": round(ts_end - ts_start, 3),
        "tokens": {
            "input": usage["input_tokens"],
            "output": usage["output_tokens"],
        },
    }
