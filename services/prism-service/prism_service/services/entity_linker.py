"""entity_linker — cross-clicking every noun/id in task and signal text to
its ontology data (task 6968cc39, epic 47bba8fe, owner relayed via the
ontology-cards session: "make sure we have cross clicking on every noun
verb etc in the tasks... there are many words in the description of the
tasks that do not show any of the linked data").

link(project, text) resolves free text against the SAME OntologyGraph
SPARQL store every other ontology surface reads (services/ontology_graph
.OntologyGraph — never a second re-derivation of task/memory/document
rows): task ids (8-char prefix or full uuid) and exact titles -> /tasks/
<id>; memory mx- ids -> /understand?concept=<id>; document/folder paths
-> /files?path=<path>; TBox class names -> /ontology?tab=structure; the
channel/workflow/task_status/proof_type Terms vocabularies -> /ontology?
tab=terms (signal_state/ask/gate_state are deliberately left out — the
owner's own list names only "channels, workflow names, statuses, proof
types", and those three extra vocabularies' values overlap ordinary
English — "none", "failed" — more than the requested four do); agent ids
-> /workflows?workflow=<id>; Jira ticket keys and GitHub PR/issue refs
(via services.signal_parse's own regexes, never a re-invented pattern) ->
a connector browse URL when one is known, else no href; known Person
actors -> no href (no actor page exists yet in this app — the resolution
path is left out entirely rather than emit a link that goes nowhere).

The label index (one SPARQL pass over the ABox + one over the TBox +
ontology_terms.terms()) is built ONCE per project and cached keyed on
ontology_rules.last_validated_at(project) — that stamp only moves forward
when a rebuild runs, so a request is a token scan against a cached dict,
never N SPARQL round trips.

Matching: text is tokenized on whole-word boundaries (letters/digits/
_./#- so a path, an id, or a ticket key is one token). At each token
position the LONGEST run of consecutive tokens is tried first, and the
longest match found anywhere wins ties globally — ids, paths, class names
and Terms values match exact-case only; a task/memory TITLE matches
case-insensitively but only once it is >= 2 tokens (so a one-word title
can never hijack ordinary prose). A single-token match under 3
characters, or in a small stopword set, is never linked. Spans never
overlap.
"""

from __future__ import annotations

import re
from typing import Callable
from urllib.parse import quote

from prism_service.services import signal_parse
from prism_service.services.ontology_graph import NS, OntologyGraph, _iri, _ref_of, open_if_exists

_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "for", "to", "of", "in", "on",
    "at", "is", "it", "be", "as", "by", "with", "from", "this", "that",
    "not", "no", "do", "did", "so", "if", "we", "you", "he", "she",
    "they", "are", "was", "were", "been", "has", "have", "had", "will",
    "can", "may", "task", "tasks", "new",
}
_MIN_LEN = 3
_MAX_PHRASE = 8

_TOKEN_RE = re.compile(r"[A-Za-z0-9_./#-]+")

_MEMORY_CLASSES = {"Pattern", "Convention", "Failure", "Decision", "Concept"}


def _task_href(key: str) -> str:
    return f"/tasks/{key}"


def _memory_href(key: str) -> str:
    return f"/understand?concept={quote(key, safe='')}"


def _doc_href(key: str) -> str:
    return f"/files?path={quote(key, safe='')}"


def _agent_href(key: str) -> str:
    return f"/workflows?workflow={quote(key, safe='')}"


# ABox class local name -> (bucket, href builder). Channel/Provider/Signal
# and the code-graph kinds are deliberately absent — Channel is covered by
# the Terms vocabulary instead (one link target, not two disagreeing
# ones), and the rest aren't named by the ticket's own link list. Person
# is absent too: see the module docstring.
_INSTANCE_TARGETS: dict[str, tuple[str, Callable[[str], str]]] = {
    "Task": ("task", _task_href),
    "Document": ("document", _doc_href),
    "Folder": ("folder", _doc_href),
    "Agent": ("agent", _agent_href),
}

# Exactly the four vocabularies the owner's own description names.
_TERM_VOCABULARIES = ("channel", "workflow", "task_status", "proof_type")


class _Entry:
    __slots__ = ("cls", "href", "label", "iri")

    def __init__(self, cls_: str, href: str, label: str, iri: str = "") -> None:
        self.cls = cls_
        self.href = href
        self.label = label
        self.iri = iri


_CACHE: dict[str, tuple[str, dict, dict, int]] = {}


def _tokens(text: str) -> list[str]:
    return [m.group(0) for m in _TOKEN_RE.finditer(text)]


