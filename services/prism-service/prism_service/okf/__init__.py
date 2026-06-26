"""Open Knowledge Format (OKF v0.1) support for PRISM.

OKF is Google Cloud's vendor-neutral standard (2026-06-12) for representing
curated knowledge as a directory of markdown files with YAML frontmatter,
cross-linked by markdown links. Spec: github.com/GoogleCloudPlatform/
knowledge-catalog/blob/main/okf/SPEC.md

This package is the PURE core (no daemon/db deps): a frontmatter codec, an
in-memory bundle model, a conformance validator, and a cross-link rewriter.
Producer/consumer/host layers build on top in services/okf_*.py.
"""

from prism_service.okf.concept import (
    OKF_VERSION,
    RESERVED_FILENAMES,
    Concept,
    dump_document,
    parse_document,
)
from prism_service.okf.bundle import Bundle
from prism_service.okf.validate import ValidationReport, validate

__all__ = [
    "OKF_VERSION",
    "RESERVED_FILENAMES",
    "Concept",
    "parse_document",
    "dump_document",
    "Bundle",
    "validate",
    "ValidationReport",
]
