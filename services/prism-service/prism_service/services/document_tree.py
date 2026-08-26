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


# ---------------------------------------------------------------------------
# place() -- "Where a new artifact goes" (SKILL.md same section). A folder
# that already exists always wins; matching is on a WHOLE hyphen-separated
# token, never a substring. Only when nothing in the tree holds the work
# does the grammar build <area>/<kind_of>/<date>, <area>/<about>, or
# <area>/<date>.
# ---------------------------------------------------------------------------

def _all_folder_paths(tree_paths: list[str]) -> set[str]:
    """Every folder path at every depth, derived the same way classify()
    builds its tree (all prefixes of each document's containing folders)."""
    folders: set[str] = set()
    for p in tree_paths:
        parts = _split(p)
        folder_parts = parts[:-1]
        for i in range(len(folder_parts)):
            folders.add("/".join(folder_parts[: i + 1]))
    return folders


def _tokens(s: str) -> list[str]:
    return [t.lower() for t in s.strip("/").split("-") if t]


def _basename(path: str) -> str:
    return path.rstrip("/").split("/")[-1]


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    n = len(needle)
    if n == 0 or n > len(haystack):
        return False
    return any(haystack[i:i + n] == needle for i in range(len(haystack) - n + 1))


def _grammar_path(*, about: str | None, area: str | None, kind_of: str | None, date: str | None) -> str:
    parts = []
    if area:
        parts.append(area)
    if kind_of:
        parts.append(kind_of)
    elif about:
        parts.append(about)
    if date:
        parts.append(date)
    return "/".join(parts)


def place(
    tree_paths: list[str],
    *,
    about: str | None = None,
    area: str | None = None,
    kind_of: str | None = None,
    date: str | None = None,
) -> dict:
    """Where a new artifact goes. Returns {"path", "reason"}.

    A folder that already exists always wins. Matching for `about` is on a
    whole hyphen-separated token of the folder's own name, never a
    substring ("chris" finds .../1-1-chris-wiggins; "pro" never finds
    "product"; "ps" never finds "platform-economics"). `area` narrows the
    search to that area's subtree. Only when nothing in the tree holds the
    work does the grammar build a path.
    """
    folders = _all_folder_paths(tree_paths)
    scoped = folders
    if area:
        scoped = {f for f in folders if f == area or f.startswith(area + "/")}

    if about:
        about_tokens = _tokens(about)
        exact: list[str] = []
        partial: list[str] = []
        for f in sorted(scoped):
            f_tokens = _tokens(_basename(f))
            if f_tokens == about_tokens:
                exact.append(f)
            elif _is_subsequence(about_tokens, f_tokens):
                partial.append(f)
        if exact:
            return {"path": exact[0], "reason": "already holds this work"}
        if partial:
            return {"path": partial[0], "reason": "matched on the name in its folder"}

    candidate = _grammar_path(about=about, area=area, kind_of=kind_of, date=date)
    if candidate in folders:
        return {"path": candidate, "reason": "already holds this work"}
    return {
        "path": candidate,
        "reason": "nothing in the tree holds this yet; built from the grammar",
    }
