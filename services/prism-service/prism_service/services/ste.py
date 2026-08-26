"""Deterministic Simplified Technical English (STE) normaliser.

This module cleans up free-text task fields before PRISM stores them.
It never calls a model. Every rule is a plain regex, so the same input
always produces the same output.

The module has two halves.

``normalize`` applies SAFE, MEANING-PRESERVING fixes only: it expands
contractions, turns a clause-joining semicolon into a sentence break,
collapses runs of spaces, and swaps a curated set of filler phrases,
nominalisations, phrasal verbs, and marketing adjectives for plainer
text. It never touches a fenced code block, an inline code span, a
URL, a file path, a task or memory id, a quoted string, a markdown
link target, or a hedge word (may, might, could, sometimes, possibly,
likely). These spans are PROTECTED. A rewrite that changed meaning
would be worse than no rewrite, so the safe list stays small on
purpose.

``check`` finds problems a regex must not fix on its own: long
sentences, passive voice, present-perfect tense, stacked hedges, an
imperative sentence that chains two instructions with "then", and an
overlong paragraph. It also reports when a safe-fix pattern (a
semicolon, a filler phrase, a nominalisation, a phrasal verb, or a
marketing word) sits inside a protected span, since normalize skipped
it on purpose and a person should still see it.

``check`` does NOT look at noun clusters (strings of nouns stacked in
front of each other, such as "task queue depth alert threshold"). That
check needs a part-of-speech tagger this module does not have.

Two modes shape the thresholds:

- ``strict`` — for oracle, stop_if, and likely_misfire. These are
  instructions a machine or a person must follow exactly. Sentences
  cap at 20 words, and the multi-instruction check is active.
- ``flavored`` — for title, description, completion_proof, and
  premise_notes. These read as prose. Sentences cap at 25 words.

``apply`` runs normalize and then check on the result, so a caller
gets the fixed text and the findings that remain after the safe fixes
ran.

``style_block`` packs a per-field report ({rules applied}, {findings})
into the shape a UI or an API response can render directly.
"""

from __future__ import annotations

import inspect
import logging
import re
from dataclasses import dataclass

MODES = ("strict", "flavored")

# ----------------------------------------------------------------------
# Coverage hook (task c7edf4e2, epic cc9a44c8 — "every text writer
# registers with Align language and cannot drift"). A listener registers
# with on_apply(callback); ste calls callback(mode, frames) every time it
# actually normalises text, so a listener such as
# services.language_alignment can prove which code paths run STE and
# which do not.
#
# The listener fires from normalize() -- not, as the name might suggest,
# only inside apply() -- because normalize() is the ONE function every
# real write path in this codebase calls (TaskService._apply_ste calls it
# once per field; MemoryService.store calls it once for description).
# apply() itself calls normalize() as its first step, so apply() still
# "invokes every listener" exactly as documented -- it just does so by
# way of the same shared code path, instead of duplicating the call and
# double-counting when a caller (a test, or the align-language dry-run
# preview) uses apply() or normalize() directly.
# ----------------------------------------------------------------------

_LISTENERS: list = []


def on_apply(callback) -> None:
    """Register ``callback(mode, frames)`` to run after ste normalises a
    piece of text. Not idempotent by id -- call once, e.g. at import time,
    the way services.language_alignment does. A listener that raises is
    caught and logged; it can never break a text write."""
    _LISTENERS.append(callback)


def _caller_frames(limit: int = 12) -> list[tuple[str, str, str, str]]:
    """A short list of (module, function, tool_hint, project_hint) tuples
    for the frames that called INTO ste, closest caller first. ste's own
    frames are skipped, so a listener never sees "normalize"/"apply".

    ``tool_hint`` is the frame's own local variable named ``name`` when it
    holds a plain string -- this is how a caller can tell apart several
    call shapes dispatched through ONE function (e.g. the MCP tool
    dispatcher, which branches on ``if name == "task_create":`` rather
    than calling a separate function per tool) without ste knowing
    anything about MCP.

    ``project_hint`` is the frame's own local variable named ``project``
    or ``project_id`` when it holds a plain string, else the ``.project``
    attribute of a local named ``self`` (a TaskService instance carries
    one directly), else the ``.project`` attribute of ``self._task_svc``
    (a MemoryService instance carries a TaskService there). Best-effort --
    a frame that carries none of these leaves it empty."""
    frames: list[tuple[str, str, str, str]] = []
    try:
        stack = inspect.stack()
    except Exception:
        return frames
    try:
        for record in stack:
            module = record.frame.f_globals.get("__name__", "")
            if module == __name__:
                continue
            function = record.function
            local_vars = record.frame.f_locals
            tool_hint = ""
            try:
                raw = local_vars.get("name")
                if isinstance(raw, str):
                    tool_hint = raw
            except Exception:
                pass
            project_hint = _project_hint_from_locals(local_vars)
            frames.append((module, function, tool_hint, project_hint))
            if len(frames) >= limit:
                break
    finally:
        del stack
    return frames


