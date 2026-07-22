"""PRIMARY of task 45e04fad — honest background-inference spend accounting.

The bug: claude_cli._parse_jsonl sums `usage` across every stream event
(multi-counting one call) and drops cache_read/cache_creation; the manifest
therefore records inflated, incomplete numbers. The authoritative totals are
on the single `type=="result"` event (all four fields + total_cost_usd + the
real model). These tests pin the corrected behaviour.
"""
import json

from prism_service.inference import claude_cli
from prism_service.services import claude_run_log as crl


# A real result event's usage shape (see data/claude_runs/*.jsonl): cache
# tokens dwarf the tracked input/output.
RESULT_USAGE = {
    "input_tokens": 2601,
    "output_tokens": 17373,
    "cache_read_input_tokens": 1028844,
    "cache_creation_input_tokens": 97540,
}


def _write_stream(path, assistant_usages, result_usage, total_cost_usd, model):
    """Emit a claude -p stream-json file: system + N assistant events (each
    carrying a partial usage snapshot) + one authoritative result event."""
    lines = [{"type": "system", "subtype": "init"}]
    for u in assistant_usages:
        lines.append({"type": "assistant",
                      "message": {"model": model, "usage": u,
                                  "content": [{"type": "text", "text": "hi"}]}})
    lines.append({"type": "result", "subtype": "success",
                  "usage": result_usage,
                  "total_cost_usd": total_cost_usd,
                  "modelUsage": {model: {"costUSD": total_cost_usd}}})
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


def _isolate_manifest(tmp_path, monkeypatch):
    runs = tmp_path / "claude_runs"
    runs.mkdir()
    monkeypatch.setattr(crl, "_RUNS_DIR", runs)
    monkeypatch.setattr(crl, "_MANIFEST", runs / "manifest.jsonl")
    return runs


def test_parse_jsonl_takes_result_usage_once_all_four_fields(tmp_path):
    """AC-1: usage comes from the result event ONCE (not the cross-event sum),
    with all four fields + real cost + model."""
    p = tmp_path / "run.jsonl"
    _write_stream(
        p,
        assistant_usages=[{"input_tokens": 5, "output_tokens": 8},
                          {"input_tokens": 5, "output_tokens": 8},
                          {"input_tokens": 40, "output_tokens": 120}],
        result_usage=RESULT_USAGE, total_cost_usd=1.571377,
        model="claude-opus-4-7[1m]",
    )
    _parsed, usage = claude_cli._parse_jsonl(p)
    # The cross-event SUM would inflate input to 2651 — must be the result's 2601.
    assert usage["input_tokens"] == 2601
    assert usage["output_tokens"] == 17373
    assert usage["cache_read_input_tokens"] == 1028844
    assert usage["cache_creation_input_tokens"] == 97540
    assert round(usage["cost_usd"], 6) == 1.571377
    assert usage["model"] == "claude-opus-4-7[1m]"


def test_record_run_persists_cache_cost_model_and_version(tmp_path, monkeypatch):
    """AC-2: the manifest entry carries all four fields + cost + model +
    an accounting_version marker distinguishing corrected rows."""
    runs = _isolate_manifest(tmp_path, monkeypatch)
    stream = runs / "abc.jsonl"
    stream.write_text("{}", encoding="utf-8")
    usage = dict(RESULT_USAGE, cost_usd=1.571377, model="claude-opus-4-7")
    crl.record_run(run_id="abc", stream_path=stream, ts_start=0.0, ts_end=1.0,
                   project="prism", purpose="graph_enrich", exit_code=0,
                   usage=usage, stderr_text="")
    entry = json.loads((runs / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert entry["cache_read_input_tokens"] == 1028844
    assert entry["cache_creation_input_tokens"] == 97540
    assert round(entry["cost_usd"], 6) == 1.571377
    assert entry["model"] == "claude-opus-4-7"
    assert entry.get("accounting_version")


def test_summarize_groups_by_purpose_all_fields_failures_included(tmp_path, monkeypatch):
    """AC-3: summarize buckets true spend by purpose with all four fields +
    cost + run count, failed runs included."""
    runs = _isolate_manifest(tmp_path, monkeypatch)
    stream = runs / "s.jsonl"
    stream.write_text("{}", encoding="utf-8")
    for pur, code in [("graph_enrich", 0), ("graph_enrich", 1), ("memory_summary", 0)]:
        crl.record_run(run_id=pur + str(code), stream_path=stream, ts_start=0.0,
                       ts_end=1.0, project="prism", purpose=pur, exit_code=code,
                       usage=dict(RESULT_USAGE, cost_usd=1.0, model="m"),
                       stderr_text="")
    summary = crl.summarize(group_by="purpose")
    ge = summary["graph_enrich"]
    assert ge["runs"] == 2  # includes the exit_code=1 failure
    assert ge["cache_read_input_tokens"] == 1028844 * 2
    assert round(ge["cost_usd"], 4) == 2.0
    assert "memory_summary" in summary


def test_prefix_rows_flagged_not_summed_into_corrected(tmp_path, monkeypatch):
    """AC-5: pre-fix rows (no accounting_version) are reported separately so
    old over-counted numbers are never silently mixed with corrected ones."""
    runs = _isolate_manifest(tmp_path, monkeypatch)
    (runs / "manifest.jsonl").write_text(
        json.dumps({"run_id": "old", "purpose": "graph_enrich", "exit_code": 0,
                    "input_tokens": 100, "output_tokens": 200, "tokens_used": 300,
                    "ts_start": 0.0, "ts_end": 1.0}) + "\n", encoding="utf-8")
    stream = runs / "s.jsonl"
    stream.write_text("{}", encoding="utf-8")
    crl.record_run(run_id="new", stream_path=stream, ts_start=0.0, ts_end=1.0,
                   project="prism", purpose="graph_enrich", exit_code=0,
                   usage=dict(RESULT_USAGE, cost_usd=1.0, model="m"), stderr_text="")
    summary = crl.summarize(group_by="purpose")
    assert summary.get("_prefix_runs", 0) >= 1
