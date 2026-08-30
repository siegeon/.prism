"""A finished run recorded under the OLD terminal id still reads as won.

The terminal node was recorded as "shipped" while the canvas draws "land",
so a task that walked every node and passed came back finished=False and
the win state painted nothing. Renaming the constant fixed new recordings;
this pins that the STORED ones are carried across too.
"""
import sqlite3

from prism_service.services import flow_run_recorder as rec


def _seed(path: str, terminal: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute(rec._SCHEMA)
    conn.execute(
        "INSERT INTO flow_node_runs (run_id, task_id, workflow_id, node_id, "
        "outcome, flow_version, recorded_at) VALUES (?,?,?,?,?,?,?)",
        ("r1", "t1", "conductor", terminal, "pass", 1, "2026-08-30"))
    conn.commit()
    conn.close()


def test_a_walk_ending_at_the_old_terminal_id_reads_as_finished(tmp_path):
    db = str(tmp_path / "scores.db")
    _seed(db, "shipped")

    runs = rec.runs_for_task(db, "t1", "conductor")

    assert [r["node_id"] for r in runs] == [rec.SHIPPED_NODE]
    assert rec.is_finished(runs) is True


def test_a_walk_ending_mid_pipeline_is_not_finished(tmp_path):
    db = str(tmp_path / "scores.db")
    _seed(db, "implement_tasks")

    runs = rec.runs_for_task(db, "t1", "conductor")

    assert rec.is_finished(runs) is False
