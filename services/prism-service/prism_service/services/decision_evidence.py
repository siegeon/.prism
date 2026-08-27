"""Backfill evidence on legacy decision memories from their own text.

The evidence field on decision memories that have empty evidence is populated
by scanning their description/summary for references to files, tasks, commits,
versions, and memory ids. All extracted values must be verbatim substrings
that appear in the input text — nothing is inferred or synthesized.

Three functions:
  - extract_evidence(text: str) -> dict: parse text and return only keys with hits
  - dry_run(project: str, limit: int) -> dict: read-only audit of what would change
  - apply(project: str, ids: list[str]) -> dict: persist changes to MemoryService
"""

import re
from typing import Optional
from prism_service.project_context import get_project


def extract_evidence(text: str) -> dict:
    """Extract evidence references from text.

    Returns only keys that have hits. Every value is a verbatim substring
    that appeared in the input text.

    Supported evidence types:
      - files: repo-relative paths ending in .py/.ts/.tsx/.ttl/.md/.json/.yaml/.js
      - tasks: 8-hex or full uuid task ids (after "task" keyword or in [task:...])
      - commits: 7-40 char hex shas (after "commit" keyword or in parentheses)
      - versions: version strings like "7.13.86"
      - memories: "mx-" + 6 hex character ids
    """
    result = {}

    # Extract files: path components ending in known extensions
    files = set()
    # Match path/to/file.ext where ext is one of the known types
    file_pattern = r'\b([a-zA-Z0-9_./\-]+\.(?:py|ts|tsx|ttl|md|json|yaml|js))\b'
    for match in re.finditer(file_pattern, text):
        candidate = match.group(1)
        # Verify it's actually in the text (should be, but double-check)
        if candidate in text:
            files.add(candidate)
    if files:
        result["files"] = sorted(files)

    # Extract tasks: 8-hex or full uuid format
    # Pattern: after "task" keyword or in [task:...] bracket tag
    tasks = set()
    # Pattern 1: "task <id>" where id is 8 hex or uuid
    task_pattern = r'task\s+([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{8})\b'
    for match in re.finditer(task_pattern, text, re.IGNORECASE):
        task_id = match.group(1)
        if task_id in text:
            tasks.add(task_id)

    # Pattern 2: [task:<id>] format
    bracket_pattern = r'\[task:([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|[0-9a-f]{8})\]'
    for match in re.finditer(bracket_pattern, text, re.IGNORECASE):
        task_id = match.group(1)
        if task_id in text:
            tasks.add(task_id)

    if tasks:
        result["tasks"] = sorted(tasks)

    # Extract commits: 7-40 hex character shas
    commits = set()
    # Pattern 1: after "commit" keyword
    commit_pattern = r'commit\s+([0-9a-f]{7,40})\b'
    for match in re.finditer(commit_pattern, text, re.IGNORECASE):
        sha = match.group(1)
        if sha in text:
            commits.add(sha)

    # Pattern 2: in parentheses (abc1234)
    paren_pattern = r'\(([0-9a-f]{7,40})\)'
    for match in re.finditer(paren_pattern, text):
        sha = match.group(1)
        # Only add if it looks like a commit sha (7-40 hex chars)
        if len(sha) >= 7 and re.match(r'^[0-9a-f]{7,40}$', sha) and sha in text:
            commits.add(sha)

    if commits:
        result["commits"] = sorted(commits)

    # Extract versions: X.Y.Z format (e.g., 7.13.86)
    versions = set()
    version_pattern = r'\b\d+\.\d+\.\d+\b'
    for match in re.finditer(version_pattern, text):
        version = match.group(0)
        if version in text:
            versions.add(version)
    if versions:
        result["versions"] = sorted(versions)

    # Extract memories: mx-<6 hex chars>
    memories = set()
    memory_pattern = r'\bmx-([0-9a-f]{6})\b'
    for match in re.finditer(memory_pattern, text):
        memory_id = f"mx-{match.group(1)}"
        if memory_id in text:
            memories.add(memory_id)
    if memories:
        result["memories"] = sorted(memories)

    return result


