"""Files live under the Learning loop (task 5bfdf527-32ef-45b2-b9f4-feae8ffe5c65).

Brings the prototype's Files surface (documents.py: the file tree IS part
of the model) into PRISM: a Files nav item under "Learning loop"
(Sidebar.tsx), a /files route (App.tsx) rendering FilesPage.tsx, backed by
a read-only GET /api/documents that classifies the brain's indexed doc
paths against the ontology grammar (proto/.claude/skills/ontology/SKILL.md
"Where a new artifact goes" + "The file tree is part of the model"):
<area>/, <area>/<series>/, <area>/<name>/, <area>/<date>/ (YYYY-MM-DD),
nesting composes.

The SPA has NO JS test runner, so the nav/route ACs are pinned by reading
the actual TSX source (same pattern as test_tasks_page_unified_queue.py).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

_SRC = _SERVICE_ROOT / "prism_service" / "web" / "src"
_SIDEBAR = _SRC / "components" / "Sidebar.tsx"
_APP = _SRC / "App.tsx"
_FILES_PAGE = _SRC / "pages" / "FilesPage.tsx"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _learning_loop_section(src: str) -> str:
    """The MAIN_SECTIONS object literal for label: "Learning loop" — never
    a character window, so a comment mentioning "Learning loop" elsewhere
    can't satisfy this."""
    marker = 'label: "Learning loop"'
    i = src.index(marker)
    # Walk back to this section's opening brace, forward to its closing one.
    start = src.rindex("{", 0, i)
    depth = 0
    end = start
    for j in range(start, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                end = j
                break
    return src[start:end + 1]


def test_sidebar_files_item_is_in_the_learning_loop_section():
    section = _learning_loop_section(_read(_SIDEBAR))
    assert '{ to: "/files", label: "Files", icon: FolderTree }' in section
    # Last item of the section (after Sessions/Consolidation/Learning).
    assert section.index('label: "Learning"') < section.index('label: "Files"')


def test_files_item_is_not_under_knowledge():
    src = _read(_SIDEBAR)
    knowledge_marker = 'label: "Knowledge"'
    ki = src.index(knowledge_marker)
    kstart = src.rindex("{", 0, ki)
    depth = 0
    kend = kstart
    for j in range(kstart, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                kend = j
                break
    assert '"/files"' not in src[kstart:kend + 1]


def test_app_routes_files_to_files_page():
    app = _read(_APP)
    assert 'lazyRoute("files", () => import("@/pages/FilesPage"))' in app
    assert '<Route path="/files" element={<FilesPage />} />' in app


def test_files_page_uses_prism_kind_label_style():
    page = _read(_FILES_PAGE)
    assert "text-transform: uppercase" in page
    assert "letter-spacing: .1em" in page
    assert "var(--text-muted)" in page


# ---------------------------------------------------------------------------
# services/document_tree.py — pure grammar classifier
# ---------------------------------------------------------------------------

def test_classify_area_only():
    from prism_service.services.document_tree import classify

    tree = classify(["security/notes.md"])
    assert tree["folders"] == [
        {"path": "security", "kind": "area", "doc_count": 1, "children": []},
    ]
    assert tree["loose_in_root"] == []
    assert tree["date_format_breaks"] == []


def test_classify_area_date():
    from prism_service.services.document_tree import classify

    tree = classify(["ops/2026-08-01/notes.md"])
    ops = tree["folders"][0]
    assert ops["kind"] == "area"
    assert ops["children"][0] == {
        "path": "ops/2026-08-01", "kind": "date", "doc_count": 1, "children": [],
    }


def test_classify_area_name_when_folder_never_recurs():
    from prism_service.services.document_tree import classify

    tree = classify(["support/1-1-chris-wiggins/notes.md"])
    name_folder = tree["folders"][0]["children"][0]
    assert name_folder["path"] == "support/1-1-chris-wiggins"
    assert name_folder["kind"] == "name"


def test_classify_series_when_folder_name_recurs_across_areas():
    from prism_service.services.document_tree import classify

    tree = classify([
        "engineering/weekly-reports/2026-08-11/report.md",
        "product/weekly-reports/2026-08-04/report.md",
    ])
    by_path = {f["path"]: f for area in tree["folders"] for f in area["children"]}
    assert by_path["engineering/weekly-reports"]["kind"] == "series"
    assert by_path["product/weekly-reports"]["kind"] == "series"


def test_classify_composed_nesting_area_series_name_date():
    from prism_service.services.document_tree import classify

    tree = classify([
        "engineering/weekly-reports/saturation/2026-08-18/report.md",
        "product/weekly-reports/2026-08-04/report.md",  # makes weekly-reports recur
    ])
    eng = next(f for f in tree["folders"] if f["path"] == "engineering")
    series = next(c for c in eng["children"] if c["path"] == "engineering/weekly-reports")
    assert series["kind"] == "series"
    name = next(c for c in series["children"] if c["path"] == "engineering/weekly-reports/saturation")
    assert name["kind"] == "name"
    date = next(c for c in name["children"] if c["path"] == "engineering/weekly-reports/saturation/2026-08-18")
    assert date["kind"] == "date"
    assert date["doc_count"] == 1


def test_classify_flags_dated_folder_that_breaks_one_format_rule():
    from prism_service.services.document_tree import classify

    tree = classify([
        "engineering/weekly-reports/saturation/2026-08-18/report.md",
        "engineering/weekly-reports/saturation/2026-Q1/report.md",
    ])
    assert "engineering/weekly-reports/saturation/2026-Q1" in tree["date_format_breaks"]
    assert "engineering/weekly-reports/saturation/2026-08-18" not in tree["date_format_breaks"]


def test_classify_flags_document_loose_in_root():
    from prism_service.services.document_tree import classify

    tree = classify(["README.md", "engineering/notes.md"])
    assert tree["loose_in_root"] == ["README.md"]


# ---------------------------------------------------------------------------
# GET /api/documents — read-through over a seeded docs table
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
    _seed_docs_db(db_path, [
        "security/notes.md",
        "engineering/weekly-reports/2026-08-11/report.md",
        "product/weekly-reports/2026-08-04/report.md",
        "README.md",
    ])

    class _BrainSvc:
        _brain_db = str(db_path)

    class _Ctx:
        brain_svc = _BrainSvc()

    monkeypatch.setattr(documents_api, "get_project", lambda p: _Ctx())
    app = FastAPI()
    app.include_router(documents_api.router, prefix="/api/documents")
    return TestClient(app)


def test_route_registered_on_api_router():
    from fastapi import FastAPI
    from prism_service.api import api_router

    app = FastAPI()
    app.include_router(api_router)
    paths = set(app.openapi()["paths"].keys())
    assert "/api/documents" in paths, f"GET /api/documents not mounted: {sorted(paths)}"


def test_get_documents_classifies_seeded_docs_table(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.get("/api/documents", params={"project": "prism"})
    assert r.status_code == 200
    body = r.json()
    assert "README.md" in body["loose_in_root"]
    paths = {f["path"] for f in body["folders"]}
    assert "security" in paths
    assert "engineering" in paths
    assert "product" in paths
