"""Unit tests for OKF v0.1 conformance validation."""

from prism_service.okf import Bundle, Concept, validate


def _concept(path, type_="convention", body="b", **fm):
    fm["type"] = type_
    fm.setdefault("title", "T")
    fm.setdefault("description", "d")
    fm.setdefault("resource", "https://x")
    fm.setdefault("tags", ["t"])
    fm.setdefault("timestamp", "2026-06-26T00:00:00Z")
    return Concept(path=path, frontmatter=fm, body=body)


def test_conformant_bundle_passes_with_no_errors():
    b = Bundle()
    b.add(_concept("/memory/conv/a.md", body="see [B](/memory/conv/b.md)"))
    b.add(_concept("/memory/conv/b.md"))
    rep = validate(b)
    assert rep.ok
    assert rep.errors == []


def test_missing_type_is_an_error():
    b = Bundle()
    c = Concept(path="/memory/conv/x.md", frontmatter={"title": "x"}, body="b")
    b.add(c)
    rep = validate(b)
    assert not rep.ok
    assert any("type" in e for e in rep.errors)


def test_broken_link_is_only_a_warning_not_an_error():
    b = Bundle()
    b.add(_concept("/memory/conv/a.md", body="see [gone](/memory/conv/missing.md)"))
    rep = validate(b)
    assert rep.ok  # tolerant: broken links never fail conformance
    assert any("broken link" in w for w in rep.warnings)


def test_missing_recommended_fields_is_warning_only():
    b = Bundle()
    b.add(Concept(path="/memory/conv/a.md", frontmatter={"type": "convention"}, body="b"))
    rep = validate(b)
    assert rep.ok
    assert any("recommended" in w for w in rep.warnings)


def test_reserved_file_with_stray_frontmatter_is_error():
    b = Bundle()
    b.add(Concept(path="/index.md", frontmatter={"okf_version": "0.1", "type": "x"}, body="i"))
    rep = validate(b)
    assert not rep.ok
    assert any("reserved" in e for e in rep.errors)