def _positions(text: str) -> list[tuple[str, int, int]]:
    """Tokenize WITH offsets, trimming a bare trailing '.' (sentence-final
    punctuation, e.g. "...see mx-abc123." must still match "mx-abc123") --
    a real path/id token never legitimately ENDS in a literal dot, so this
    never clips a genuine match."""
    out = []
    for m in _TOKEN_RE.finditer(text):
        tok, end = m.group(0), m.end()
        while tok.endswith(".") and len(tok) > 1:
            tok, end = tok[:-1], end - 1
        out.append((tok, m.start(), end))
    return out


def _register(exact: dict, ci: dict, text: str, entry: "_Entry") -> int:
    """Add one label to the index; returns its token count (0 when the
    label is empty or fails the single-token stopword/length filter) so
    the caller can track the longest phrase seen. A phrase of >= 2 tokens
    is ALSO keyed case-insensitively — a one-word label never is, so it
    can only ever match its own exact case."""
    toks = tuple(_tokens(text))
    if not toks:
        return 0
    if len(toks) == 1:
        tok = toks[0]
        if len(tok) < _MIN_LEN or tok.lower() in _STOPWORDS:
            return 0
        exact.setdefault(toks, entry)
        return 1
    exact.setdefault(toks, entry)
    ci.setdefault(tuple(t.lower() for t in toks), entry)
    return len(toks)


def _build_index(project: str) -> tuple[dict, dict, int]:
    """One SPARQL pass over the ABox for real instances (tasks/documents/
    folders/agents/memories), one over the TBox for class names, and one
    ontology_terms.terms() call for the four requested vocabularies."""
    graph = OntologyGraph(project)
    exact: dict[tuple[str, ...], _Entry] = {}
    ci: dict[tuple[str, ...], _Entry] = {}
    max_len = 1

    def add(text: str, entry: _Entry) -> None:
        nonlocal max_len
        max_len = max(max_len, _register(exact, ci, text, entry))

    abox = graph._abox_iri.value
    model = NS + "model"

    q = ("PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
         f"SELECT ?i ?label ?cls WHERE {{ GRAPH <{abox}> {{ "
         "?i a ?cls . OPTIONAL { ?i rdfs:label ?label } } }")
    for row in graph.query(q)["bindings"]:
        iri, cls_full = row["i"], row["cls"]
        label = row.get("label") or ""
        cls_local = cls_full[len(NS):] if cls_full.startswith(NS) else cls_full
        if cls_local in _MEMORY_CLASSES:
            key = _ref_of("memory", iri)
            add(key, _Entry(cls_local, _memory_href(key), label or key, iri))
            continue
        target = _INSTANCE_TARGETS.get(cls_local)
        if target is None:
            continue
        bucket, href_fn = target
        key = _ref_of(bucket, iri)
        add(key, _Entry(cls_local, href_fn(key), label or key, iri))
        if cls_local == "Task":
            if len(key) > 8:
                add(key[:8], _Entry(cls_local, href_fn(key), label or key, iri))
            if label:
                add(label, _Entry(cls_local, href_fn(key), label, iri))

    qc = ("PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
          f"SELECT ?c WHERE {{ GRAPH <{model}> {{ ?c a rdfs:Class }} }}")
    for row in graph.query(qc)["bindings"]:
        c = row["c"]
        local = c[len(NS):] if c.startswith(NS) else c
        add(local, _Entry(local, "/ontology?tab=structure", local, c))

    from prism_service.services import ontology_terms as terms_svc

    report = terms_svc.terms(project)
    for vocab in report["vocabularies"]:
        if vocab["name"] not in _TERM_VOCABULARIES:
            continue
        for term in vocab["terms"]:
            value = term["value"]
            if value:
                add(value, _Entry("Term", "/ontology?tab=terms", value))

    return exact, ci, min(max_len, _MAX_PHRASE)


def _index(project: str) -> tuple[dict, dict, int]:
    """Cached per project, invalidated on the graph's own validated_at
    stamp — never re-queries SPARQL for a request that lands between two
    rebuilds. Auto-rebuilds an absent/empty graph ONCE (the same posture
    api/okf.py's GET routes already use) so a fresh project still links."""
    from prism_service.services import ontology_prototype_projection as proj
    from prism_service.services import ontology_rules

    g = open_if_exists(project)
    if g is None or g.is_empty():
        proj.rebuild(project)

    stamp = ontology_rules.last_validated_at(project)
    cached = _CACHE.get(project)
    if cached is not None and cached[0] == stamp:
        return cached[1], cached[2], cached[3]
    exact, ci, max_len = _build_index(project)
    _CACHE[project] = (stamp, exact, ci, max_len)
    return exact, ci, max_len


