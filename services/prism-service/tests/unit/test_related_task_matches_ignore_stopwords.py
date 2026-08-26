"""Related-task matches ignore stopwords (task 461b7985).

Found live 2026-08-25 on signal c7375d58: the related_tasks leg's title
overlap check counted stopwords ("can", "the", ...) as real overlap, so a
signal like 'Can you review the Work board channel filter before Friday?'
spuriously matched every task whose title also happened to start with
'Can you ...'. Fixed by filtering stopwords + short tokens before scoring,
weighting the remaining content tokens by idf over the board's task
titles, and requiring a real content-token overlap above a small score
threshold. channel_ref-equality and id-token matches are untouched.

Fixtures copied from test_signal_resolves_against_ontology.py.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent
if str(_SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVICE_ROOT))


@pytest.fixture
def project():
    return f"signal-resolve-{uuid.uuid4().hex[:8]}"


def _ctx(project: str):
    from prism_service.project_context import get_project
    return get_project(project)


def _make_signal(project: str, **kw):
    from prism_service.models.signal import Signal
    defaults = dict(project=project, channel="ui", subject="", body="",
                     sender="", channel_ref="")
    defaults.update(kw)
    return Signal(**defaults)


def test_stopword_only_overlap_no_longer_matches_noise_tasks(project):
    """The live regression: a 'Can you ...'-shaped board pollutes every
    stopword-only overlap. Only the task with real content-token overlap
    (board/channel/work) should match; the two 'Can ...' noise tasks,
    which share nothing but stopwords with the subject, must not."""
    from prism_service.services.signal_resolver import resolve

    ctx = _ctx(project)
    noise1 = ctx.task_svc.create(title="Can you approve the release", channel="ui")
    noise2 = ctx.task_svc.create(title="Can we ship on Friday", channel="ui")
    target = ctx.task_svc.create(title="The Work board filters by channel", channel="ui")
    ctx.task_svc.create(title="Investigate ticket rollout", channel="ui")

    signal = _make_signal(
        project, subject="Can you review the Work board channel filter?",
    )
    matches = resolve(project, signal)
    ids = {t["id"]: t for t in matches["related_tasks"]}

    assert target.id in ids
    assert noise1.id not in ids
    assert noise2.id not in ids

    why = ids[target.id]["why"]
    assert "channel" in why and "board" in why and "work" in why
    # no stopword ever named as a matched content token
    assert "can" not in why.split(":")[-1]


def test_id_like_token_in_subject_still_matches(project):
    from prism_service.services.signal_resolver import resolve

    ctx = _ctx(project)
    task = ctx.task_svc.create(title="Some unrelated title here", channel="ui")

    signal = _make_signal(project, subject=f"re: {task.id[:8]} status update")
    matches = resolve(project, signal)
    ids = {t["id"]: t for t in matches["related_tasks"]}

    assert task.id in ids
    assert "id-like token match" in ids[task.id]["why"]


def test_channel_ref_equality_still_matches(project):
    from prism_service.services.signal_resolver import resolve

    ctx = _ctx(project)
    task = ctx.task_svc.create(
        title="totally different", channel="github", channel_ref="org/repo#7",
    )
    signal = _make_signal(
        project, channel="github", channel_ref="org/repo#7", subject="hello",
    )
    matches = resolve(project, signal)
    ids = {t["id"]: t for t in matches["related_tasks"]}

    assert task.id in ids
    assert "channel_ref" in ids[task.id]["why"]
