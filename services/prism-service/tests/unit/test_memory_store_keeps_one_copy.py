"""Task f0633425 — the memory store keeps ONE copy of a fact.

RED today. ``MemoryService.store`` mints a fresh ``mx-`` id on every call
(``_generate_id``), so re-seeding a memory whose ``name`` AND
``description`` are byte-identical to one already on disk APPENDS a row and
archives the previous one. Measured on the live prism store 2026-08-30:
``/api/memory/entries?project=prism`` served 7,441,732 bytes over 2,799
entries, of which 2,308 rows were surplus copies in 16 groups — one group
(``feedback-codex-doctrine-scope``) held 252 byte-identical generations.

This file pins four claims:

  1. store() called twice with the same domain + name + description leaves
     ONE entry, with the id and recorded_at of the first write.
  2. Two entries that share a name but NOT a description are two real
     facts. The reduction pass must never fold them together.
  3. deduplicate() keeps the EARLIEST member of each byte-identical group
     and archives the rest OUT of the live domain file. No row is deleted:
     every dropped row is readable afterwards in the archive sidecar.
  4. deduplicate(dry_run=True) writes nothing and reports the actual text
     it would keep and drop, not only counts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))

from prism_service.services.memory_service import MemoryService


_FACT = (
    "A conductor gate is decided by a distinct actor. "
    "The producing session cannot clear its own gate."
)

# A SECOND, genuinely different fact that shares the first one's name.
# Kept far below the 0.85 similarity threshold store() already applies.
_OTHER_FACT = (
    "Rebuild the web bundle after a change to the screen. "
    "A bounce of the daemon proves the Python build only."
)

_NAME = "gates-need-a-distinct-actor"
_DOMAIN = "gates"


def _svc(tmp_path: Path) -> MemoryService:
    return MemoryService(str(tmp_path / "mulch"))


def _rows(svc: MemoryService, domain: str) -> list[dict]:
    """Raw JSONL rows of a live domain file, in file order."""
    return _read_jsonl(svc._domain_file(domain))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _seed(svc: MemoryService, domain: str, rows: list[dict]) -> None:
    """Write raw rows straight to a domain file. This bypasses store()."""
    path = svc._domain_file(domain)
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8",
    )


def _copy(entry_id: str, when: str, **over) -> dict:
    """One byte-identical generation of the same fact."""
    row = {
        "id": entry_id,
        "type": "convention",
        "name": _NAME,
        "description": _FACT,
        "summary": "",
        "classification": "foundational",
        "recorded_at": when,
        "outcomes": [],
        "evidence": {},
        "domain": _DOMAIN,
        "recall_count": 0,
        "last_recalled": "",
        "status": "archived",
        "valid_at": when,
        "invalid_at": when,
        "importance": 8,
        "memory_type": "semantic",
        "generation": 1,
        "effectiveness": 0.0,
        "adr_status": "",
        "supersedes": "",
        "owner_user_id": "",
    }
    row.update(over)
    return row


def _three_copies(svc: MemoryService) -> None:
    _seed(svc, _DOMAIN, [
        _copy("mx-000001", "2026-01-01T00:00:00+00:00"),
        _copy("mx-000002", "2026-02-01T00:00:00+00:00"),
        _copy("mx-000003", "2026-03-01T00:00:00+00:00",
              status="active", invalid_at="", recall_count=4,
              last_recalled="2026-03-02T00:00:00+00:00"),
    ])


# ---------------------------------------------------------------------
# 1. The producer keeps one copy
# ---------------------------------------------------------------------


def test_storing_the_same_fact_twice_leaves_one_entry(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    first = svc.store(
        domain=_DOMAIN, name=_NAME, description=_FACT,
        type="convention", classification="foundational", importance=8,
    )
    second = svc.store(
        domain=_DOMAIN, name=_NAME, description=_FACT,
        type="convention", classification="foundational", importance=8,
    )

    rows = _rows(svc, _DOMAIN)
    assert len(rows) == 1, f"the second store appended a row: {rows}"
    assert second.id == first.id
    assert rows[0]["id"] == first.id
    assert rows[0]["recorded_at"] == first.recorded_at
    assert rows[0]["status"] == "active"
    assert rows[0]["invalid_at"] == ""


def test_ten_identical_stores_still_leave_one_entry(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    for _ in range(10):
        svc.store(
            domain=_DOMAIN, name=_NAME, description=_FACT,
            type="convention", classification="foundational", importance=8,
        )
    assert len(_rows(svc, _DOMAIN)) == 1


# ---------------------------------------------------------------------
# 2. Two facts under one name stay two facts
# ---------------------------------------------------------------------


def test_a_same_name_different_description_pair_is_not_merged(
    tmp_path: Path,
) -> None:
    svc = _svc(tmp_path)
    svc.store(
        domain=_DOMAIN, name=_NAME, description=_FACT,
        type="convention", classification="foundational",
    )
    svc.store(
        domain=_DOMAIN, name=_NAME, description=_OTHER_FACT,
        type="convention", classification="foundational",
    )
    assert len(_rows(svc, _DOMAIN)) == 2

    report = svc.deduplicate(dry_run=False)
    assert report["rows_archived"] == 0
    assert report["groups"] == 0

    rows = _rows(svc, _DOMAIN)
    assert len(rows) == 2, "the pass merged two different facts"
    kept_text = {r["description"] for r in rows}
    assert len(kept_text) == 2


# ---------------------------------------------------------------------
# 3. The reduction pass keeps the earliest and archives the rest
# ---------------------------------------------------------------------


def test_the_pass_keeps_the_earliest_and_archives_the_rest(
    tmp_path: Path,
) -> None:
    svc = _svc(tmp_path)
    _three_copies(svc)

    report = svc.deduplicate(dry_run=False)
    assert report["dry_run"] is False
    assert report["groups"] == 1
    assert report["rows_archived"] == 2

    live = _rows(svc, _DOMAIN)
    assert len(live) == 1
    assert live[0]["id"] == "mx-000001", "the earliest copy must survive"
    assert live[0]["recorded_at"] == "2026-01-01T00:00:00+00:00"
    # The survivor carries the group's CURRENT state, so the fact stays
    # recallable — keeping the earliest row must not retire a live memory.
    assert live[0]["status"] == "active"
    assert live[0]["invalid_at"] == ""
    assert live[0]["recall_count"] == 4
    assert live[0]["last_recalled"] == "2026-03-02T00:00:00+00:00"


def test_the_pass_archives_and_never_deletes(tmp_path: Path) -> None:
    svc = _svc(tmp_path)
    _three_copies(svc)

    svc.deduplicate(dry_run=False)

    live = _rows(svc, _DOMAIN)
    archived = _read_jsonl(svc._archive_file(_DOMAIN))
    assert [r["id"] for r in archived] == ["mx-000002", "mx-000003"]
    assert all(r["status"] == "archived" for r in archived)
    assert all(r["invalid_at"] for r in archived)
    assert all(r["description"] == _FACT for r in archived)
    # Every seeded row is still readable somewhere on disk.
    assert len(live) + len(archived) == 3


def test_the_archive_sidecar_is_not_a_domain(tmp_path: Path) -> None:
    """The archive must not come back through list_domains / the API."""
    svc = _svc(tmp_path)
    _three_copies(svc)
    svc.deduplicate(dry_run=False)

    assert svc.list_domains() == [_DOMAIN]
    all_ids = {e.id for e in svc._all_entries()}
    assert all_ids == {"mx-000001"}


# ---------------------------------------------------------------------
# 4. The dry run writes nothing and shows the text
# ---------------------------------------------------------------------


def test_the_dry_run_writes_nothing_and_shows_the_text(
    tmp_path: Path,
) -> None:
    svc = _svc(tmp_path)
    _three_copies(svc)
    before = svc._domain_file(_DOMAIN).read_text(encoding="utf-8")

    report = svc.deduplicate(dry_run=True)

    assert report["dry_run"] is True
    assert report["groups"] == 1
    assert report["rows_archived"] == 2
    assert svc._domain_file(_DOMAIN).read_text(encoding="utf-8") == before
    assert not svc._archive_file(_DOMAIN).exists()

    samples = report["samples"]
    assert samples, "a dry run must show samples, not only counts"
    sample = samples[0]
    assert sample["domain"] == _DOMAIN
    assert sample["name"] == _NAME
    assert sample["copies"] == 3
    assert sample["kept_id"] == "mx-000001"
    assert sample["dropped_ids"] == ["mx-000002", "mx-000003"]
    # The reader must be able to SEE the text, both sides of the decision.
    assert _FACT[:40] in sample["kept_description"]
    assert _FACT[:40] in sample["dropped_description"]