def _project_hint_from_locals(local_vars: dict) -> str:
    for key in ("project", "project_id"):
        try:
            raw = local_vars.get(key)
        except Exception:
            raw = None
        if isinstance(raw, str) and raw:
            return raw
    try:
        owner = local_vars.get("self")
    except Exception:
        owner = None
    if owner is not None:
        project = getattr(owner, "project", None)
        if isinstance(project, str) and project:
            return project
        project_id = getattr(owner, "project_id", None)
        if isinstance(project_id, str) and project_id:
            return project_id
        task_svc = getattr(owner, "_task_svc", None)
        project = getattr(task_svc, "project", None) if task_svc else None
        if isinstance(project, str) and project:
            return project
    return ""


def _notify_listeners(mode: str) -> None:
    if not _LISTENERS:
        return
    frames = _caller_frames()
    for callback in list(_LISTENERS):
        try:
            callback(mode, frames)
        except Exception:
            logging.getLogger(__name__).warning(
                "an ste.on_apply listener raised; the write proceeds "
                "unaffected.", exc_info=True)


_bootstrapped = False


def _ensure_coverage_listener_registered() -> None:
    """Import services.language_alignment once, purely for its import-time
    ``on_apply(...)`` side effect (task c7edf4e2). Lazy and local for the
    same reason the lexicon import inside apply() below is local: a
    top-level import here would be circular (language_alignment imports
    ste). ste does not need language_alignment for its own logic -- this
    bootstrap exists only so the coverage listener is registered before
    the FIRST real write, without requiring every caller of normalize()
    to import language_alignment itself first."""
    global _bootstrapped
    if _bootstrapped:
        return
    _bootstrapped = True
    try:
        from prism_service.services import language_alignment  # noqa: F401
    except Exception:
        logging.getLogger(__name__).warning(
            "could not register the align-language coverage listener",
            exc_info=True)


@dataclass(frozen=True)
class Finding:
    """One thing check() found. ``start``/``end`` are byte offsets into
    the text that was checked."""

    rule: str
    message: str
    start: int
    end: int
    excerpt: str


# ----------------------------------------------------------------------
# Substitution tables. Each entry is (phrase, replacement) unless a
# third element forces the replacement's case (used for "I'm" -> "I
# am", where "I" stays capital no matter how the source was written).
# ----------------------------------------------------------------------

_CONTRACTIONS = [
    ("don't", "do not", False),
    ("can't", "cannot", False),
    ("won't", "will not", False),
    ("it's", "it is", False),
    ("isn't", "is not", False),
    ("aren't", "are not", False),
    ("didn't", "did not", False),
    ("doesn't", "does not", False),
    ("wouldn't", "would not", False),
    ("shouldn't", "should not", False),
    ("couldn't", "could not", False),
    ("we're", "we are", False),
    ("they're", "they are", False),
    ("you're", "you are", False),
    ("i'm", "I am", True),
    ("let's", "let us", False),
]

_FILLER = [
    ("in order to", "to"),
    ("prior to", "before"),
    ("subsequent to", "after"),
    ("in the event that", "if"),
    ("due to the fact that", "because"),
    ("at this point in time", "now"),
    ("make use of", "use"),
    ("utilize", "use"),
    ("a number of", "several"),
]

_NOMINALISATION = [
    ("perform an analysis of", "analyze"),
    ("provide assistance to", "help"),
    ("carry out an inspection of", "inspect"),
    ("make a decision", "decide"),
]

