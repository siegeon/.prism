"""Pure grammar classifier for the file tree ontology.

Grammar read off proto/.claude/skills/ontology/SKILL.md ("Where a new
artifact goes" + "The file tree is part of the model"):

    <area>/            where a body of work is kept
    <area>/<series>/    a recurring series (e.g. weekly-reports)
    <area>/<name>/      a named piece of work
    <area>/<date>/      a dated instance, YYYY-MM-DD

Nesting composes (engineering/weekly-reports/saturation/2026-08-18 is an
area, a series, a name and a date). A folder recurring under two or more
distinct parents anywhere in the tree is read as a "series" (the same
signal the SKILL cites: weekly-reports recurs in 7 of 14 areas); a folder
that never recurs is a one-off "name". No I/O — callers hand in plain
path strings pulled from wherever documents are indexed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# YYYY-MM-DD exactly.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Looks dated but isn't YYYY-MM-DD — e.g. "2026-Q1", "2026-08" — the
# `dated-folder-uses-one-format` break the SKILL calls out.
_DATE_BREAK_RE = re.compile(r"^\d{4}-(Q[1-4]|\d{1,2})$")


@dataclass
class _Node:
    name: str
    path: str
    children: dict[str, "_Node"] = field(default_factory=dict)
    doc_count: int = 0


def _split(path: str) -> list[str]:
    return [p for p in path.strip("/").split("/") if p]


def classify(paths: list[str]) -> dict:
    """Classify a flat list of document paths into the ontology grammar.

    Returns {"folders": [...], "loose_in_root": [...], "date_format_breaks": [...]}.
    Each folder node is {"path", "kind", "doc_count", "children"}.
    """
    root = _Node(name="", path="")
    loose_in_root: list[str] = []

    for p in paths:
        parts = _split(p)
        if not parts:
            continue
        if len(parts) == 1:
            loose_in_root.append(p)
            continue
        folder_parts = parts[:-1]
        node = root
        for i, part in enumerate(folder_parts):
            child_path = "/".join(folder_parts[: i + 1])
            if part not in node.children:
                node.children[part] = _Node(name=part, path=child_path)
            node = node.children[part]
        node.doc_count += 1

    # How many distinct parents each folder name appears under, anywhere in
    # the tree — a name that recurs (weekly-reports) is a series.
    name_occurrences: dict[str, int] = {}

    def _count_names(node: _Node) -> None:
        for child in node.children.values():
            name_occurrences[child.name] = name_occurrences.get(child.name, 0) + 1
            _count_names(child)

    _count_names(root)

    date_format_breaks: list[str] = []

    def _kind(node: _Node, depth: int) -> str:
        if depth == 1:
            return "area"
        if _DATE_RE.match(node.name):
            return "date"
        if _DATE_BREAK_RE.match(node.name):
            date_format_breaks.append(node.path)
            return "date"
        if name_occurrences.get(node.name, 0) > 1:
            return "series"
        return "name"

    def _rollup(node: _Node) -> int:
        total = node.doc_count
        for c in node.children.values():
            total += _rollup(c)
        node.doc_count = total
        return total

    _rollup(root)

    def _build(node: _Node, depth: int) -> list[dict]:
        out = []
        for child in sorted(node.children.values(), key=lambda n: n.name):
            out.append({
                "path": child.path,
                "kind": _kind(child, depth),
                "doc_count": child.doc_count,
                "children": _build(child, depth + 1),
            })
        return out

    folders = _build(root, 1)
    return {
        "folders": folders,
        "loose_in_root": sorted(loose_in_root),
        "date_format_breaks": sorted(date_format_breaks),
    }
