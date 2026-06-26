"""Unit tests for the OKF frontmatter codec and bundle model."""

import pytest

from prism_service.okf import Bundle, Concept, dump_document, parse_document
from prism_service.okf.concept import OKF_VERSION, FrontmatterError


def test_parse_roundtrip_preserves_type_and_unknown_keys():
    text = (
        "---\n"
        "type: convention\n"
        "title: Task titles\n"
        "custom_x: keep-me\n"
        "---\n\n"
        "# Body\nFK to [customers](/brain/customers.md).\n"
    )
    c = parse_document(text, path="/memory/conventions/task-titles.md")
    assert c.type == "convention"
    assert c.title == "Task titles"
    # Unknown keys MUST be preserved (SPEC: consumer MUST NOT drop them).
    assert c.frontmatter["custom_x"] == "keep-me"
    # Round-trips back to a parseable concept with the same fields.
    again = parse_document(dump_document(c), path=c.path)
    assert again.frontmatter == c.frontmatter
    assert "FK to" in again.body


def test_document_without_frontmatter_parses_empty():
    c = parse_document("# Knowledge\n\n* item\n", path="/index.md")
    assert c.frontmatter == {}
    assert c.has_valid_type is False


def test_missing_type_is_detectable():
    c = parse_document("---\ntitle: x\n---\nbody\n")
    assert c.type == ""
    assert c.has_valid_type is False


def test_malformed_frontmatter_raises():
    with pytest.raises(FrontmatterError):
        parse_document("---\ntype: : : bad\n  - oops\n---\nbody\n", path="/bad.md")


def test_bundle_index_carries_okf_version_and_sections():
    b = Bundle()
    b.add(Concept(path="/memory/conv/a.md", frontmatter={"type": "convention", "title": "A"}, body="x"))
    b.add(Concept(path="/brain/foo.py.md", frontmatter={"type": "code", "title": "foo"}, body="y"))
    idx = b.render_index_md()
    assert f'okf_version: "{OKF_VERSION}"' in idx
    assert "## memory" in idx and "## brain" in idx
    assert "[A](/memory/conv/a.md)" in idx
    assert set(b.sections()) == {"memory", "brain"}