_PHRASAL_VERB = [
    ("spin up", "start"),
    ("kick off", "start"),
    ("reach out to", "contact"),
    ("dive into", "read"),
    ("figure out", "determine"),
]

_MARKETING_WORDS = [
    "seamlessly",
    "seamless",
    "effortlessly",
    "effortless",
    "robust",
    "powerful",
    "cutting-edge",
    "blazing-fast",
]

_HEDGE_WORDS = ("may", "might", "could", "sometimes", "possibly", "likely")

_ABBREVIATIONS = {"e.g", "i.e", "etc", "vs"}

_PARTICIPLE_IRREGULAR = {
    "done", "known", "called", "named", "set", "used", "given", "written",
    "born", "held", "made", "taken", "seen", "shown", "built", "sent",
    "kept", "left", "broken", "chosen", "driven", "eaten", "forgotten",
    "hidden", "ridden", "spoken", "stolen", "thrown", "worn", "put",
    "read", "run", "found", "brought", "bought", "caught", "taught",
    "thought", "meant", "paid", "said", "sold", "told", "understood",
    "lost", "won", "begun", "drawn", "grown", "fallen", "gone",
}

_PASSIVE_BE_VERBS = ("is", "are", "was", "were", "been", "being", "be")

# Sentence punctuation that can sit glued to the end of a URL or a path
# token. It is never part of the protected span.
_TRAILING_PUNCT = ".,;:!?)]}\"'"

_PASSIVE_ALLOWLIST = {
    "is done", "is required", "are known", "is called", "is named",
    "is set", "is used", "are used",
}


