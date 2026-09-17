import json
from prism_service.services.task_execution import project_task_runs

def run(task="t", usage=None):
    return {"id":"r","status":"Failed","createTime":"2026-09-17T00:00:00Z", "timeline":[{"step":"infer","startedAt":"2026-09-17T00:00:00Z","endedAt":"2026-09-17T00:00:02Z"}],
        "data":{"project":"p","vars":{"taskId":task},"definition":{"steps":[{"id":"infer"},{"id":"apply"}]},
        "results":[{"step":"infer","status":"failed","output":json.dumps({"reason":{"run_id":"model-r","usage":usage}},ensure_ascii=False)}]}}

def test_task_filter_and_failure_receipts():
    paths=[]
    result=project_task_runs("p","t",lambda path: paths.append(path) or {"runs":[run(),run("other")]})
    assert paths==["/workflows/history/p?limit=200&taskId=t"]
    assert len(result["runs"])==1
    steps=result["runs"][0]["steps"]
    assert steps[0]["status"]=="failed" and steps[1]["status"]=="pending"
    assert steps[0]["elapsedMs"]==2000
    assert steps[0]["usage"] is None
    assert steps[1]["outputBytes"] is None

def test_usage_is_preserved_without_double_counting():
    usage={"input_tokens":10,"output_tokens":3,"cache_read_input_tokens":20,"cost_usd":0.01}
    step=project_task_runs("p","t",lambda _: {"runs":[run(usage=usage)]})["runs"][0]["steps"][0]
    assert step["usage"]==usage and step["inferenceRunId"]=="model-r"
    assert step["outputBytes"]==len(step["output"].encode("utf-8"))


def test_missing_usage_is_not_zero_and_stream_chunks_are_not_summed():
    from prism_service.services.task_execution import reported_usage
    assert reported_usage([]) is None
    assert reported_usage([{"type":"result"}]) is None
    result=reported_usage([{"type":"assistant","usage":{"input_tokens":999}},
        {"type":"result","usage":{"input_tokens":0,"output_tokens":3}}])
    assert result["input_tokens"]==0 and result["output_tokens"]==3
    assert result["cost_usd"] is None and result["cache_read_input_tokens"] is None
