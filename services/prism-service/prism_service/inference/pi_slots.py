"""Schema-validated model slot-fill with bounded retry (task 6d36ed99,
C2 of the PI-orchestration build — parent 81b23574 FR-2).

The drive engine never hands the small local model a whole document to
author. Every inference on the drive path is a SMALL SLOT — title,
oracle line, summary paragraph, FR/AC line, one-line verdict — with:

  * a tiny focused prompt (no long rubric for a qwen3-class model to
    drop on the floor — risk R1, mx-239c13);
  * a shape SCHEMA (single-line / word-count / char-cap) the returned
    fill must validate against;
  * BOUNDED retry (``retries`` per call, default PRISM_SLOT_RETRIES=2):
    a malformed/empty fill is rejected and re-prompted;
  * a DETERMINISTIC FALLBACK derived from the request context on
    exhaustion — a drive NEVER hard-stalls on a slot and a bad value is
    NEVER emitted into a plan. Specs without a fallback resolve to a
    typed failure result (ok=False) instead of raising.

The model seam is injectable (any ``callable(prompt, system) -> str``)
so unit tests run entirely on stubs; the default seam is the local
micro model via inference/local_llm.complete with purpose='slot-fill'
so ledger rows attribute slot burn (parent FR-4).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Union


def default_retries() -> int:
    """Bounded-retry budget: re-prompts allowed AFTER the first attempt."""
    try:
        return max(0, int(os.environ.get("PRISM_SLOT_RETRIES", "2")))
    except ValueError:
        return 2


# ----------------------------------------------------------------------
# Slot contract
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class SlotSpec:
    """One small model slot: prompt + shape schema + deterministic
    fallback. ``schema`` keys (all optional): single_line, min_words,
    max_words, min_chars, max_chars."""

    name: str
    prompt: str
    schema: dict = field(default_factory=dict)
    fallback: Optional[Callable[[dict], str]] = None


@dataclass
class SlotResult:
    """Outcome of one fill_slot call. ``ok`` is True only for a value
    that is schema-valid (model-authored or deterministic fallback);
    ``fallback_used`` marks exhaustion recovery and ``error`` preserves
    the last validation failure either way."""

    slot: str
    ok: bool
    value: str = ""
    attempts: int = 0
    fallback_used: bool = False
    error: str = ""


# ----------------------------------------------------------------------
# Lenient extraction — the reflection-parser lesson: models wrap JSON in
# think-tags, code fences, and prose; find the value anyway.
# ----------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*(.*?)```", re.DOTALL)
_JSON_OBJ_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def extract_value(raw: str) -> str:
    """Pull the candidate slot value out of a raw model output.

    Accepts ``{"value": ...}`` JSON anywhere (fences / think-tags /
    prose-before-JSON tolerated); otherwise the cleaned plain text is
    the candidate. Never raises."""
    text = _THINK_RE.sub("", raw or "")
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1)
    for m in _JSON_OBJ_RE.finditer(text):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        if isinstance(obj, dict) and "value" in obj:
            return str(obj["value"]).strip()
    return text.strip()


def validate(value: str, schema: dict) -> str:
    """Return '' when ``value`` satisfies ``schema``, else the reason."""
    if not (value or "").strip():
        return "empty value"
    v = value.strip()
    if schema.get("single_line") and len(v.splitlines()) > 1:
        return "expected a single line"
    words = len(v.split())
    if schema.get("min_words") and words < int(schema["min_words"]):
        return f"too short: {words} word(s) < min {schema['min_words']}"
    if schema.get("max_words") and words > int(schema["max_words"]):
        return f"too long: {words} word(s) > max {schema['max_words']}"
    if schema.get("min_chars") and len(v) < int(schema["min_chars"]):
        return f"too short: {len(v)} chars < min {schema['min_chars']}"
    if schema.get("max_chars") and len(v) > int(schema["max_chars"]):
        return f"too long: {len(v)} chars > max {schema['max_chars']}"
    return ""


# ----------------------------------------------------------------------
# Deterministic fallbacks — pure functions of the request context
# ----------------------------------------------------------------------

def _ask(context: dict) -> str:
    return str((context or {}).get("feature_ask")
               or (context or {}).get("title") or "").strip()


def _fallback_title(context: dict) -> str:
    words = [w for w in re.sub(r"[^\w\s-]", "", _ask(context)).split() if w]
    words = words or ["Untitled", "feature"]
    out = words[:9]
    while len(out) < 4:
        out.append(("task", "drive", "slice", "work")[len(out) % 4])
    return " ".join(out[0:1] + [w.lower() for w in out[1:]]).capitalize()


def _fallback_oracle(context: dict) -> str:
    return ("Targeted pytest for this slice passes and the feature is "
            "observable on the running service")


def _fallback_summary(context: dict) -> str:
    ask = _ask(context) or "the requested feature"
    return (f"Deliver {ask} as a small, verifiable slice: implement the "
            "change behind the existing seams, keep the surface minimal, "
            "and prove the outcome with a targeted test.")


def _fallback_fr_line(context: dict) -> str:
    ask = _ask(context) or "the requested behavior"
    return f"The system implements {ask} behind existing seams"


def _fallback_ac_line(context: dict) -> str:
    ask = _ask(context) or "the requested behavior"
    return f"Given the feature is built, {ask} is observable and verified"


def _fallback_verdict(context: dict) -> str:
    return "unverified: slot model produced no valid verdict"


# ----------------------------------------------------------------------
# Registry — the drive-path slots (parent FR-2)
# ----------------------------------------------------------------------

_JSON_HINT = ' Respond with JSON: {"value": "<text>"}.'

SLOTS: dict[str, SlotSpec] = {
    "title": SlotSpec(
        name="title",
        prompt=("Write a plain-language feature title, 4-9 words, for: "
                "{feature_ask}." + _JSON_HINT),
        schema={"single_line": True, "min_words": 4, "max_words": 9,
                "max_chars": 90},
        fallback=_fallback_title,
    ),
    "oracle": SlotSpec(
        name="oracle",
        prompt=("Write ONE line naming the observable check that proves "
                "this outcome: {feature_ask}." + _JSON_HINT),
        schema={"single_line": True, "min_words": 3, "max_chars": 200},
        fallback=_fallback_oracle,
    ),
    "summary": SlotSpec(
        name="summary",
        prompt=("Write one short paragraph (2-4 sentences) summarizing the "
                "change: {feature_ask}." + _JSON_HINT),
        schema={"min_chars": 40, "max_chars": 1200},
        fallback=_fallback_summary,
    ),
    "fr_line": SlotSpec(
        name="fr_line",
        prompt=("Write ONE functional-requirement line (no id prefix) for: "
                "{feature_ask}." + _JSON_HINT),
        schema={"single_line": True, "min_words": 4, "max_chars": 300},
        fallback=_fallback_fr_line,
    ),
    "ac_line": SlotSpec(
        name="ac_line",
        prompt=("Write ONE acceptance-criterion line (no id prefix, no "
                "oracle suffix) for: {feature_ask}." + _JSON_HINT),
        schema={"single_line": True, "min_words": 4, "max_chars": 300},
        fallback=_fallback_ac_line,
    ),
    "verdict": SlotSpec(
        name="verdict",
        prompt=("In ONE line: does the evidence satisfy the check? Evidence: "
                "{evidence}. Check: {check}." + _JSON_HINT),
        schema={"single_line": True, "max_chars": 300},
        fallback=_fallback_verdict,
    ),
}


# ----------------------------------------------------------------------
# Model seam + fill loop
# ----------------------------------------------------------------------

def _default_model(prompt: str, system: str = "") -> str:
    """Default seam: one blocking local-micro-model completion, ledgered
    as purpose='slot-fill' (parent FR-4). Import deferred so unit tests
    on stub models never touch inference."""
    from prism_service.inference import local_llm

    res = local_llm.complete(
        prompt, system=system, json_mode=True, max_tokens=256,
        purpose="slot-fill",
    )
    return str((res or {}).get("text") or "")


_SYSTEM = ("You fill ONE small text slot. Return only the requested JSON, "
           "no commentary.")


def fill_slot(
    slot: Union[str, SlotSpec],
    context: Optional[dict] = None,
    *,
    model: Optional[Callable[..., str]] = None,
    retries: Optional[int] = None,
) -> SlotResult:
    """Fill one slot with bounded retry. Never raises into the drive
    loop: exhaustion resolves to the spec's deterministic fallback
    (ok=True, fallback_used=True, error preserved) or, without a
    fallback, a typed failure (ok=False, value='')."""
    spec = SLOTS[slot] if isinstance(slot, str) else slot
    ctx = context or {}
    call = model or _default_model
    budget = default_retries() if retries is None else max(0, int(retries))

    # Substitute only bare {identifier} placeholders so the JSON hint's
    # literal braces survive; unbound fields render as <name>.
    prompt = re.sub(
        r"\{([a-z_][a-z0-9_]*)\}",
        lambda m: str(ctx[m.group(1)]) if m.group(1) in ctx else f"<{m.group(1)}>",
        spec.prompt,
    )

    attempts = 0
    last_err = "no attempt made"
    for attempt in range(budget + 1):
        attempts = attempt + 1
        try:
            raw = call(prompt, _SYSTEM)
        except Exception as exc:
            last_err = f"model call failed: {type(exc).__name__}: {exc}"
            continue
        value = extract_value(str(raw or ""))
        err = validate(value, spec.schema)
        if not err:
            return SlotResult(slot=spec.name, ok=True, value=value,
                              attempts=attempts)
        last_err = f"attempt {attempts}: {err}"

    if spec.fallback is not None:
        fb = spec.fallback(ctx)
        return SlotResult(
            slot=spec.name, ok=True, value=fb, attempts=attempts,
            fallback_used=True,
            error=f"exhausted {attempts} attempt(s); {last_err}; "
                  "deterministic fallback used",
        )
    return SlotResult(
        slot=spec.name, ok=False, value="", attempts=attempts,
        error=f"exhausted {attempts} attempt(s); {last_err}; "
              "no fallback for this slot",
    )
