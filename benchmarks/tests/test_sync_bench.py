from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_sync_run():
    path = Path(__file__).resolve().parent.parent / "sync" / "run.py"
    spec = importlib.util.spec_from_file_location("sync_run", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_metadata_cache_noop_reports_no_changes(tmp_path):
    mod = _load_sync_run()
    sample = tmp_path / "sample.py"
    sample.write_text("print('hello')\n", encoding="utf-8")

    files = mod.eligible_files(tmp_path)
    cache = mod.build_metadata_cache(tmp_path, files)

    assert files == ["sample.py"]
    assert mod.scan_with_metadata_cache(tmp_path, files, cache) == {}


def test_metadata_cache_hashes_changed_file(tmp_path):
    mod = _load_sync_run()
    sample = tmp_path / "sample.py"
    sample.write_text("print('hello')\n", encoding="utf-8")

    files = mod.eligible_files(tmp_path)
    cache = mod.build_metadata_cache(tmp_path, files)
    stale = {key: dict(value) for key, value in cache.items()}
    stale["sample.py"]["size"] -= 1

    changed = mod.scan_with_metadata_cache(tmp_path, files, stale)

    assert list(changed) == ["sample.py"]
    assert changed["sample.py"] == cache["sample.py"]["sha256"]
