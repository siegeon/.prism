"""Shared layout/id helpers for archify map builders."""

from __future__ import annotations

import re

_ID_OK = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*$")
_ID_BAD_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")


def slug(text: str) -> str:
    """Turn arbitrary text into a valid archify id: ^[a-zA-Z][a-zA-Z0-9_-]*$."""
    s = _ID_BAD_CHARS.sub("-", str(text)).strip("-")
    if not s:
        s = "n"
    if not s[0].isalpha():
        s = f"n-{s}"
    return s


def clip(text: str, n: int) -> str:
    """Clip text to at most n characters, adding an ellipsis when trimmed."""
    text = str(text)
    if len(text) <= n:
        return text
    if n <= 1:
        return text[:n]
    return text[: n - 1] + "…"


def place_grid(groups: list[list[str]], cols: int = 4) -> dict[str, tuple[int, int]]:
    """Place each group of ids on its own band of rows, left-to-right.

    Returns id -> (row, col). Each group starts a fresh row band directly
    below the previous group's rows.
    """
    positions: dict[str, tuple[int, int]] = {}
    row_offset = 0
    for group in groups:
        if not group:
            continue
        for i, item_id in enumerate(group):
            row = row_offset + (i // cols)
            col = i % cols
            positions[item_id] = (row, col)
        rows_used = (len(group) + cols - 1) // cols
        row_offset += max(rows_used, 1)
    return positions
