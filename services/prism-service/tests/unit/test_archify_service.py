"""Tests for ArchifyService."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

# Add service root to path for imports
_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


def test_service_init(tmp_path, monkeypatch):
    """Test ArchifyService initialization."""
    if shutil.which("node") is None:
        return  # Skip if node not available

    from prism_service import config
    from prism_service.services.archify_service import ArchifyService

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    # Force config re-import to pick up new env var
    import importlib
    importlib.reload(config)

    svc = ArchifyService("default")
    assert svc.project == "default"
    assert svc.root == config.project_data_dir("default") / "archify"
    assert svc.root.exists()


def test_map_dir_kind(tmp_path, monkeypatch):
    """Test map_dir for code/concepts/language kinds."""
    if shutil.which("node") is None:
        return

    from prism_service import config
    from prism_service.services.archify_service import ArchifyService

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    import importlib
    importlib.reload(config)

    svc = ArchifyService("default")

    code_dir = svc.map_dir("code")
    assert code_dir == svc.root / "code"
    assert code_dir.exists()

    concepts_dir = svc.map_dir("concepts")
    assert concepts_dir == svc.root / "concepts"


def test_map_dir_task(tmp_path, monkeypatch):
    """Test map_dir for task kind requires task_id."""
    if shutil.which("node") is None:
        return

    from prism_service import config
    from prism_service.services.archify_service import ArchifyService

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    import importlib
    importlib.reload(config)

    svc = ArchifyService("default")

    # Should raise if task_id is None
    try:
        svc.map_dir("task", task_id=None)
        assert False, "should raise"
    except ValueError:
        pass

    task_dir = svc.map_dir("task", task_id="abc123")
    assert task_dir == svc.root / "task" / "abc123"
    assert task_dir.exists()


def test_meta_returns_none_when_not_found(tmp_path, monkeypatch):
    """Test meta() returns None for missing map."""
    if shutil.which("node") is None:
        return

    from prism_service import config
    from prism_service.services.archify_service import ArchifyService

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    import importlib
    importlib.reload(config)

    svc = ArchifyService("default")
    meta = svc.meta("code")
    assert meta is None


def test_html_returns_none_when_not_found(tmp_path, monkeypatch):
    """Test html() returns None for missing map."""
    if shutil.which("node") is None:
        return

    from prism_service import config
    from prism_service.services.archify_service import ArchifyService

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    import importlib
    importlib.reload(config)

    svc = ArchifyService("default")
    html = svc.html("code")
    assert html is None


def test_ir_returns_none_when_not_found(tmp_path, monkeypatch):
    """Test ir() returns None for missing map."""
    if shutil.which("node") is None:
        return

    from prism_service import config
    from prism_service.services.archify_service import ArchifyService

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    import importlib
    importlib.reload(config)

    svc = ArchifyService("default")
    ir = svc.ir("code")
    assert ir is None


def test_receipt_returns_none_when_not_found(tmp_path, monkeypatch):
    """Test receipt() returns None for missing map."""
    if shutil.which("node") is None:
        return

    from prism_service import config
    from prism_service.services.archify_service import ArchifyService

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    import importlib
    importlib.reload(config)

    svc = ArchifyService("default")
    receipt = svc.receipt("code")
    assert receipt is None


def test_list_maps_empty(tmp_path, monkeypatch):
    """Test list_maps returns empty list when no maps exist."""
    if shutil.which("node") is None:
        return

    from prism_service import config
    from prism_service.services.archify_service import ArchifyService

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    import importlib
    importlib.reload(config)

    svc = ArchifyService("default")
    maps = svc.list_maps()
    assert maps == []


def test_list_maps_includes_stored_meta(tmp_path, monkeypatch):
    """Test list_maps includes meta.json files."""
    if shutil.which("node") is None:
        return

    from prism_service import config
    from prism_service.services.archify_service import ArchifyService

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    import importlib
    importlib.reload(config)

    svc = ArchifyService("default")

    # Write a meta file for "code" kind
    code_dir = svc.map_dir("code")
    meta = {
        "kind": "code",
        "ok": True,
        "components": 5,
        "connections": 3
    }
    (code_dir / "meta.json").write_text(json.dumps(meta))

    maps = svc.list_maps()
    assert len(maps) == 1
    assert maps[0]["kind"] == "code"


def test_doctor_succeeds(tmp_path, monkeypatch):
    """Test doctor() returns a dict with ok/node/output."""
    if shutil.which("node") is None:
        return

    from prism_service import config
    from prism_service.services.archify_service import ArchifyService

    monkeypatch.setenv("PRISM_DATA_DIR", str(tmp_path))
    import importlib
    importlib.reload(config)

    svc = ArchifyService("default")
    result = svc.doctor()

    assert isinstance(result, dict)
    assert "ok" in result
    assert "node" in result
    assert "output" in result
