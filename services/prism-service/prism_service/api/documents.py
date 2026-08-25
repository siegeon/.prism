"""Documents API -- the file tree, classified against the ontology grammar.

GET /api/documents derives folders/documents from the brain's indexed docs
table (source_file paths already collected by BrainService/Brain -- see
engines/brain_engine.py's `docs` table). Read-only: no new store, no
writes -- a fresh sqlite connection onto the SAME brain.db BrainService
already maintains, then handed to services/document_tree.classify(), which
is pure and does all the grammar work.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from prism_service.project_context import get_project
from prism_service.services.document_tree import classify, place
from prism_service.services import sqlite_db

router = APIRouter()


def list_source_files(project: str) -> list[str]:
    try:
        proj = get_project(project)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"unknown project: {project}: {exc}")
    brain_db = getattr(proj.brain_svc, "_brain_db", None)
    if not brain_db or not Path(brain_db).exists():
        return []
    # Through the sqlite chokepoint (services/sqlite_db) like every other
    # site — the repo's grep-gate forbids bare sqlite3.connect. Plain SELECT
    # only; no writes happen on this handle.
    conn = sqlite_db.connect(brain_db)
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_file FROM docs WHERE source_file IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


@router.get("")
def get_documents(project: str = Query("default")) -> dict:
    return classify(list_source_files(project))


class PlaceRequest(BaseModel):
    about: str | None = None
    area: str | None = None
    kind_of: str | None = None
    date: str | None = None


@router.post("/place")
def post_place(body: PlaceRequest, project: str = Query("default")) -> dict:
    return place(
        list_source_files(project),
        about=body.about,
        area=body.area,
        kind_of=body.kind_of,
        date=body.date,
    )
