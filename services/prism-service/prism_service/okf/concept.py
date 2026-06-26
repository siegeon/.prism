"""OKF concept document: frontmatter + markdown body, and its codec.

The single frontmatter codec for PRISM — generalizes the hand-rolled scanner
that used to live at api/memory.py (_parse_claude_memory). A *concept* is any
non-reserved .md file; OKF requires exactly one field: a non-empty `type`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

OKF_VERSION = "0.1"

# Reserved filenames carry structure, not concept frontmatter (SPEC §reserved).
RESERVED_FILENAMES = {"index.md", "log.md"}

# Recommended frontmatter fields, in spec priority order.
RECOMMENDED_FIELDS = ("title", "description", "resource", "tags", "timestamp")

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?(.*)\Z", re.DOTALL)


@dataclass
class Concept:
    """A single OKF concept document.

    `path` is the bundle-relative identity (e.g. "/memory/conventions/x.md").
    `frontmatter` preserves every key (unknown keys included, per SPEC: a
    consumer MUST NOT drop them). `body` is the UTF-8 markdown after the block.
    """

    path: str = ""
    frontmatter: dict = field(default_factory=dict)
    body: str = ""

    @property
    def type(self) -> str:
        return str(self.frontmatter.get("type", "") or "")

    @property
    def title(self) -> str:
        return str(self.frontmatter.get("title", "") or "")

    @property
    def has_valid_type(self) -> bool:
        """OKF's one hard per-concept rule: a non-empty string `type`."""
        return bool(self.type.strip())


class FrontmatterError(ValueError):
    """Raised when a concept's YAML frontmatter cannot be parsed."""


def parse_document(text: str, path: str = "") -> Concept:
    """Parse raw .md text into a Concept.

    A document with no leading `---` block parses to empty frontmatter (the
    reserved index.md/log.md case) — never raises for that. Malformed YAML
    inside a real block raises FrontmatterError so validate() can report it.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return Concept(path=path, frontmatter={}, body=text.strip())
    raw, body = m.group(1), m.group(2)
    try:
        fm = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:  # malformed frontmatter
        raise FrontmatterError(f"{path or '<doc>'}: {exc}") from exc
    if not isinstance(fm, dict):
        raise FrontmatterError(f"{path or '<doc>'}: frontmatter is not a mapping")
    return Concept(path=path, frontmatter=fm, body=body.strip())


def dump_document(concept: Concept) -> str:
    """Serialize a Concept back to conformant .md text (frontmatter + body).

    Round-trips parse_document for concepts with frontmatter. Concepts with
    empty frontmatter (reserved files) emit the body alone, no `---` block.
    """
    if not concept.frontmatter:
        return concept.body.strip() + "\n"
    fm = yaml.safe_dump(
        concept.frontmatter, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).strip()
    return f"---\n{fm}\n---\n\n{concept.body.strip()}\n"