def dry_run(project: str, limit: int = 200) -> dict:
    """Audit which decision memories would be backfilled.

    Reads active decision memories with empty evidence from MemoryService,
    extracts evidence from their description/summary, and returns a summary
    of what would change (read-only, no writes).

    Args:
        project: Project name for MemoryService
        limit: Max decision entries to scan (default 200)

    Returns:
        dict with keys:
          - total_empty: count of decision memories with empty evidence
          - would_backfill: count with extractable evidence
          - no_reference: list of ids with no extractable references
          - samples: list of first ~15 entries with evidence (id, evidence, excerpt)
    """
    try:
        memory_svc = get_project(project).memory_svc
    except Exception as e:
        return {
            "total_empty": 0,
            "would_backfill": 0,
            "no_reference": [],
            "samples": [],
            "error": str(e),
        }

    # Gather all decision memories across all domains
    all_decisions = []
    for domain in memory_svc.list_domains():
        entries = memory_svc.list_entries(
            domain=domain,
            type_filter="decision",
            status_filter="active",
        )
        all_decisions.extend(entries)

    # Limit to requested amount
    decisions = all_decisions[:limit]

    total_empty = 0
    would_backfill = 0
    no_reference = []
    samples = []

    for entry in decisions:
        # Count entries with empty evidence
        if not entry.evidence:
            total_empty += 1

            # Extract evidence from the entry's text
            text = entry.description or ""
            if entry.summary:
                text = f"{text}\n{entry.summary}"

            extracted = extract_evidence(text)

            if extracted:
                # This entry has extractable evidence
                would_backfill += 1

                # Add to samples (limit to ~15)
                if len(samples) < 15:
                    # Create a 120-char excerpt
                    excerpt = text[:120]
                    if len(text) > 120:
                        excerpt = excerpt + "..."
                    samples.append({
                        "id": entry.id,
                        "name": entry.name,
                        "evidence": extracted,
                        "excerpt": excerpt,
                    })
            else:
                # No references found
                no_reference.append(entry.id)

    return {
        "total_empty": total_empty,
        "would_backfill": would_backfill,
        "no_reference": no_reference,
        "samples": samples,
    }


def apply(project: str, ids: list[str]) -> dict:
    """Update evidence on specified decision memories.

    Extracts evidence from each entry's description/summary and persists
    it through MemoryService.update. Idempotent: if evidence is already
    populated, the entry is left unchanged.

    Args:
        project: Project name for MemoryService
        ids: List of memory ids to update

    Returns:
        dict with:
          - updated: count of entries that were modified
          - skipped: count already having evidence
          - errors: list of error messages (if any)
    """
    try:
        memory_svc = get_project(project).memory_svc
    except Exception as e:
        return {
            "updated": 0,
            "skipped": 0,
            "errors": [str(e)],
        }

    updated = 0
    skipped = 0
    errors = []

    # Process in batches of 10
    batch_size = 10
    for i in range(0, len(ids), batch_size):
        batch_ids = ids[i : i + batch_size]

        for entry_id in batch_ids:
            try:
                entry = memory_svc.get_entry(entry_id)
                if not entry:
                    errors.append(f"Entry not found: {entry_id}")
                    continue

                # Skip if evidence is already populated
                if entry.evidence:
                    skipped += 1
                    continue

                # Extract evidence from description/summary
                text = entry.description or ""
                if entry.summary:
                    text = f"{text}\n{entry.summary}"

                extracted = extract_evidence(text)
                if extracted:
                    # Update the entry with extracted evidence
                    memory_svc.update_entry(entry_id, evidence=extracted)
                    updated += 1
                else:
                    # No evidence to extract, leave as-is
                    skipped += 1

            except Exception as e:
                errors.append(f"Error processing {entry_id}: {str(e)}")

    return {
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
    }