# ----------------------------------------------------------------------
# Protected spans — never rewritten by normalize.
# ----------------------------------------------------------------------


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Find every span that a rewrite must leave alone: fenced code,
    inline code, URLs, markdown link targets, quoted strings, task and
    memory ids, and file-path-shaped tokens. Returns merged, sorted,
    non-overlapping spans."""
    spans: list[tuple[int, int]] = []

    for m in re.finditer(r"```[\s\S]*?```", text):
        spans.append((m.start(), m.end()))
    for m in re.finditer(r"`[^`\n]*`", text):
        spans.append((m.start(), m.end()))
    for m in re.finditer(r"https?://\S+", text):
        # Punctuation glued to the end of a URL belongs to the sentence,
        # not to the URL: "see https://x/y; then" keeps its semicolon
        # rewritable (found live on 7.13.72, epic b2acfa16).
        spans.append((m.start(), m.start() + len(m.group(0).rstrip(_TRAILING_PUNCT))))
    for m in re.finditer(r"\[[^\]\n]*\]\(([^)\n]*)\)", text):
        spans.append(m.span(1))
    for m in re.finditer(r'"[^"\n]{0,200}"', text):
        spans.append((m.start(), m.end()))
    # Non-greedy content that may itself hold an apostrophe (a contraction
    # inside the quoted string, e.g. 'don't do that') — the lookaround
    # picks the real closing quote by requiring it not be glued to a
    # following word character, so a mid-word contraction apostrophe is
    # skipped as a candidate close and the search keeps extending.
    for m in re.finditer(r"(?<!\w)'([^\n]{0,200}?)'(?!\w)", text):
        spans.append((m.start(), m.end()))
    for m in re.finditer(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
        text,
    ):
        spans.append((m.start(), m.end()))
    for m in re.finditer(r"\bmx-[0-9a-fA-F]+\b", text, re.IGNORECASE):
        spans.append((m.start(), m.end()))
    for m in re.finditer(r"\b[0-9a-fA-F]{8}\b", text):
        spans.append((m.start(), m.end()))
    for m in re.finditer(r"\S+", text):
        # Same rule for a path: "Features/*/Endpoints.cs;" protects the
        # path and leaves the semicolon to the sentence rules.
        tok = m.group(0).rstrip(_TRAILING_PUNCT)
        if tok and ("/" in tok or re.search(r"\.[A-Za-z][A-Za-z0-9]{0,5}$", tok)):
            spans.append((m.start(), m.start() + len(tok)))

    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    ordered = sorted(spans)
    merged = [list(ordered[0])]
    for s, e in ordered[1:]:
        if s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _in_protected(pos: int, protected: list[tuple[int, int]]) -> bool:
    for s, e in protected:
        if s <= pos < e:
            return True
        if s > pos:
            break
    return False


def _subtract_protected(
    start: int, end: int, protected: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """The parts of [start, end) that are NOT inside a protected span."""
    ranges: list[tuple[int, int]] = []
    cursor = start
    for ps, pe in protected:
        if pe <= start or ps >= end:
            continue
        cs, ce = max(ps, start), min(pe, end)
        if cs > cursor:
            ranges.append((cursor, cs))
        cursor = max(cursor, ce)
    if cursor < end:
        ranges.append((cursor, end))
    return ranges


def _segments(
    text: str, protected: list[tuple[int, int]]
) -> list[tuple[bool, str]]:
    """The whole text as alternating (is_protected, chunk) pieces, in
    order, covering every character exactly once."""
    out: list[tuple[bool, str]] = []
    cursor = 0
    for s, e in protected:
        if s > cursor:
            out.append((False, text[cursor:s]))
        out.append((True, text[s:e]))
        cursor = e
    if cursor < len(text):
        out.append((False, text[cursor:]))
    return out


# ----------------------------------------------------------------------
# Safe rewrites (normalize)
# ----------------------------------------------------------------------


def _apply_table(text: str, table, rule_name: str) -> tuple[str, bool]:
    changed = False
    for entry in table:
        if len(entry) == 3:
            phrase, repl, force_case = entry
        else:
            phrase, repl = entry
            force_case = False
        pattern = re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE)

        def _repl(m, repl=repl, force_case=force_case):
            nonlocal changed
            changed = True
            if force_case:
                return repl
            if m.group(0)[:1].isupper():
                return repl[:1].upper() + repl[1:]
            return repl

        text = pattern.sub(_repl, text)
    return text, changed


def _apply_semicolon(text: str) -> tuple[str, bool]:
    changed = False

    def _repl(m):
        nonlocal changed
        changed = True
        return ". " + m.group(1).upper()

    new_text = re.sub(r";\s*([a-zA-Z])", _repl, text)
    return new_text, changed


def _apply_marketing(text: str) -> tuple[str, bool]:
    changed = False
    for word in _MARKETING_WORDS:
        pattern = re.compile(r"\s*\b" + re.escape(word) + r"\b\s*", re.IGNORECASE)
        new_text, n = pattern.subn(" ", text)
        if n:
            changed = True
            text = new_text
    if changed:
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r" +([.,;:!?])", r"\1", text)
        text = re.sub(
            r"\b(a|an)\s+([.,;:!?]|$)", r"\2", text,
            flags=re.MULTILINE | re.IGNORECASE,
        )
    return text, changed


def _normalize_segment(text: str) -> tuple[str, list[str]]:
    applied: list[str] = []

    text, c = _apply_table(text, _CONTRACTIONS, "contraction")
    if c:
        applied.append("contraction")

    text, c = _apply_semicolon(text)
    if c:
        applied.append("semicolon")

    text, c = _apply_table(text, _FILLER, "filler")
    if c:
        applied.append("filler")

    text, c = _apply_table(text, _NOMINALISATION, "nominalisation")
    if c:
        applied.append("nominalisation")

    text, c = _apply_table(text, _PHRASAL_VERB, "phrasal-verb")
    if c:
        applied.append("phrasal-verb")

    text, c = _apply_marketing(text)
    if c:
        applied.append("marketing")

    new_text = re.sub(r"[ \t]+", " ", text)
    if new_text != text:
        applied.append("whitespace")
    text = new_text

    return text, applied


def normalize(text: str, mode: str = "flavored") -> tuple[str, list[str]]:
    """Apply only safe, meaning-preserving fixes to ``text``.

    Returns the fixed text and the distinct rule names that changed
    something, in the order each rule first fired. Protected spans
    (code, URLs, quotes, ids, file paths, markdown link targets) are
    copied through byte-for-byte.
    """
    if mode not in MODES:
        raise ValueError(f"unknown STE mode: {mode!r}")
    if not text:
        return text, []

    protected = _protected_spans(text)
    segments = _segments(text, protected)

    out_parts: list[str] = []
    applied_order: list[str] = []
    for is_protected, chunk in segments:
        if is_protected:
            out_parts.append(chunk)
            continue
        fixed_chunk, applied = _normalize_segment(chunk)
        out_parts.append(fixed_chunk)
        for rule in applied:
            if rule not in applied_order:
                applied_order.append(rule)

    _ensure_coverage_listener_registered()
    _notify_listeners(mode)
    return "".join(out_parts), applied_order


# ----------------------------------------------------------------------
# Sentence and paragraph splitting (check)
# ----------------------------------------------------------------------


def _preceding_token(text: str, pos: int) -> str:
    j = pos
    while j > 0 and not text[j - 1].isspace():
        j -= 1
    return text[j:pos]


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    s, e = start, end
    while s < e and text[s].isspace():
        s += 1
    while e > s and text[e - 1].isspace():
        e -= 1
    return s, e


def _split_sentences(
    text: str, protected: list[tuple[int, int]]
) -> list[tuple[str, int, int]]:
    """Split text into sentences. A period does not end a sentence when
    it sits inside a protected span, when it closes a known
    abbreviation (e.g., i.e., etc., vs.), when no whitespace follows it
    (a decimal or a dotted identifier such as api.tasks.py or
    v7.13.70), or when the next real character is lowercase."""
    n = len(text)
    boundaries: list[int] = []

    for m in re.finditer(r"[.!?]", text):
        pos = m.start()
        if _in_protected(pos, protected):
            continue
        nxt = pos + 1
        if nxt < n and not text[nxt].isspace():
            continue
        if text[pos] == "." and _preceding_token(text, pos).lower() in _ABBREVIATIONS:
            continue
        if nxt < n:
            j = nxt
            while j < n and text[j].isspace():
                j += 1
            if j < n and not (text[j].isupper() or text[j] in "\"'([{"):
                continue
        boundaries.append(pos + 1)

    sentences: list[tuple[str, int, int]] = []
    start = 0
    for end in boundaries:
        s, e = _trim_span(text, start, end)
        if e > s:
            sentences.append((text[s:e], s, e))
        start = end
    s, e = _trim_span(text, start, n)
    if e > s:
        sentences.append((text[s:e], s, e))
    return sentences


def _split_paragraphs(text: str) -> list[tuple[int, int]]:
    paragraphs: list[tuple[int, int]] = []
    start = 0
    for m in re.finditer(r"\n\s*\n", text):
        if m.start() > start:
            paragraphs.append((start, m.start()))
        start = m.end()
    if start < len(text):
        paragraphs.append((start, len(text)))
    return paragraphs


def _count_words(
    text: str, start: int, end: int, protected: list[tuple[int, int]]
) -> int:
    """Word count for [start, end). A protected span counts as exactly
    one word, no matter how many words it holds."""
    pieces: list[str] = []
    cursor = start
    for ps, pe in protected:
        if pe <= start:
            continue
        if ps >= end:
            break
        cs, ce = max(ps, start), min(pe, end)
        if cs > cursor:
            pieces.append(text[cursor:cs])
        pieces.append(" X ")
        cursor = max(cursor, ce)
    if cursor < end:
        pieces.append(text[cursor:end])
    return len("".join(pieces).split())


def _is_participle(word: str) -> bool:
    w = word.lower()
    return w.endswith("ed") or w.endswith("en") or w in _PARTICIPLE_IRREGULAR


def _find_passive(segment: str, base: int) -> list[tuple[int, int]]:
    hits: list[tuple[int, int]] = []
    for m in re.finditer(
        r"\b(" + "|".join(_PASSIVE_BE_VERBS) + r")\s+(\w+)\b",
        segment, re.IGNORECASE,
    ):
        be, word = m.group(1), m.group(2)
        if not _is_participle(word):
            continue
        if f"{be.lower()} {word.lower()}" in _PASSIVE_ALLOWLIST:
            continue
        hits.append((base + m.start(), base + m.end()))
    return hits


def _find_present_perfect(segment: str, base: int) -> list[tuple[int, int]]:
    hits: list[tuple[int, int]] = []
    for m in re.finditer(r"\b(has|have|had)\s+(\w+)\b", segment, re.IGNORECASE):
        if _is_participle(m.group(2)):
            hits.append((base + m.start(), base + m.end()))
    return hits


def _find_hedges(segment: str, base: int) -> list[tuple[int, int]]:
    hits: list[tuple[int, int]] = []
    for m in re.finditer(
        r"\b(" + "|".join(_HEDGE_WORDS) + r")\b", segment, re.IGNORECASE
    ):
        hits.append((base + m.start(), base + m.end()))
    return hits


# ----------------------------------------------------------------------
# check()
# ----------------------------------------------------------------------


def _finding(rule: str, message: str, start: int, end: int, text: str) -> Finding:
    return Finding(rule=rule, message=message, start=start, end=end,
                    excerpt=text[start:end])


def _check_protected_leftovers(
    text: str, protected: list[tuple[int, int]]
) -> list[Finding]:
    """A pattern normalize would fix, but that survived because it sat
    inside a protected span. Report it so a person can fix it by hand."""
    findings: list[Finding] = []
    for s, e in protected:
        chunk = text[s:e]
        for m in re.finditer(r";", chunk):
            findings.append(_finding(
                "semicolon",
                "A semicolon sits inside a protected span. The "
                "normaliser does not rewrite protected text. Fix it "
                "by hand.",
                s + m.start(), s + m.end(), text))
        for phrase, _repl in _FILLER:
            for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", chunk, re.IGNORECASE):
                findings.append(_finding(
                    "filler",
                    f"The filler phrase '{phrase}' sits inside a "
                    "protected span. Fix it by hand.",
                    s + m.start(), s + m.end(), text))
        for phrase, _repl in _NOMINALISATION:
            for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", chunk, re.IGNORECASE):
                findings.append(_finding(
                    "nominalisation",
                    f"The nominalisation '{phrase}' sits inside a "
                    "protected span. Fix it by hand.",
                    s + m.start(), s + m.end(), text))
        for phrase, _repl in _PHRASAL_VERB:
            for m in re.finditer(r"\b" + re.escape(phrase) + r"\b", chunk, re.IGNORECASE):
                findings.append(_finding(
                    "phrasal-verb",
                    f"The phrasal verb '{phrase}' sits inside a "
                    "protected span. Fix it by hand.",
                    s + m.start(), s + m.end(), text))
        for word in _MARKETING_WORDS:
            for m in re.finditer(r"\b" + re.escape(word) + r"\b", chunk, re.IGNORECASE):
                findings.append(_finding(
                    "marketing",
                    f"The marketing word '{word}' sits inside a "
                    "protected span. Fix it by hand.",
                    s + m.start(), s + m.end(), text))
    return findings


def _check_long_paragraphs(
    text: str,
    protected: list[tuple[int, int]],
    sentences: list[tuple[str, int, int]],
) -> list[Finding]:
    findings: list[Finding] = []
    for ps, pe in _split_paragraphs(text):
        count = sum(1 for _, s, e in sentences if s >= ps and e <= pe)
        if count > 6:
            ts, te = _trim_span(text, ps, pe)
            if te > ts:
                findings.append(_finding(
                    "long-paragraph",
                    f"This paragraph has {count} sentences. Keep a "
                    "paragraph to six sentences or fewer. Split it.",
                    ts, te, text))
    return findings


def check(text: str, mode: str = "flavored") -> list[Finding]:
    """Report problems normalize must not fix on its own: long
    sentences, passive voice, present-perfect tense, stacked hedges, a
    chained multi-instruction sentence (strict mode only), an overlong
    paragraph, and a safe-fix pattern that survived inside a protected
    span. Noun clusters are not checked — that needs a part-of-speech
    tagger this module does not have."""
    if mode not in MODES:
        raise ValueError(f"unknown STE mode: {mode!r}")
    if not text:
        return []

    protected = _protected_spans(text)
    findings: list[Finding] = []
    findings.extend(_check_protected_leftovers(text, protected))

    sentences = _split_sentences(text, protected)
    threshold = 20 if mode == "strict" else 25

    for sent_text, s, e in sentences:
        wc = _count_words(text, s, e, protected)
        if wc > threshold:
            findings.append(_finding(
                "sentence-length",
                f"This sentence has {wc} words. The limit is "
                f"{threshold} words in {mode} mode. Split it.",
                s, e, text))

        sub_ranges = _subtract_protected(s, e, protected)
        hedge_hits: list[tuple[int, int]] = []
        perfect_hits: list[tuple[int, int]] = []
        passive_hits: list[tuple[int, int]] = []
        for a, b in sub_ranges:
            seg = text[a:b]
            hedge_hits.extend(_find_hedges(seg, a))
            perfect_hits.extend(_find_present_perfect(seg, a))
            passive_hits.extend(_find_passive(seg, a))

        for hs, he in passive_hits:
            findings.append(_finding(
                "passive-voice",
                "This is passive voice. Name the actor and use an "
                "active verb.",
                hs, he, text))

        for hs, he in perfect_hits:
            if any(hh[0] < hs for hh in hedge_hits):
                continue
            findings.append(_finding(
                "present-perfect",
                "This is present-perfect tense. Use simple past or "
                "simple present.",
                hs, he, text))

        if len(hedge_hits) >= 2:
            findings.append(_finding(
                "hedge-stacking",
                "Two or more hedge words sit in one sentence. Pick "
                "one claim and state it plainly.",
                s, e, text))

        if mode == "strict":
            low = sent_text.lower()
            if ", then" in low or " and then" in low:
                findings.append(_finding(
                    "multi-instruction",
                    "This sentence chains two instructions with "
                    "'then'. Split it into two sentences.",
                    s, e, text))

    findings.extend(_check_long_paragraphs(text, protected, sentences))
    findings.sort(key=lambda f: (f.start, f.end))
    return findings


def apply(text: str, mode: str = "flavored") -> tuple[str, list[Finding]]:
    """Run normalize, then the lexicon aligner (task 2ee65e14 —
    services.lexicon.align swaps a synonym for the ontology's canonical
    term, e.g. "ticket" -> "Task"; owner decision 2026-08-26: the act is
    called ALIGN, not converge), then check the result. Returns the
    fully fixed text and the findings that remain.

    The lexicon import is local, not module-level: services.lexicon
    imports ste's own _protected_spans/_segments to skip code, URLs,
    and quoted text, so a top-level import here would be circular.

    Any listener registered with on_apply (task c7edf4e2) already ran by
    the time this returns -- normalize(), called below, is where the
    listeners actually fire, since normalize() is the function every real
    write path calls directly. apply() still "invokes every listener" as
    documented; it just does so through the same shared call, rather than
    firing a second time and double-counting a caller that already went
    through normalize() on its own.
    """
    fixed_text, _rules = normalize(text, mode=mode)
    from prism_service.services import lexicon
    aligned_text, _applied = lexicon.align(fixed_text)
    findings = check(aligned_text, mode=mode)
    return aligned_text, findings


def style_block(
    fields: dict[str, tuple[list[str], list[Finding]]]
    | dict[str, tuple[list[str], list[Finding], list[dict]]]
) -> dict:
    """Pack a per-field report into one dict a UI or an API response can
    render directly: {"fixed": {field: [rule, ...]}, "findings":
    [{field, rule, message, excerpt}, ...], "aligned": [{field, from,
    to}, ...]}. A field with no applied rules is left out of "fixed"; a
    field with no findings contributes nothing to "findings"; a field
    with no lexicon replacements contributes nothing to "aligned".

    Each field's value is a (rules, findings) pair, or a (rules,
    findings, aligned) triple when the caller also ran
    services.lexicon.align on that field (task 2ee65e14) — the 2-tuple
    form stays valid so an existing caller (MemoryService) does not
    have to change.
    """
    fixed: dict[str, list[str]] = {}
    findings_out: list[dict] = []
    aligned_out: list[dict] = []
    for field_name, value in fields.items():
        rules, findings = value[0], value[1]
        aligned = value[2] if len(value) > 2 else []
        if rules:
            fixed[field_name] = list(rules)
        for f in findings:
            findings_out.append({
                "field": field_name,
                "rule": f.rule,
                "message": f.message,
                "excerpt": f.excerpt,
            })
        for c in aligned:
            aligned_out.append({
                "field": field_name,
                "from": c["from"],
                "to": c["to"],
            })
    return {"fixed": fixed, "findings": findings_out, "aligned": aligned_out}
