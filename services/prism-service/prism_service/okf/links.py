"""Cross-link rewriting between PRISM's link forms and OKF markdown links.

PRISM authors links two ways: memory `[[name]]` wikilinks and graph edges
(source->target). OKF expresses a relationship as a normal markdown link,
bundle-root-absolute by preference: `[label](/section/slug.md)`. These helpers
convert in both directions and are deliberately tolerant — per SPEC a consumer
MUST tolerate broken links, so resolution returns None rather than raising.
"""

from __future__ import annotations

import re

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_MDLINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")


def wikilinks_to_md(body: str, resolver) -> str:
    """Rewrite `[[name]]` -> `[name](/path.md)` using resolver(name)->path|None.

    Unresolved names are left as `[[name]]` text (tolerant; not an error).
    """

    def _sub(m: re.Match) -> str:
        name = m.group(1).strip()
        path = resolver(name)
        return f"[{name}]({path})" if path else m.group(0)

    return _WIKILINK_RE.sub(_sub, body)


def extract_links(body: str) -> list[str]:
    """Return every markdown link target (href) in a concept body, in order."""
    return [m.group(2).strip() for m in _MDLINK_RE.finditer(body)]


def edge_to_mdlink(label: str, target_path: str) -> str:
    """Render a graph edge as a bundle-root-absolute markdown link."""
    return f"[{label}]({target_path})"
