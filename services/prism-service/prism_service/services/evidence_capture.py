"""Pure capture logic for attaching real, observable evidence to a gate.

RED STUB: signatures are pinned so the tests COLLECT and FAIL on behaviour
(pytest rc==1), not on an ImportError (rc==2). The trusted red seat requires
a demonstrated behavioural red; the implementation lands in the next commit.
"""
from __future__ import annotations

from typing import Optional


def capture_walkthrough(
    url: str,
    out_dir: str,
    selector: Optional[str] = None,
    video: bool = True,
    now=None,
) -> dict:
    raise NotImplementedError("capture_walkthrough: red stub")


def assertion_source_for(node_id: str, workspace: str) -> str:
    raise NotImplementedError("assertion_source_for: red stub")


def provenance(build_version: str, tree_sha: str, now=None) -> dict:
    raise NotImplementedError("provenance: red stub")
