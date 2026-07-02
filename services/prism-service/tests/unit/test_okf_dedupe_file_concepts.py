"""RED scaffold — Understand Code domain must not list one file twice (task 61aafcc6).

/understand's Code domain listed the SAME file as two concepts — a bare
'prism_cli.py' and the qualified 'prism_service/cli/prism_cli.py' — inflating
the domain's concept count. The OKF graph projection must dedup file concepts by
resolved path: when one file concept's title is a segment-aligned path-suffix of
another's, they denote the same file; keep the most-qualified (longest) title.

Unit-tests the pure dedup helper the /api/okf/graph projection uses. FAILS
before the fix: prism_service.services.okf_host.dedupe_file_concepts does not
exist.
"""


def test_bare_and_qualified_same_file_collapse_to_one():
    from prism_service.services.okf_host import dedupe_file_concepts

    nodes = [
        {"id": "a", "type": "file", "title": "prism_cli.py", "domain": "code"},
        {"id": "b", "type": "file",
         "title": "prism_service/cli/prism_cli.py", "domain": "code"},
        {"id": "c", "type": "file", "title": "main.py", "domain": "code"},
    ]
    out = dedupe_file_concepts(nodes)
    titles = [n["title"] for n in out]
    # The qualified path survives; the bare-basename duplicate is dropped.
    assert "prism_service/cli/prism_cli.py" in titles
    assert "prism_cli.py" not in titles
    # Exactly one prism_cli file concept remains (count no longer inflated).
    assert sum(1 for n in out
               if n["type"] == "file" and n["title"].endswith("prism_cli.py")) == 1
    # An unrelated file is untouched.
    assert "main.py" in titles


def test_distinct_files_sharing_a_basename_are_not_merged():
    from prism_service.services.okf_host import dedupe_file_concepts

    nodes = [
        {"id": "a", "type": "file", "title": "pkg_a/utils.py", "domain": "code"},
        {"id": "b", "type": "file", "title": "pkg_b/utils.py", "domain": "code"},
    ]
    out = dedupe_file_concepts(nodes)
    assert len(out) == 2, "distinct qualified paths must not be merged"


def test_non_file_concepts_pass_through_untouched():
    from prism_service.services.okf_host import dedupe_file_concepts

    nodes = [
        {"id": "a", "type": "file", "title": "prism_cli.py", "domain": "code"},
        {"id": "b", "type": "file",
         "title": "prism_service/cli/prism_cli.py", "domain": "code"},
        # A non-file concept that happens to share the basename must survive.
        {"id": "d", "type": "decision", "title": "prism_cli.py",
         "domain": "architecture"},
    ]
    out = dedupe_file_concepts(nodes)
    assert any(n["id"] == "d" for n in out), "non-file concept wrongly deduped"
