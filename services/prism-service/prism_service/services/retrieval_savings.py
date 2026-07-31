"""Honest per-retrieval "tokens saved" estimate.

The model: baseline (what you would have read otherwise, in full) minus
what the retrieval actually handed you. PRISM returns precise spans, so
the alternative it displaces is opening the covered files whole.

Every rule here exists to stop the estimate flattering us:

  * a file we cannot stat contributes NOTHING to the baseline, so a
    partially-resolvable result set yields a SMALLER claim, never a
    wrong one;
  * when not one covered file could be sized, there is no estimate at
    all (``None``), rather than a fabricated zero;
  * when the payload is not actually smaller than the files it covers
    (tiny files, where pointers cost more than the source), we claim
    NOTHING -- that retrieval genuinely saved you nothing;
  * the result is labelled an estimate, because it is one: it assumes
    the reader would otherwise have opened each covered file in full.

Deliberately NOT included: any instruction telling the calling agent to
report these numbers to the user. Tool output is not an advertising
channel; the number is here for whoever wants to read it.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

# Rough chars-per-token. Good enough for an estimate, and deliberately
# not model-specific -- a precise-looking number here would imply a
# precision the whole approach does not have.
_CHARS_PER_TOKEN = 4


def to_tokens(chars: int) -> int:
    """Rough token count for a character length."""
    return round(chars / _CHARS_PER_TOKEN)


def baseline_for(
    paths: Iterable[str],
    root: Optional[str] = None,
) -> Optional[dict[str, int]]:
    """Whole-file baseline for the DISTINCT ``paths``, sized from disk.

    Returns ``{"files": n, "baseline_chars": c}``, or ``None`` when not a
    single path could be sized -- the caller then omits the estimate
    instead of claiming a bogus one. Unreadable or missing files are
    skipped, which can only shrink the claim.
    """
    total = 0
    files = 0
    for rel in dict.fromkeys(p for p in paths if p):
        full = os.path.join(root, rel) if root else rel
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        total += size
        files += 1
    return {"files": files, "baseline_chars": total} if files else None


def savings_for(
    payload_chars: int,
    baseline: Optional[dict[str, int]],
) -> Optional[dict[str, Any]]:
    """The savings claim for one retrieval, or ``None`` to stay silent.

    ``None`` (claim nothing) when there is no baseline, when the baseline
    is empty, or when the payload is no smaller than the files it covers.
    Those are the cases where a number would be dishonest rather than
    merely imprecise.
    """
    if not baseline:
        return None
    baseline_chars = baseline.get("baseline_chars", 0)
    if baseline_chars <= 0:
        return None
    baseline_tokens = to_tokens(baseline_chars)
    payload_tokens = to_tokens(payload_chars)
    if baseline_tokens <= payload_tokens:
        return None
    saved = baseline_tokens - payload_tokens
    return {
        "tokens_saved": saved,
        "percent": round(saved / baseline_tokens * 100),
        "payload_tokens": payload_tokens,
        "baseline_tokens": baseline_tokens,
        "files_covered": baseline.get("files", 0),
        "basis": (
            "estimate: assumes reading each covered file in full "
            "(~4 chars/token)"
        ),
    }