def _jira_href(key: str) -> str:
    """The connector browse URL 'when known' (a live Jira connection with
    a resolvable site) -- best-effort, "" on anything short of that
    (no connection, lookup failure) rather than a broken partial URL."""
    try:
        from prism_service.services.integration_store import get_integration_store
        from prism_service.services.jira_auth import get_jira_auth_store

        row = get_integration_store()._db.execute(
            "SELECT workspace_id, remote_scope FROM integration_connections "
            "WHERE provider='jira' LIMIT 1"
        ).fetchone()
        if not row:
            return ""
        site = get_jira_auth_store().site_url(row["workspace_id"], row["remote_scope"])
        return f"{site.rstrip('/')}/browse/{key}" if site else ""
    except Exception:
        return ""


def _ticket_and_pr_spans(text: str) -> list[tuple[int, int, _Entry, str]]:
    """Jira ticket keys and GitHub PR/issue refs, found with the SAME
    regexes signal_parse.parse() itself extracts with (never a re-invented
    pattern) -- but kept here as match objects so real character offsets
    survive, which parse()'s own Extraction shape does not carry."""
    out: list[tuple[int, int, _Entry, str]] = []
    known = signal_parse._known_jira_projects()
    for m in signal_parse._JIRA_RE.finditer(text):
        project_key, number = m.group(1), m.group(2)
        if project_key not in known:
            continue
        key = f"{project_key}-{number}"
        entry = _Entry("JiraIssue", _jira_href(key), key, _iri("jira_issue", key))
        out.append((m.start(), m.end(), entry, m.group(0)))

    # GitHub URL refs carry their own known destination; the other three
    # PR/issue shorthands (owner/repo#N, "PR #N", bare #N) name a real
    # entity but no connector is wired to resolve them to a URL yet.
    for m in signal_parse._GH_URL_RE.finditer(text):
        repo, kind_word, num = m.group(1), m.group(2), m.group(3)
        ref_key = f"{repo}#{num}"
        href = f"https://{m.group(0)}"
        entry = _Entry("PullRequest", href, ref_key, _iri("pull_request", ref_key))
        out.append((m.start(), m.end(), entry, m.group(0)))
    for m in signal_parse._OWNER_REPO_HASH_RE.finditer(text):
        ref_key = f"{m.group(1)}#{m.group(2)}"
        entry = _Entry("PullRequest", "", ref_key, _iri("pull_request", ref_key))
        out.append((m.start(), m.end(), entry, m.group(0)))
    for m in signal_parse._PR_WORD_RE.finditer(text):
        ref_key = f"#{m.group(1)}"
        entry = _Entry("PullRequest", "", ref_key, _iri("pull_request", ref_key))
        out.append((m.start(), m.end(), entry, m.group(0)))
    for m in signal_parse._BARE_HASH_RE.finditer(text):
        ref_key = f"#{m.group(1)}"
        entry = _Entry("PullRequest", "", ref_key, _iri("pull_request", ref_key))
        out.append((m.start(), m.end(), entry, m.group(0)))
    return out


def link(project: str, text: str) -> list[dict]:
    """Every ontology-known entity `text` mentions, as non-overlapping
    spans in reading order: [{start, end, text, kind, iri, cls, href,
    label}]. See the module docstring for the matching rules."""
    if not text or not text.strip():
        return []

    exact, ci, max_len = _index(project)
    positions = _positions(text)

    candidates: list[tuple[int, int, _Entry, str]] = []
    n = len(positions)
    for i in range(n):
        for length in range(min(max_len, n - i), 0, -1):
            toks = tuple(p[0] for p in positions[i:i + length])
            entry = exact.get(toks)
            if entry is None and length >= 2:
                entry = ci.get(tuple(t.lower() for t in toks))
            if entry is not None:
                start, end = positions[i][1], positions[i + length - 1][2]
                candidates.append((start, end, entry, text[start:end]))
                break

    candidates.extend(_ticket_and_pr_spans(text))

    # Longest match wins globally, leftmost breaks ties; a span whose
    # range overlaps one already accepted is dropped, never re-split.
    candidates.sort(key=lambda c: (-(c[1] - c[0]), c[0]))
    accepted: list[tuple[int, int, _Entry, str]] = []
    for cand in candidates:
        start, end = cand[0], cand[1]
        if any(start < a[1] and end > a[0] for a in accepted):
            continue
        accepted.append(cand)
    accepted.sort(key=lambda c: c[0])

    return [
        {"start": s, "end": e, "text": t, "kind": entry.cls.lower(),
         "iri": entry.iri, "cls": entry.cls, "href": entry.href, "label": entry.label}
        for s, e, entry, t in accepted
    ]
