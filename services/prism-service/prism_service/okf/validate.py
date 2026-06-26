"""OKF v0.1 conformance validator.

Implements the SPEC's three HARD rules as errors and the recommended-field /
link-health guidance as soft warnings (SPEC: consumers SHOULD treat everything
beyond the hard rules as soft guidance). A bundle is conformant iff there are
zero errors.

Hard rules:
  1. Every non-reserved .md has parseable YAML frontmatter.
  2. Every frontmatter block has a non-empty `type`.
  3. Reserved filenames (index.md/log.md) carry no concept frontmatter
     (index.md may carry only `okf_version` at the bundle root).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from prism_service.okf.bundle import Bundle
from prism_service.okf.concept import (
    RECOMMENDED_FIELDS,
    RESERVED_FILENAMES,
    Concept,
)
from prism_service.okf.links import extract_links


@dataclass
class ValidationReport:
    """Result of validating a bundle. `ok` is True iff no errors."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _is_reserved(path: str) -> bool:
    return path.rsplit("/", 1)[-1] in RESERVED_FILENAMES


def validate(bundle: Bundle) -> ValidationReport:
    """Validate a Bundle against OKF v0.1. Returns a ValidationReport."""
    rep = ValidationReport()
    known = set(bundle.paths())
    for path in bundle.paths():
        concept = bundle.concepts[path]
        if _is_reserved(path):
            # Rule 3: reserved files don't carry concept frontmatter.
            stray = set(concept.frontmatter) - {"okf_version"}
            if stray:
                rep.errors.append(f"{path}: reserved file carries frontmatter {sorted(stray)}")
            continue
        # Rule 2 (implies rule 1 parsed): non-empty type.
        if not concept.has_valid_type:
            rep.errors.append(f"{path}: missing or empty required `type` field")
        # Soft: recommended fields.
        missing = [f for f in RECOMMENDED_FIELDS if f not in concept.frontmatter]
        if missing:
            rep.warnings.append(f"{path}: missing recommended fields {missing}")
        # Soft: broken intra-bundle links (consumers MUST tolerate; we warn).
        for href in extract_links(concept.body):
            if href.startswith("/") and href not in known:
                rep.warnings.append(f"{path}: broken link -> {href}")
    return rep

