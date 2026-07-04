"""RED tests for the incremental transcript tail-parser (task 88a5b228).

Every ~5s conductor poll re-reads and json.loads the ENTIRE transcript
whenever the (mtime,size) cache key misses -- and an in-progress session's
file grows every turn, so the cache misses on every poll. These tests pin
the incremental contract: on growth, only appended bytes are read; totals
and event lists stay byte-identical to a cold parse; truncation/rotation
forces a full re-parse; a partial trailing line is never counted until it
is completed.

The incrementality tests (AC-1, AC-5) FAIL on main because the summers
full-read the file on every cache miss. The correctness tests (AC-2..AC-4)
guard the incremental path against regressions.
"""
from __future__ import annotations

import json
import os

import pytest

from prism_service.services import claude_transcripts as ct


def _line(out: int, inp: int = 0, cr: int = 0, cc: int = 0, ts: str | None = None) -> str:
    """One assistant transcript line carrying a usage block.
    Billable per sum_usage = out + inp + cr + cc."""
    evt = {"type": "assistant", "message": {"usage": {
        "output_tokens": out, "input_tokens": inp,
        "cache_read_input_tokens": cr, "cache_creation_input_tokens": cc,
    }}}
    if ts is not None:
        evt["timestamp"] = ts
    return json.dumps(evt)


def _write(path, lines, *, trailing_newline=True):
    text = "\n".join(lines)
    if trailing_newline and lines:
        text += "\n"
    path.write_text(text, encoding="utf-8")


def _bump_mtime(path):
    """Force a distinct, larger mtime so a same-second append is still seen."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 2))


@pytest.fixture(autouse=True)
def _clear_caches():
    ct._TOKEN_CACHE.clear()
    ct._TOKEN_EVENTS_CACHE.clear()
    yield
    ct._TOKEN_CACHE.clear()
    ct._TOKEN_EVENTS_CACHE.clear()


class _ByteCounter:
    """Wrap Path.read_text and builtins.open so a test can measure how many
    bytes a summer physically pulled from ONE target path during a call.
    A full re-read counts ~file size; an incremental read counts ~tail size."""

    def __init__(self, monkeypatch, target):
        self.target = str(target)
        self.bytes_read = 0
        real_read_text = ct.Path.read_text
        real_open = open

        def counting_read_text(path_self, *a, **k):
            data = real_read_text(path_self, *a, **k)
            if str(path_self) == self.target:
                self.bytes_read += len(data.encode("utf-8", "replace"))
            return data

        def counting_open(file, *a, **k):
            fh = real_open(file, *a, **k)
            mode = k.get("mode", a[0] if a else "r")
            if str(file) == self.target and "b" in mode:
                return _CountingBinary(fh, self)
            return fh

        monkeypatch.setattr(ct.Path, "read_text", counting_read_text)
        monkeypatch.setattr("builtins.open", counting_open)


class _CountingBinary:
    def __init__(self, fh, counter):
        self._fh = fh
        self._counter = counter

    def read(self, *a, **k):
        data = self._fh.read(*a, **k)
        self._counter.bytes_read += len(data)
        return data

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def __enter__(self):
        self._fh.__enter__()
        return self

    def __exit__(self, *a):
        return self._fh.__exit__(*a)


# --- AC-1: append re-reads only appended bytes; total == cold parse ---------

def test_append_reads_only_new_bytes_and_total_matches_cold(tmp_path, monkeypatch):
    p = tmp_path / "s.jsonl"
    _write(p, [_line(100), _line(50)])
    assert ct._sum_billable_tokens(p) == 150            # cold parse -> cache

    _write(p, [_line(100), _line(50), _line(7)])        # append one line
    _bump_mtime(p)
    counter = _ByteCounter(monkeypatch, p)
    total = ct._sum_billable_tokens(p)

    assert total == 157, "incremental total must equal a cold full parse"
    appended = len((_line(7) + "\n").encode("utf-8"))
    assert counter.bytes_read <= appended * 2, (
        f"expected to read only the appended tail (~{appended}B), read "
        f"{counter.bytes_read}B -- summer is not incremental"
    )


# --- AC-2: truncation/rotation forces a full re-parse -----------------------

def test_truncation_triggers_full_reparse(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_line(100), _line(100), _line(100)])
    assert ct._sum_billable_tokens(p) == 300

    _write(p, [_line(5)])                                # smaller -> rotation
    _bump_mtime(p)
    assert ct._sum_billable_tokens(p) == 5, (
        "a shrunk file must be fully re-parsed, not served from a stale offset"
    )


# --- AC-3: partial trailing line not counted until completed ----------------

def test_partial_trailing_line_counted_once_when_completed(tmp_path):
    p = tmp_path / "s.jsonl"
    _write(p, [_line(100)])
    assert ct._sum_billable_tokens(p) == 100

    full = _line(40)
    half = full[: len(full) // 2]                        # truncated json, no newline
    p.write_text(_line(100) + "\n" + half, encoding="utf-8")
    _bump_mtime(p)
    assert ct._sum_billable_tokens(p) == 100, "a partial last line must not be counted"

    p.write_text(_line(100) + "\n" + full + "\n", encoding="utf-8")
    _bump_mtime(p)
    assert ct._sum_billable_tokens(p) == 140, (
        "completing the line must count it exactly once (not zero, not twice)"
    )


# --- AC-4: _token_events incremental list == cold parse ---------------------

def test_token_events_incremental_matches_cold(tmp_path):
    p = tmp_path / "s.jsonl"
    base = [_line(10, ts="2026-07-04T10:00:00Z"),
            _line(20, ts="2026-07-04T10:00:05Z")]
    _write(p, base)
    ct._token_events(p)                                  # warm

    for i, extra in enumerate([
        _line(30, ts="2026-07-04T10:00:10Z"),
        _line(40, ts="2026-07-04T10:00:15Z"),
        _line(50, ts="2026-07-04T10:00:20Z"),
    ]):
        base.append(extra)
        _write(p, base)
        _bump_mtime(p)
        incremental = ct._token_events(p)

        ct._TOKEN_EVENTS_CACHE.clear()
        cold = ct._token_events(p)
        assert incremental == cold, f"round {i}: incremental events != cold parse"


# --- AC-5: per-poll work after append >=10x cheaper on a large transcript ----

def test_large_transcript_incremental_is_10x_cheaper(tmp_path, monkeypatch):
    p = tmp_path / "big.jsonl"
    lines = [_line(10, inp=5, cr=1000, ts=f"2026-07-04T{i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z")
             for i in range(20000)]
    _write(p, lines)
    size = p.stat().st_size
    assert size > 3_000_000, f"fixture should be multi-MB, got {size}B"
    ct._sum_billable_tokens(p)                           # warm the cache

    lines.append(_line(9))
    _write(p, lines)
    _bump_mtime(p)
    counter = _ByteCounter(monkeypatch, p)
    ct._sum_billable_tokens(p)

    assert counter.bytes_read <= size / 10, (
        f"incremental poll read {counter.bytes_read}B of a {size}B file -- "
        "expected <=10% (appended tail only)"
    )
