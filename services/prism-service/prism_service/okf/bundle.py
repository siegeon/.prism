"""In-memory OKF bundle: a set of concepts plus reserved index.md / log.md.

This is a pure data structure — no filesystem or db. The host/producer layers
populate it (from projections or disk); the validator and serializers consume
it. A bundle is a directory of concept docs whose paths are bundle-relative and
root-absolute (begin with "/").
"""

from __future__ import annotations

from dataclasses import dataclass, field

from prism_service.okf.concept import OKF_VERSION, Concept, dump_document


@dataclass
class Bundle:
    """A collection of OKF concepts keyed by bundle-relative path."""

    concepts: dict[str, Concept] = field(default_factory=dict)
    okf_version: str = OKF_VERSION
    # Optional rendered reserved files (auto-generated if absent).
    index_md: str = ""
    log_md: str = ""

    def add(self, concept: Concept) -> None:
        self.concepts[concept.path] = concept

    def get(self, path: str) -> Concept | None:
        return self.concepts.get(path)

    def paths(self) -> list[str]:
        return sorted(self.concepts.keys())

    def sections(self) -> list[str]:
        """Top-level sections (first path segment) present in the bundle."""
        segs = set()
        for p in self.concepts:
            parts = p.strip("/").split("/")
            if len(parts) > 1:
                segs.add(parts[0])
        return sorted(segs)

    def render_index_md(self) -> str:
        """Auto-generate the root index.md (progressive disclosure).

        Reserved file: carries only `okf_version` frontmatter at the root, then
        section headings with `* [Title](/path) - description` list entries.
        """
        if self.index_md:
            return self.index_md
        lines = [f"---\nokf_version: \"{self.okf_version}\"\n---\n", "# Knowledge", ""]
        by_section: dict[str, list[Concept]] = {}
        for path in self.paths():
            section = path.strip("/").split("/")[0]
            by_section.setdefault(section, []).append(self.concepts[path])
        for section in sorted(by_section):
            lines.append(f"## {section}")
            for c in by_section[section]:
                title = c.title or c.path.rsplit("/", 1)[-1]
                desc = str(c.frontmatter.get("description", "") or "")
                entry = f"* [{title}]({c.path})"
                lines.append(f"{entry} - {desc}" if desc else entry)
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    def files(self) -> dict[str, str]:
        """Render the whole bundle to {bundle_path: markdown_text} for raw HTTP.

        Includes the auto-generated /index.md and (if present) /log.md.
        """
        out = {"/index.md": self.render_index_md()}
        if self.log_md:
            out["/log.md"] = self.log_md
        for path, concept in self.concepts.items():
            out[path] = dump_document(concept)
        return out
