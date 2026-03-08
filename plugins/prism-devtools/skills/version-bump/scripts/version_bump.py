#!/usr/bin/env python3
"""
Bump prism-devtools plugin version, update CHANGELOG, and create git tag.

Usage:
    python version_bump.py patch          # 3.5.0 → 3.5.1
    python version_bump.py minor          # 3.5.0 → 3.6.0
    python version_bump.py major          # 3.5.0 → 4.0.0
    python version_bump.py status         # Show current version and tag info
    python version_bump.py tag            # Create git tag for current version
    python version_bump.py sync-changelog # Add CHANGELOG stub for current version

Exit codes:
    0 = success
    1 = usage/validation error
    2 = file system error
"""
import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path


def _find_plugin_root() -> Path:
    """Walk up from script to find the prism-devtools plugin root."""
    current = Path(__file__).resolve().parent
    while current != current.parent:
        plugin_json = current / ".claude-plugin" / "plugin.json"
        if plugin_json.exists():
            return current
        current = current.parent
    # Fallback: relative from known script location
    fallback = Path(__file__).resolve().parent.parent.parent
    if (fallback / ".claude-plugin" / "plugin.json").exists():
        return fallback
    print("ERROR: Could not find plugin root with .claude-plugin/plugin.json", file=sys.stderr)
    sys.exit(2)


PLUGIN_ROOT = _find_plugin_root()
PLUGIN_JSON = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
CHANGELOG = PLUGIN_ROOT / "CHANGELOG.md"


def read_version() -> str:
    """Read current version from plugin.json."""
    data = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    return data.get("version", "0.0.0")


def write_version(new_version: str) -> None:
    """Update version in plugin.json, preserving formatting."""
    data = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    data["version"] = new_version
    PLUGIN_JSON.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def bump(current: str, part: str) -> str:
    """Compute new semver version."""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", current)
    if not match:
        print(f"ERROR: Invalid semver: {current}", file=sys.stderr)
        sys.exit(1)
    major, minor, patch = int(match[1]), int(match[2]), int(match[3])
    if part == "major":
        return f"{major + 1}.0.0"
    elif part == "minor":
        return f"{major}.{minor + 1}.0"
    elif part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        print(f"ERROR: Unknown bump type: {part}. Use major/minor/patch.", file=sys.stderr)
        sys.exit(1)


def update_changelog(new_version: str) -> bool:
    """Prepend a new version section to CHANGELOG.md. Returns True if updated."""
    if not CHANGELOG.exists():
        print(f"WARNING: CHANGELOG.md not found at {CHANGELOG}", file=sys.stderr)
        return False

    text = CHANGELOG.read_text(encoding="utf-8")

    # Check if this version already has a section
    if f"## [{new_version}]" in text:
        print(f"  CHANGELOG already has [{new_version}] section, skipping.")
        return False

    today = date.today().isoformat()
    new_section = f"## [{new_version}] - {today}\n\n### Added\n\n### Fixed\n\n"

    # Insert after the header lines (before first ## [x.y.z] section)
    first_version = re.search(r"^## \[\d+\.\d+\.\d+\]", text, re.MULTILINE)
    if first_version:
        insert_pos = first_version.start()
        text = text[:insert_pos] + new_section + text[insert_pos:]
    else:
        # No existing versions, append after header
        text += "\n" + new_section

    CHANGELOG.write_text(text, encoding="utf-8")
    return True


def create_tag(version: str) -> bool:
    """Create an annotated git tag if it doesn't exist."""
    tag_name = f"v{version}"
    # Check if tag exists
    result = subprocess.run(
        ["git", "tag", "-l", tag_name],
        capture_output=True, text=True, cwd=PLUGIN_ROOT,
    )
    if tag_name in result.stdout.strip().split("\n"):
        print(f"  Tag {tag_name} already exists, skipping.")
        return False

    subprocess.run(
        ["git", "tag", "-a", tag_name, "-m", f"Release {version}"],
        check=True, cwd=PLUGIN_ROOT,
    )
    print(f"  Created tag: {tag_name}")
    return True


def git_status() -> None:
    """Show version status: plugin.json, CHANGELOG, and tags."""
    current = read_version()
    print(f"  plugin.json version: {current}")

    # Latest CHANGELOG version
    if CHANGELOG.exists():
        text = CHANGELOG.read_text(encoding="utf-8")
        match = re.search(r"## \[(\d+\.\d+\.\d+)\]", text)
        changelog_ver = match.group(1) if match else "none"
        print(f"  CHANGELOG latest:    {changelog_ver}")
        if changelog_ver != current:
            print(f"  ⚠ CHANGELOG is out of sync (expected {current})")
    else:
        print("  CHANGELOG: not found")

    # Latest git tag
    result = subprocess.run(
        ["git", "tag", "--sort=-v:refname"],
        capture_output=True, text=True, cwd=PLUGIN_ROOT,
    )
    tags = [t.strip() for t in result.stdout.strip().split("\n") if t.strip()]
    latest_tag = tags[0] if tags else "none"
    print(f"  Latest git tag:      {latest_tag}")
    if latest_tag != f"v{current}":
        print(f"  ⚠ Git tag is out of sync (expected v{current})")

    # Commits since latest tag
    if tags:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{latest_tag}..HEAD"],
            capture_output=True, text=True, cwd=PLUGIN_ROOT,
        )
        count = result.stdout.strip()
        print(f"  Commits since tag:   {count}")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print("Usage: version_bump.py <major|minor|patch|status|tag|sync-changelog>")
        sys.exit(1)

    action = sys.argv[1].lower()

    if action == "status":
        git_status()
        return

    current = read_version()

    if action == "tag":
        create_tag(current)
        return

    if action == "sync-changelog":
        if update_changelog(current):
            print(f"  Added CHANGELOG section for [{current}]")
        return

    if action not in ("major", "minor", "patch"):
        print(f"ERROR: Unknown action: {action}. Use major/minor/patch/status/tag/sync-changelog.",
              file=sys.stderr)
        sys.exit(1)

    new_version = bump(current, action)
    print(f"  Bumping: {current} → {new_version}")

    write_version(new_version)
    print(f"  Updated plugin.json")

    if update_changelog(new_version):
        print(f"  Updated CHANGELOG.md with [{new_version}] section")

    create_tag(new_version)

    print(f"\n  ✓ Version bumped to {new_version}")
    print(f"  Next steps:")
    print(f"    1. Fill in CHANGELOG.md [{new_version}] section with changes")
    print(f"    2. git add -A && git commit -m 'PLAT-XXXX Bump version {current} → {new_version}'")
    print(f"    3. git push origin main --tags")


if __name__ == "__main__":
    main()
