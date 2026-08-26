"""Memory text is normalised to STE when stored (task 5de57583).

MemoryService.store runs the description through the deterministic STE
normaliser (services/ste.py, "flavored" mode — the same mode task
title/description use) before persisting it, and records a style block
on ``svc.last_style`` describing what changed. Every path that writes a
memory through MemoryService.store — the service itself, the MCP
``memory_store`` tool, and the API's supersede route — must see the same
normalised text.

``name`` is a kebab-case id and is left untouched by the normaliser.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services import ste
from prism_service.services.memory_service import MemoryService

# A description carrying a file path (protected span), a semicolon that
# sits OUTSIDE any protected span (so it becomes a sentence break), and a
# contraction. This is the shared fixture text for the service/MCP/API
# tests below.
_DESC = (
    "Routes defined in Features/*/Endpoints.cs. Each delegates to a "
    "Handler.cs; don't use controllers."
)
_DESC_NORMALISED = ste.normalize(_DESC, "flavored")[0]


# ----------------------------------------------------------------------
# 1. Service-level: MemoryService.store normalises description in place.
# ----------------------------------------------------------------------


def test_service_store_normalises_description_to_ste(tmp_path):
    svc = MemoryService(mulch_dir=str(tmp_path / "mulch"))

    entry = svc.store(
        domain="architecture",
        name="routes-in-endpoints-cs",
        description=_DESC,
        type="convention",
        classification="tactical",
    )

    # The stored description is byte-identical to what ste.normalize
    # produces directly on the same raw text.
    assert entry.description == _DESC_NORMALISED
    assert entry.description == ste.normalize(_DESC, "flavored")[0]

    # The semicolon after "Handler.cs" (not glued to a protected file
    # path token) became a sentence break.
    assert "Handler.cs. Do not use controllers." in entry.description
    assert ";" not in entry.description

    # The file path is preserved byte-identical inside the normalised text.
    assert "Features/*/Endpoints.cs" in entry.description

    # name is a kebab-case id and stays untouched.
    assert entry.name == "routes-in-endpoints-cs"

    # svc.last_style reports the rules that fired on the description.
    assert "semicolon" in svc.last_style["fixed"]["description"]
    assert "contraction" in svc.last_style["fixed"]["description"]


def test_service_store_leaves_hedge_sentence_unchanged_no_fixed_rules(tmp_path):
    """A sentence with no safe-fix pattern is stored unchanged, and
    svc.last_style reports no "fixed" rules for the description field."""
    svc = MemoryService(mulch_dir=str(tmp_path / "mulch"))
    text = "This may have failed because the cache was cold."

    entry = svc.store(
        domain="feedback",
        name="cold-cache-hedge",
        description=text,
        type="failure",
        classification="tactical",
    )

    assert entry.description == text
    assert "description" not in svc.last_style.get("fixed", {})


def test_service_store_keeps_fenced_code_block_byte_identical(tmp_path):
    """A fenced code block's content is copied through unchanged, even
    though it contains a semicolon and a contraction the normaliser
    would otherwise rewrite outside the fence."""
    svc = MemoryService(mulch_dir=str(tmp_path / "mulch"))
    text = (
        "See the fix:\n\n"
        "```python\n"
        "if not ok:\n"
        "    raise ValueError(\"don't; retry\")\n"
        "```\n\n"
        "Don't skip this."
    )

    entry = svc.store(
        domain="conventions",
        name="fenced-code-block-protected",
        description=text,
        type="pattern",
        classification="tactical",
    )

    assert "raise ValueError(\"don't; retry\")" in entry.description
    # Outside the fence, the contraction IS rewritten.
    assert "Do not skip this." in entry.description


# ----------------------------------------------------------------------
# 2. MCP memory_store tool — reachable through the real dispatcher.
# ----------------------------------------------------------------------

_PID = "test-memory-ste"


def _isolated_project(tmp_path, pid=_PID):
    from prism_service import config as cfg
    original = cfg.PROJECTS_DIR
    cfg.PROJECTS_DIR = tmp_path / "projects"
    cfg.PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
    from prism_service import project_context as pc
    pc._contexts.clear()
    yield pid
    cfg.PROJECTS_DIR = original
    pc._contexts.clear()


@pytest.fixture
def project(tmp_path):
    yield from _isolated_project(tmp_path)


def _call(tool_name, arguments=None, project_id=_PID):
    from prism_service.mcp.tools import handle_tool
    return asyncio.run(
        handle_tool(tool_name, arguments or {}, project_id=project_id)
    )


def _text(result):
    assert len(result) == 1
    return result[0].text


def test_mcp_memory_store_normalises_and_returns_style(project):
    res = json.loads(_text(_call("memory_store", {
        "domain": "architecture",
        "name": "routes-in-endpoints-cs",
        "description": _DESC,
        "type": "convention",
        "classification": "tactical",
    }, project_id=project)))

    assert res["description"] == _DESC_NORMALISED
    assert "style" in res
    assert "semicolon" in res["style"]["fixed"]["description"]
    assert res["name"] == "routes-in-endpoints-cs"


# ----------------------------------------------------------------------
# 3. API route — the supersede action on POST /entry/{id}/action, the
#    seam that stores a NEW memory through MemoryService.store from the
#    web UI / HTTP callers (memory.py has no separate "create" route;
#    "supersede" is the one that calls svc.store()).
# ----------------------------------------------------------------------


def _api_client(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from prism_service.api import memory as memory_api

    svc = MemoryService(mulch_dir=str(tmp_path / "mulch"))

    class _Ctx:
        memory_svc = svc

    monkeypatch.setattr(memory_api, "get_project", lambda p: _Ctx())

    app = FastAPI()
    app.include_router(memory_api.router, prefix="/api/memory")
    return TestClient(app), svc


def test_api_supersede_route_normalises_and_returns_style(tmp_path, monkeypatch):
    client, svc = _api_client(tmp_path, monkeypatch)

    # Seed the entry that will be superseded.
    seed = svc.store(
        domain="architecture",
        name="routes-in-endpoints-cs",
        description="placeholder",
        type="convention",
        classification="tactical",
    )

    resp = client.post(
        f"/api/memory/entry/{seed.id}/action",
        params={"project": "prism"},
        json={"action": "supersede", "description": _DESC},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["entry"]["description"] == _DESC_NORMALISED
    assert "style" in body
    assert "semicolon" in body["style"]["fixed"]["description"]
