"""Regression coverage for the source-run frontend rebuild contract.

The Aspire watcher serves ``web_dist`` while Vite rebuilds it.  Emptying that
directory at the beginning of a watched build makes the SPA fallback return
500 until the new ``index.html`` is emitted.
"""

from pathlib import Path


_VITE_CONFIG = (
    Path(__file__).resolve().parents[2]
    / "prism_service"
    / "web"
    / "vite.config.ts"
)


def test_watch_build_preserves_last_complete_bundle() -> None:
    source = _VITE_CONFIG.read_text(encoding="utf-8")

    assert 'process.env.PRISM_WATCH_BUILD === "1"' in source
    assert "outDir: WATCH_BUILD ? WATCH_STAGE : WEB_DIST" in source
    assert 'name: "prism-atomic-watch-publisher"' in source
    assert "fs.cpSync" in source
    assert "fs.copyFileSync" in source
    assert "fs.renameSync(nextIndex" in source
    assert source.index("fs.cpSync") < source.index("fs.renameSync(nextIndex")
    assert "preview:" in source
    assert '"/api": { target: BACKEND' in source


def test_spa_index_keeps_last_complete_document_during_replacement(tmp_path, monkeypatch) -> None:
    from prism_service import main

    index = tmp_path / "index.html"
    index.write_text("<html>known good</html>", encoding="utf-8")
    monkeypatch.setattr(main, "WEB_DIST", tmp_path)
    monkeypatch.setattr(main, "_SPA_INDEX_CACHE", None)

    first = main._spa_index_response()
    index.unlink()  # exact watched-build replacement window
    during_rebuild = main._spa_index_response()

    assert first.status_code == 200
    assert during_rebuild.status_code == 200
    assert during_rebuild.body == b"<html>known good</html>"
