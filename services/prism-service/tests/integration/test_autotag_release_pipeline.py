"""RED scaffold — STRAND A: auto-tag on version-bump landing (task 56458db1).

A landed PRISM_VERSION bump on main strands UN-published today: the only
thing that pushes a v<version> tag is a manual /ship, so release.yml (which
triggers ONLY on `push: tags: v*`) never fires for a merged bump (memory
project_implement_merges_strand_unreleased_versions). This task adds a NEW
.github/workflows/autotag.yml that, on push to main scoped to the service
paths, reads PRISM_VERSION and creates+pushes the v<version> tag ONLY when
no matching tag exists — which then cascades into release.yml's existing
tag trigger, producing the GitHub Release wheel with NO manual /ship.

Source-structure asserts (the CI workflow file IS the user-facing seam —
a backend that "knows" the version but no workflow that pushes the tag is
the exact stranding the task removes). ALL FAIL today: autotag.yml does
not exist.

Acceptance criteria pinned here:
  A1 — autotag.yml exists, on.push.branches==[main], path filter scoped
       to services/prism-service/**.
  A2 — reads PRISM_VERSION from
       services/prism-service/prism_service/__version__.py, computes tag
       name v<version>.
  A3 — creates+pushes the tag ONLY when no matching tag already exists
       (idempotent — guarded so a re-run on main with an existing tag is
       a no-op, never a duplicate/erroring push).
  A4 — the pushed v<version> tag cascades into release.yml's existing
       tag trigger (release.yml is UNCHANGED — its `push: tags: v*`
       trigger is what the autotag push lands on).
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent          # services/prism-service
_REPO_ROOT = _SERVICE_ROOT.parent.parent            # E:/.prism
_WORKFLOWS = _REPO_ROOT / ".github" / "workflows"
_AUTOTAG = _WORKFLOWS / "autotag.yml"
_RELEASE = _WORKFLOWS / "release.yml"
_VERSION_FILE_REL = "services/prism-service/prism_service/__version__.py"


def _autotag_src() -> str:
    return _AUTOTAG.read_text(encoding="utf-8")


# ── A1: workflow exists, triggers on push to main, path-scoped ───────────

def test_autotag_workflow_file_exists():
    assert _AUTOTAG.exists(), (
        ".github/workflows/autotag.yml does not exist — a merged "
        "PRISM_VERSION bump strands un-published (no tag is ever pushed "
        "except a manual /ship)"
    )


def test_autotag_triggers_on_push_to_main():
    """on.push.branches must include main — the tag is cut when a bump
    LANDS on main, not on every branch."""
    import yaml

    doc = yaml.safe_load(_autotag_src())
    # PyYAML parses the bare `on:` key as the boolean True.
    on = doc.get("on", doc.get(True))
    assert on is not None, "autotag.yml has no `on:` trigger block"
    push = on.get("push") if isinstance(on, dict) else None
    assert push is not None, "autotag.yml does not trigger on push"
    branches = push.get("branches")
    assert branches and "main" in branches, (
        "autotag.yml push trigger is not scoped to branches:[main] — it "
        f"must fire when a bump lands on main (got branches={branches!r})"
    )


def test_autotag_path_scoped_to_service():
    """The push trigger must be path-filtered to services/prism-service/**
    so unrelated commits (docs, plugin) don't trigger a tag attempt."""
    import yaml

    doc = yaml.safe_load(_autotag_src())
    on = doc.get("on", doc.get(True))
    push = on.get("push") if isinstance(on, dict) else None
    paths = (push or {}).get("paths") or (push or {}).get("paths-ignore")
    assert paths, (
        "autotag.yml push trigger has no path filter — it must be scoped "
        "to services/prism-service/** (the version source lives there)"
    )
    joined = " ".join(paths)
    assert "services/prism-service" in joined, (
        "autotag.yml path filter does not scope to services/prism-service/** "
        f"(got paths={paths!r})"
    )


# ── A2: reads PRISM_VERSION from the version file, computes v<version> ────

def test_autotag_reads_prism_version_from_version_file():
    src = _autotag_src()
    assert "__version__.py" in src or _VERSION_FILE_REL in src, (
        "autotag.yml does not read the version from "
        f"{_VERSION_FILE_REL} — it must source PRISM_VERSION from the "
        "single source of truth, not a hard-coded literal"
    )
    assert "PRISM_VERSION" in src, (
        "autotag.yml never references PRISM_VERSION — it cannot compute "
        "the tag name from the version"
    )


def test_autotag_computes_v_prefixed_tag_name():
    """The tag must be v<version> (matching release.yml's `tags: v*`)."""
    src = _autotag_src()
    # The computed tag must carry the leading 'v' so it matches the
    # release.yml trigger glob `v*`. Look for a v-prefix being applied to
    # the read version (e.g. "v$VERSION" / "v${VERSION}" / 'v' . version).
    assert ("v$" in src or "v${" in src or 'v"$' in src
            or "\"v\"" in src or "'v'" in src or "v{{" in src), (
        "autotag.yml does not prefix the version with 'v' to form the tag "
        "name — release.yml triggers on `tags: v*`, so a bare version tag "
        "would never cascade into the publish flow"
    )


# ── A3: idempotent — only create+push when no matching tag exists ────────

def test_autotag_is_idempotent_guard_present():
    """A re-run on main with an existing v<version> tag must be a no-op —
    the workflow must check whether the tag already exists before pushing
    (git ls-remote / git tag -l / rev-parse guard), never blindly push a
    duplicate that errors the job."""
    src = _autotag_src().lower()
    has_existence_check = (
        "ls-remote" in src
        or "git tag -l" in src
        or "rev-parse" in src
        or "git show-ref" in src
        or "tag --list" in src
    )
    assert has_existence_check, (
        "autotag.yml has no existing-tag guard — re-running on main with "
        "the tag already present would push a duplicate (error) instead of "
        "a clean no-op (not idempotent)"
    )


def test_autotag_pushes_the_tag():
    """The workflow must actually create AND push the tag (the push is what
    lands on release.yml's `push: tags` trigger)."""
    src = _autotag_src()
    assert "git push" in src and "tag" in src.lower(), (
        "autotag.yml never pushes a git tag — without the push, release.yml "
        "is never triggered and the bump stays un-published"
    )


# ── A4: cascades into the UNCHANGED release.yml tag trigger ──────────────

def test_release_yml_tag_trigger_unchanged():
    """release.yml must still trigger on `push: tags: v*` — the autotag
    push is designed to cascade into it WITHOUT editing release.yml. This
    guards the contract: the pushed tag is what release.yml consumes."""
    assert _RELEASE.exists(), "release.yml missing — nothing to cascade into"
    import yaml

    doc = yaml.safe_load(_RELEASE.read_text(encoding="utf-8"))
    on = doc.get("on", doc.get(True))
    tags = ((on or {}).get("push") or {}).get("tags")
    assert tags and any(t == "v*" or t.startswith("v") for t in tags), (
        "release.yml no longer triggers on `push: tags: v*` — the autotag "
        "cascade contract is broken (the pushed v<version> tag must land "
        "on this trigger to publish the wheel)"
    )
