"""python -m prism_service.ontology.vocab [--write|--check] (task 31b737fb).

Regenerates ontology/vocab.json from the enums declared in code -- never a
hand-kept literal list (ontology-SKILL.md "Adding to the model / A new
vocabulary value": add it to the enum, vocab.json regenerates, and the
Conway scan starts guarding the new word). --write regenerates the file;
--check exits 1 when the file on disk has drifted from what the enums
currently produce, so a CI/pre-commit hook can catch a hand-edit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_VOCAB_JSON = Path(__file__).resolve().parent / "vocab.json"


def build_vocab() -> dict[str, list[str]]:
    """The seven vocabularies -- channel/ask/bucket/signal_state read off
    signal_parse's own str Enums (the single source vocab.py and the
    resolver both use), the rest off ontology_terms.py's declared tuples
    and models.workflow.WORKFLOWS."""
    from prism_service.models.workflow import WORKFLOWS
    from prism_service.services.ontology_terms import (
        GATE_STATES, PROOF_TYPES, TASK_STATUSES,
    )
    from prism_service.services.signal_parse import (
        AskKind, Bucket, Channel, SignalState,
    )

    return {
        "channel": sorted(c.value for c in Channel if c.value),
        "ask": [k.value for k in AskKind],
        "bucket": [b.value for b in Bucket],
        "signal_state": [s.value for s in SignalState],
        "task_status": list(TASK_STATUSES),
        "workflow": sorted(WORKFLOWS.keys()),
        "proof_type": list(PROOF_TYPES),
        "gate_state": list(GATE_STATES),
    }


def _rendered(vocab: dict[str, list[str]]) -> str:
    return json.dumps(vocab, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true",
                        help="regenerate vocab.json from the enums")
    group.add_argument("--check", action="store_true",
                        help="exit 1 if vocab.json is stale")
    args = parser.parse_args(argv)

    text = _rendered(build_vocab())

    if args.write:
        _VOCAB_JSON.write_text(text, encoding="utf-8")
        print(f"wrote {_VOCAB_JSON}")
        return 0

    if not _VOCAB_JSON.exists():
        print(f"{_VOCAB_JSON} does not exist -- run --write", file=sys.stderr)
        return 1
    current = _VOCAB_JSON.read_text(encoding="utf-8")
    if current != text:
        print(f"{_VOCAB_JSON} is stale -- run --write", file=sys.stderr)
        return 1
    print("vocab.json is up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
