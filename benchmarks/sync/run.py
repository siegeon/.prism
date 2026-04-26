"""Micro-benchmark for PRISM SessionStart sync hashing.

Measures the current hook-shaped approach (read and hash every eligible
tracked source file) against a persistent-cache-shaped approach (stat all
eligible files, then hash only files whose metadata changed).

This does not mutate the working tree. It is intended to validate whether
incremental-indexing work should start with a file metadata cache, a Merkle
tree, or neither.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

SOURCE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".cs", ".go", ".rs",
    ".java", ".rb", ".php", ".cpp", ".c", ".h", ".hpp",
    ".md", ".yml", ".yaml", ".toml",
}
SKIP_PARTS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
    ".next", ".nuxt", "target", ".claude",
}
MAX_FILE_BYTES = 300_000


def _git_tracked(root: Path) -> list[str] | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        ).stdout
    except Exception:
        return None
    return [line.strip() for line in out.splitlines() if line.strip()]


def _should_skip(path: Path, root: Path) -> bool:
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        return True
    if any(part in SKIP_PARTS for part in rel_parts):
        return True
    if path.suffix not in SOURCE_EXTS:
        return True
    try:
        size = path.stat().st_size
    except OSError:
        return True
    return size == 0 or size > MAX_FILE_BYTES


def eligible_files(root: Path) -> list[str]:
    """Return hook-compatible eligible source paths relative to ``root``."""
    tracked = _git_tracked(root)
    if tracked is not None:
        candidates = (root / rel for rel in tracked)
    else:
        candidates = root.rglob("*")

    out: list[str] = []
    for path in candidates:
        if not path.is_file() or _should_skip(path, root):
            continue
        out.append(path.relative_to(root).as_posix())
    return sorted(out)


def _hash_text_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def collect_full_hashes(root: Path, files: list[str]) -> dict[str, str]:
    """Current hook behavior: read and hash every eligible file."""
    hashes: dict[str, str] = {}
    for rel in files:
        digest = _hash_text_file(root / rel)
        if digest:
            hashes[rel] = digest
    return hashes


def build_metadata_cache(root: Path, files: list[str]) -> dict[str, dict[str, Any]]:
    """Build the cache a metadata-first sync path would persist."""
    cache: dict[str, dict[str, Any]] = {}
    for rel in files:
        path = root / rel
        try:
            stat = path.stat()
        except OSError:
            continue
        digest = _hash_text_file(path)
        if digest:
            cache[rel] = {
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
                "sha256": digest,
            }
    return cache


def scan_with_metadata_cache(
    root: Path,
    files: list[str],
    cache: dict[str, dict[str, Any]],
) -> dict[str, str]:
    """Stat every file, hash only files whose size/mtime changed."""
    changed: dict[str, str] = {}
    for rel in files:
        path = root / rel
        try:
            stat = path.stat()
        except OSError:
            changed[rel] = "missing"
            continue
        old = cache.get(rel)
        if (
            old is None
            or old.get("mtime_ns") != stat.st_mtime_ns
            or old.get("size") != stat.st_size
        ):
            digest = _hash_text_file(path)
            changed[rel] = digest or "unreadable"
    return changed


def _bench(fn, iterations: int) -> dict[str, Any]:
    samples: list[float] = []
    last: Any = None
    for _ in range(iterations):
        t0 = time.perf_counter()
        last = fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return {
        "median_ms": round(statistics.median(samples), 3),
        "max_ms": round(max(samples), 3),
        "result_count": len(last) if hasattr(last, "__len__") else None,
    }


def run(root: Path, iterations: int) -> dict[str, Any]:
    root = root.resolve()
    files = eligible_files(root)
    cache = build_metadata_cache(root, files)
    total_bytes = sum((root / rel).stat().st_size for rel in files)
    largest = max(files, key=lambda rel: (root / rel).stat().st_size, default="")

    fake_delta_cache = dict(cache)
    if largest:
        old = dict(fake_delta_cache[largest])
        old["mtime_ns"] = int(old["mtime_ns"]) - 1
        fake_delta_cache[largest] = old

    return {
        "root": str(root),
        "eligible_files": len(files),
        "eligible_bytes": total_bytes,
        "largest_file": largest,
        "current_full_hash": _bench(
            lambda: collect_full_hashes(root, files),
            iterations,
        ),
        "metadata_cache_noop": _bench(
            lambda: scan_with_metadata_cache(root, files, cache),
            iterations,
        ),
        "metadata_cache_one_file_delta": _bench(
            lambda: scan_with_metadata_cache(root, files, fake_delta_cache),
            iterations,
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--iterations", type=int, default=7)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = run(args.root, max(1, args.iterations))
    text = json.dumps(result, indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
