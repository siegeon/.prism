"""Where a new artifact goes (task 1c122936-a36c-40e5-9e2d-d67b696b3003).

Pins documents.place(), ported from the prototype (proto/.claude/skills/
ontology/SKILL.md "Where a new artifact goes"): a folder that already
exists always wins; matching is on a whole hyphen-separated token, never a
substring; only when nothing in the tree holds the work does the grammar
build a path.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

# A small realistic tree: existing folders to match against, plus the
# substring-negative traps ("product" for "pro", "platform-economics" for
# "ps"), and no "engineering/weekly-reports/2026-08-25" yet.
FIXTURE_PATHS = [
    "engineering/release-stability/notes.md",
    "engineering/release-stability/incident-2026-08-01.md",
    "engineering/weekly-reports/2026-08-11/report.md",
    "engineering/weekly-reports/2026-08-18/report.md",
    "engineering/product/roadmap.md",
    "growth/platform-economics/model.md",
    "support/1-1-chris-wiggins/notes.md",
    "support/1-1-chris-wiggins/2026-07-01.md",
]


# ---------------------------------------------------------------------------
# services/document_tree.place() -- the three worked examples
# ---------------------------------------------------------------------------

def test_existing_folder_wins_for_release_stability():
    from prism_service.services.document_tree import place

    result = place(FIXTURE_PATHS, about="release-stability")
    assert result["path"] == "engineering/release-stability"
    assert result["reason"] == "already holds this work"


def test_named_piece_matches_on_whole_token_in_its_folder():
    from prism_service.services.document_tree import place

    result = place(FIXTURE_PATHS, about="chris", area="support")
    assert result["path"] == "support/1-1-chris-wiggins"
    assert result["reason"] == "matched on the name in its folder"


def test_grammar_composes_area_series_date_when_absent():
    from prism_service.services.document_tree import place

    result = place(
        FIXTURE_PATHS, area="engineering", kind_of="weekly-reports", date="2026-08-25"
    )
    assert result["path"] == "engineering/weekly-reports/2026-08-25"
    assert result["reason"] == "nothing in the tree holds this yet; built from the grammar"


# ---------------------------------------------------------------------------
# Substring negatives: matching is on a WHOLE hyphen-separated token
# ---------------------------------------------------------------------------

def test_about_pro_does_not_match_product():
    from prism_service.services.document_tree import place

    result = place(FIXTURE_PATHS, about="pro")
    assert result["path"] != "engineering/product"
    assert "product" not in result["path"]


def test_about_ps_does_not_match_platform_economics():
    from prism_service.services.document_tree import place

    result = place(FIXTURE_PATHS, about="ps")
    assert result["path"] != "growth/platform-economics"
    assert "platform-economics" not in result["path"]


# ---------------------------------------------------------------------------
# POST /api/documents/place -- reads the same brain docs source as GET
# ---------------------------------------------------------------------------

def _seed_docs_db(db_path: Path, source_files: list[str]) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE docs (id TEXT PRIMARY KEY, source_file TEXT, content TEXT, domain TEXT)"
    )
    for i, sf in enumerate(source_files):
        conn.execute(
            "INSERT INTO docs (id, source_file, content, domain) VALUES (?, ?, ?, ?)",
            (f"doc-{i}", sf, "x", "code"),
        )
    conn.commit()
    conn.close()


def _client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from prism_service.api import documents as documents_api

    db_path = tmp_path / "brain.db"
    _seed_docs_db(db_path, FIXTURE_PATHS)

    class _BrainSvc:
        _brain_db = str(db_path)

    class _Ctx:
        brain_svc = _BrainSvc()

    monkeypatch.setattr(documents_api, "get_project", lambda p: _Ctx())
    app = FastAPI()
    app.include_router(documents_api.router, prefix="/api/documents")
    return TestClient(app)


def test_post_place_route_registered_on_api_router():
    from fastapi import FastAPI
    from prism_service.api import api_router

    app = FastAPI()
    app.include_router(api_router)
    paths = set(app.openapi()["paths"].keys())
    assert "/api/documents/place" in paths, f"POST /api/documents/place not mounted: {sorted(paths)}"


def test_post_place_returns_existing_folder(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post(
        "/api/documents/place",
        params={"project": "prism"},
        json={"about": "chris", "area": "support"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "support/1-1-chris-wiggins"
    assert body["reason"] == "matched on the name in its folder"


def test_post_place_builds_from_grammar_when_absent(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.post(
        "/api/documents/place",
        params={"project": "prism"},
        json={"area": "engineering", "kind_of": "weekly-reports", "date": "2026-08-25"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "engineering/weekly-reports/2026-08-25"
    assert body["reason"] == "nothing in the tree holds this yet; built from the grammar"


# ---------------------------------------------------------------------------
# MCP tool registration
# ---------------------------------------------------------------------------

def test_documents_place_tool_is_registered():
    from prism_service.mcp.tools import TOOLS

    names = {t.name for t in TOOLS}
    assert "documents_place" in names


def test_documents_place_tool_is_reachable_via_the_all_profile():
    # Not added to the curated INTERACTIVE_TOOL_NAMES set here: that would
    # also require updating test_mcp_tool_profiles.py's exact-count pin,
    # which is outside this task's allowed_files. Registered in TOOLS, so
    # it is reachable via tool_profile=all.
    from prism_service.mcp.tools import tools_for_profile

    names = {t.name for t in tools_for_profile("all")}
    assert "documents_place" in names
