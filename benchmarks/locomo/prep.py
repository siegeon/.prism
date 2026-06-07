"""Provision benchmarks/data/locomo.json from the upstream LoCoMo dataset.

Downloads snap-research/locomo `locomo10.json` and converts it to the schema
`benchmarks/locomo/run.py` expects: one entry per QA pair carrying its whole
conversation as the haystack; gold = the session(s) cited in `evidence`
(`D<n>:<turn>` -> `session_<n>`). LoCoMo category ints map to names; category 2
is the temporal split. Adversarial QAs (no gold session) are skipped.

Usage: python benchmarks/locomo/prep.py
[Source: https://github.com/snap-research/locomo (CC-BY-NC 4.0)]
"""
from __future__ import annotations

import json
import re
import urllib.request
from pathlib import Path

URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
CAT = {1: "multi_hop", 2: "temporal", 3: "open_domain", 4: "single_hop", 5: "adversarial"}
DATA = Path(__file__).resolve().parent.parent / "data" / "locomo.json"


def _sessions(conv: dict) -> tuple[list[str], list[list[dict]]]:
    ids: list[str] = []
    sess: list[list[dict]] = []
    n = 1
    while f"session_{n}" in conv:
        ids.append(f"session_{n}")
        sess.append([{"role": t.get("speaker", "user"), "content": t.get("text", "")}
                     for t in conv[f"session_{n}"]])
        n += 1
    return ids, sess


def _gold(evidence, sample_id: str) -> list[str]:
    out: list[str] = []
    for ev in evidence or []:
        m = re.match(r"D(\d+):", str(ev))
        if m:
            sid = f"{sample_id}_session_{m.group(1)}"
            if sid not in out:
                out.append(sid)
    return out


def convert(raw: list[dict]) -> list[dict]:
    entries: list[dict] = []
    for sample in raw:
        sid = sample.get("sample_id", "s")
        ids, sess = _sessions(sample.get("conversation", {}))
        hs_ids = [f"{sid}_{i}" for i in ids]
        for qi, qa in enumerate(sample.get("qa", [])):
            gold = _gold(qa.get("evidence"), sid)
            if not gold:  # adversarial / unanswerable — no gold session to retrieve
                continue
            entries.append({
                "question_id": f"{sid}_q{qi}",
                "question": qa.get("question", ""),
                "answer_session_ids": gold,
                "category": CAT.get(qa.get("category"), "other"),
                "haystack_session_ids": hs_ids,
                "haystack_sessions": sess,
            })
    return entries


def main() -> None:
    with urllib.request.urlopen(URL, timeout=180) as r:
        raw = json.load(r)
    entries = convert(raw)
    DATA.parent.mkdir(parents=True, exist_ok=True)
    with open(DATA, "w", encoding="utf-8") as f:
        json.dump(entries, f)
    temporal = sum(1 for e in entries if e["category"] == "temporal")
    print(f"wrote {len(entries)} entries ({temporal} temporal) -> {DATA}")


if __name__ == "__main__":
    main()
