"""Test results become linkable entities at gates — red→green pins for task
b8703343-e8e3-4e1b-abdf-b8e19380d885.

  AC-1  the xref `test` rung resolves a pytest node id / test-file token to
        a live /artifact href (file part healed via resolve_indexed_file);
        out-of-repo stays honestly unresolved.
  AC-2  GET /gate/readiness serves receipt-backed `tests` rows — one per
        pytest id in the task's derived OracleSpec, provenance from the
        latest matching EvidenceReceipt; ids without a receipt say so.
  AC-4  no new persistence: the rows are a VIEW — they change when (and
        only when) a new receipt is appended.
"""

from __future__ import annotations


NODE_ID = ("services/prism-service/tests/integration/"
           "test_gate_decide_idempotent_rewind.py::test_rewind_http_lever")
BARE_FILE = "services/prism-service/tests/unit/test_xref_neighbors.py"


class _EchoBrain:
    """resolve_indexed_file echoes path-shaped tokens = 'every project file
    is indexed' (the pattern test_xref_neighbors.py uses); everything else
    unresolved."""

    def find_symbol(self, leaf, limit=10):
        return []

    def resolve_indexed_file(self, token):
        t = str(token or "").replace("\\", "/")
        if t.startswith("C:/outside/"):
            return None
        return t if "/" in t and t.endswith(".py") else None

    def get_recent_searches(self, limit=200):
        return []


class _NoMemory:
    def get_entry(self, token):
        return None

    def list_domains(self):
        return []

    def list_entries(self, _d):
        return []


# ---------------------------------------------------------------------------
# AC-1 — the test rung resolves live
# ---------------------------------------------------------------------------


def test_resolve_test_node_id():
    from prism_service.api.xref import resolve_token
    res = resolve_token(NODE_ID, _NoMemory(), _EchoBrain())
    assert res["kind"] == "test", res
    assert res["href"].startswith("/artifact?focus=")
    assert "test_gate_decide_idempotent_rewind.py" in res["href"]
    assert "symbol=test_rewind_http_lever" in res["href"]


def test_resolve_test_bare_file():
    from prism_service.api.xref import resolve_token
    res = resolve_token(BARE_FILE, _NoMemory(), _EchoBrain())
    # a bare test-file path resolves through the ladder to a LIVE href —
    # the file rung may claim it first (kind 'file'), which is still a live
    # test surface; what must NOT happen is 'unresolved'.
    assert res["kind"] in ("test", "file"), res
    assert res["href"], res


def test_resolve_test_out_of_repo_stays_unresolved():
    from prism_service.api.xref import resolve_token
    res = resolve_token("C:/outside/tests/test_x.py::test_y",
                        _NoMemory(), _EchoBrain())
    assert res["kind"] == "unresolved", res


# ---------------------------------------------------------------------------
# AC-2 / AC-4 — readiness serves receipt-backed rows (a pure view)
# ---------------------------------------------------------------------------


def _world(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import conductor as conductor_api
    from prism_service.services.conductor_service import ConductorService
    from prism_service.services.task_service import TaskService

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path / "data"))
    task_svc = TaskService(str(tmp_path / "tasks.db"))
    cond = ConductorService(str(tmp_path / "scores.db"), enable_engine=False,
                            task_svc=task_svc)
    cond._project_name = "testproj"
    monkeypatch.setattr(conductor_api, "_svc", lambda project: cond)
    app = FastAPI()
    app.include_router(conductor_api.router, prefix="/api/conductor")
    return TestClient(app), task_svc


def test_readiness_lists_ids_without_receipt_as_unevidenced(tmp_path,
                                                            monkeypatch):
    client, task_svc = _world(tmp_path, monkeypatch)
    t = task_svc.create(title="rows without receipt", oracle="tests green",
                        proof_type="test")
    task_svc.update(t.id, verify=[f"pytest {NODE_ID.split('::')[0]} -q"])

    r = client.get(f"/api/conductor/gate/readiness?task_id={t.id}"
                   f"&project=testproj")
    assert r.status_code == 200, r.text
    rows = r.json().get("tests")
    assert rows, r.json()
    assert rows[0]["passed"] is None  # listed, honestly unevidenced
    assert rows[0]["href"], rows[0]


def test_readiness_rows_carry_receipt_provenance(tmp_path, monkeypatch):
    from prism_service.services import oracle_spec as osp
    client, task_svc = _world(tmp_path, monkeypatch)
    t = task_svc.create(title="rows with receipt", oracle="tests green",
                        proof_type="test")
    task_svc.update(t.id, verify=[f"pytest {NODE_ID.split('::')[0]} -q"])
    task = task_svc.get(t.id)

    spec = osp.OracleSpec.from_task(task)
    assert spec.adapter == osp.ADAPTER_PYTEST
    osp.append_receipt("testproj", osp.EvidenceReceipt(
        task_id=t.id, job_id="prov-1", spec_hash=spec.spec_hash(),
        tree_sha="tree-1", adapter=spec.adapter, passed=True,
        status="passed", ended_at="2026-07-15T00:00:00+00:00"))

    r = client.get(f"/api/conductor/gate/readiness?task_id={t.id}"
                   f"&project=testproj")
    rows = r.json().get("tests")
    assert rows and rows[0]["passed"] is True, r.json()
    assert rows[0]["receipt_job_id"] == "prov-1"
    assert rows[0]["ended_at"] == "2026-07-15T00:00:00+00:00"

    # AC-4: the rows are a VIEW over receipts — a NEW failing receipt flips
    # them with no other persistence involved.
    osp.append_receipt("testproj", osp.EvidenceReceipt(
        task_id=t.id, job_id="prov-2", spec_hash=spec.spec_hash(),
        tree_sha="tree-1", adapter=spec.adapter, passed=False,
        status="failed", ended_at="2026-07-15T01:00:00+00:00"))
    r2 = client.get(f"/api/conductor/gate/readiness?task_id={t.id}"
                    f"&project=testproj")
    rows2 = r2.json().get("tests")
    assert rows2[0]["passed"] is False
    assert rows2[0]["receipt_job_id"] == "prov-2"
