"""Read task-linked engine receipts. Never infer a successful or zero-cost run."""
import json
from datetime import datetime
from urllib.parse import quote

def project_task_runs(project, task_id, engine):
    payload = engine(f"/workflows/history/{quote(project, safe='')}?limit=200&taskId={quote(task_id, safe='')}")
    runs = []
    for run in payload.get("runs", []):
        data = run.get("data") or {}
        variables = data.get("vars") or {}
        if data.get("project") != project or task_id not in (variables.get("taskId"), variables.get("task_id")):
            continue
        definition = data.get("definition") or {}
        results = {r.get("step"): r for r in data.get("results", [])}
        times = {t.get("step"): t for t in run.get("timeline", [])}
        steps = []
        for declared in definition.get("steps", []):
            sid = declared.get("id")
            result, timing = results.get(sid, {}), times.get(sid, {})
            output = result.get("output", "")
            inference = {}
            try:
                body = json.loads(output)
                inference = body.get("reason") if isinstance(body.get("reason"), dict) else (body.get("outputs") or {}).get("inference", {})
            except (ValueError, TypeError, AttributeError):
                pass
            live = (run.get("runtime") or {}).get("currentStep") == sid
            status = result.get("status") or ((run.get("runtime") or {}).get("status") if live else "pending")
            telemetry = result.get("telemetry") or {}
            elapsed = telemetry.get("elapsedMs")
            if elapsed is None and timing.get("startedAt") and timing.get("endedAt"):
                elapsed = (datetime.fromisoformat(timing["endedAt"]) - datetime.fromisoformat(timing["startedAt"])).total_seconds() * 1000
            steps.append({"id": sid, "kind": declared.get("kind"), "persona": declared.get("persona"),
                "status": status, "startedAt": timing.get("startedAt") or ((run.get("runtime") or {}).get("startedAt") if live else None),
                "endedAt": timing.get("endedAt"), "elapsedMs": elapsed,
                "outputBytes": len(output.encode("utf-8")) if result else None,
                "logicalInputBytes": telemetry.get("logicalInputBytes"), "exitCode": result.get("exitCode"),
                "output": output, "inferenceRunId": inference.get("run_id"), "usage": inference.get("usage")})
        runs.append({"id": run["id"], "status": run.get("status"), "name": definition.get("name") or definition.get("id"),
            "bot": definition.get("botId"), "fsm": definition.get("fsmId"), "definitionHash": run.get("definitionHash"),
            "startedAt": run.get("createTime"), "endedAt": run.get("completeTime"), "steps": steps})
    return {"runs": runs, "limit": 200, "coverage": "Step receipts and UTF-8 output bytes. Historical input bytes, unreported model usage, CPU cycles, and network bytes are not recorded. Output appears when a step completes; this is not a live token stream."}


def reported_usage(events):
    """Only a provider's final result is authoritative; absent fields stay unknown."""
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result is None or not isinstance(result.get("usage"), dict):
        return None
    usage = result["usage"]
    return {**{k: usage.get(k) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")},
            "cost_usd": result.get("total_cost_usd"),
            "model": ", ".join((result.get("modelUsage") or {}).keys()) or None}
