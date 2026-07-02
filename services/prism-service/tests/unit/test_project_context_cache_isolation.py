"""ProjectContext cache must key by data dir, not project name (33a1397b).

The process-global ``_contexts`` cache in project_context.py keyed on the
project NAME alone. Under a changed ``PRISM_DATA_DIR`` (every test that
monkeypatches a tmp data dir), ``get_project(name)`` returned the ALREADY
cached context bound to the FIRST data dir, so writes leaked into a shared
store and later tests' listing assertions saw phantom rows.

Contract: two ``get_project`` calls for the SAME project id under DIFFERENT
data dirs must return DISTINCT contexts bound to DISTINCT stores.

  * AC-1 distinct context objects  — oracle: ctx_a is not ctx_b
  * AC-2 distinct on-disk stores   — oracle: ctx_a._data_dir != ctx_b._data_dir
"""

from __future__ import annotations


def _point_at(monkeypatch, data_dir):
    """Route BOTH the env (the cache key source) and config's frozen
    globals (the seeding/existence source) at *data_dir*, the way a real
    isolated test must so a PRISM_DATA_DIR change actually takes effect."""
    import prism_service.config as cfg
    monkeypatch.setenv("PRISM_DATA_DIR", str(data_dir))
    monkeypatch.setattr(cfg, "DATA_DIR", data_dir)
    monkeypatch.setattr(cfg, "PROJECTS_DIR", data_dir / "projects")


def test_get_project_cache_keys_by_data_dir(tmp_path, monkeypatch):
    from prism_service import project_context as pc
    from prism_service.config import project_data_dir

    dir_a = tmp_path / "store_a"
    dir_b = tmp_path / "store_b"

    # First data dir: seed the project then resolve its context.
    _point_at(monkeypatch, dir_a)
    project_data_dir("isoproj")          # existence door under dir_a
    ctx_a = pc.get_project("isoproj")

    # Switch data dir (as a per-test monkeypatch does) and resolve again.
    _point_at(monkeypatch, dir_b)
    project_data_dir("isoproj")          # existence door under dir_b
    ctx_b = pc.get_project("isoproj")

    # AC-1: a data-dir change must yield a FRESH context. Pre-fix the
    # name-only cache handed back the dir_a context here.
    assert ctx_a is not ctx_b, (
        "get_project returned the cached dir_a context under a new data dir "
        "- cache must key by (data_dir, project_id)"
    )
    # AC-2: the two contexts must be bound to different on-disk stores.
    assert ctx_a._data_dir != ctx_b._data_dir
    assert str(dir_a) in str(ctx_a._data_dir)
    assert str(dir_b) in str(ctx_b._data_dir)
