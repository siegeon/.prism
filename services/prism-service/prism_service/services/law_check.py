"""law_check — the law runs over a diff at the gates (task 2bfe49db, epic
61821448: "Understand writes the law, the ontology holds it, the code
obeys it").

services/law_promotion.py (task c5650403) turns an architecture-principle
memory into a SHACL rule in ``<project data dir>/ontology/
promoted-shapes.ttl`` -- a SPARQLConstraint over ``o:imports`` between
``o:Module`` nodes whose ``rdfs:label`` is a repo-relative path. Until this
task, that rule only ran on the full ontology rebuild -- a slice that broke
a promoted principle would not see the refusal until the next rebuild, if
ever. This module runs the SAME promoted rules over the task's own
worktree DIFF at green_gate, cheaply: a handful of triples, in-process, no
worker subprocess.

Deliberately narrow, mirroring reachability_check.py's own doctrine:

  * ONLY the project's own ``promoted-shapes.ttl`` is loaded here -- never
    the package's built-in ``ontology/shapes*.ttl``. Those rules are not
    principles a memory promoted; they are not "the law" this tooth is
    checking, and loading them would validate against constraints that
    have nothing to do with a promoted architecture principle.
  * Only Python files the diff ADDS or MODIFIES are inspected (never
    deleted files, never tests) -- the same shape reachability_check.py
    scopes its own diff read to.
  * The ABox built here is a throwaway, in-process rdflib.Graph of tens of
    triples -- one o:Module per file touched by an import, one o:imports
    edge per resolved in-repo import. No owlrl closure (nothing here needs
    class-hierarchy inference) and no subprocess worker
    (ontology_rules.run_shapes's child-process isolation exists for a
    much bigger, real-ontology-scale SPARQLConstraint pass; this graph is
    too small to need it).
  * Abstains ("") on anything it cannot resolve: no promoted-shapes.ttl,
    no Python change in the diff, an unparsable file, an unresolvable
    workspace. Never raises -- a bug in this tooth must never take down a
    gate it was only ever meant to narrow.

Non-policy module (control_plane.POLICY_FILES lists conductor_service.py,
not this file) so the diff into the policy file stays minimal at the call
site -- see ConductorService.adjudicate_green_gate.
"""

from __future__ import annotations

import ast
import logging
import re
import subprocess
from pathlib import Path

import rdflib

from prism_service.services.ontology_graph import NS

logger = logging.getLogger(__name__)

_SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
_MAX_LINES = 5


# ---------------------------------------------------------------------------
# Diff read -- Python files ADDED or MODIFIED only, never deleted, never
# tests. Mirrors reachability_check._is_test_path exactly (kept local
# rather than imported, so this module stays a self-contained, easily
# read tooth -- the two checks read the same diff shape independently).
# ---------------------------------------------------------------------------

def _is_test_path(rel: str) -> bool:
    parts = rel.replace("\\", "/").split("/")
    if "tests" in parts:
        return True
    name = parts[-1] if parts else ""
    return (name.startswith("test_") or name.endswith("_test.py")
            or name == "conftest.py")


def _changed_python_files(workspace: Path, baseline: str) -> list[str]:
    """Repo-relative paths of every non-test .py file the working tree
    ADDS or MODIFIES relative to `baseline` -- never a deleted file."""
    changed: list[str] = []
    try:
        out = subprocess.run(
            ["git", "diff", "--name-status", baseline],
            cwd=str(workspace), capture_output=True, text=True, timeout=10)
    except Exception:
        return []
    if out.returncode == 0:
        for line in out.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            status = parts[0]
            if status.startswith("D"):
                continue
            changed.append(parts[-1])
    try:
        out2 = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(workspace), capture_output=True, text=True, timeout=10)
        if out2.returncode == 0:
            changed.extend(ln.strip() for ln in out2.stdout.splitlines()
                           if ln.strip())
    except Exception:
        pass

    seen: set = set()
    result: list[str] = []
    for rel in changed:
        rel = rel.strip()
        if not rel or rel in seen:
            continue
        seen.add(rel)
        if rel.endswith(".py") and not _is_test_path(rel):
            result.append(rel)
    return result


def _all_repo_files(workspace: Path) -> list[str]:
    """Every tracked + untracked-not-ignored file, git-scoped -- used to
    resolve a dotted import to a real repo file (never stdlib/third-
    party)."""
    files: list[str] = []
    for args in (["ls-files"], ["ls-files", "--others", "--exclude-standard"]):
        try:
            out = subprocess.run(["git", *args], cwd=str(workspace),
                                 capture_output=True, text=True, timeout=15)
        except Exception:
            continue
        if out.returncode == 0:
            files.extend(ln.strip() for ln in out.stdout.splitlines()
                        if ln.strip())
    return files


# ---------------------------------------------------------------------------
# Dotted import -> repo-relative path, and repo-relative path -> the label
# a promoted principle's SPARQL FILTERs on (STRSTARTS(?fromPath, "models")
# etc, task c5650403's principle rules key on bare package-relative
# segments like "models"/"services", not the full checkout path).
# ---------------------------------------------------------------------------

def _resolve_dotted_to_repo_path(dotted: str, repo_files: list[str]) -> str | None:
    """`dotted` ("prism_service.services.y") -> the repo-relative file it
    names, matched as a path SUFFIX against `repo_files` -- a submodule
    file first (a.b.c -> a/b/c.py or a/b/c/__init__.py), so this never
    needs a hard-coded package name and works the same whether the
    checkout nests the package under services/prism-service/ or not.
    None for anything that resolves to no in-repo file (stdlib, third-
    party, or a name that is not actually a module path)."""
    if not dotted or dotted.startswith("."):
        return None
    parts = [p for p in dotted.split(".") if p]
    if not parts:
        return None
    suffix_file = "/".join(parts) + ".py"
    suffix_pkg = "/".join(parts) + "/__init__.py"
    for f in repo_files:
        fn = f.replace("\\", "/")
        if fn == suffix_file or fn.endswith("/" + suffix_file):
            return fn
        if fn == suffix_pkg or fn.endswith("/" + suffix_pkg):
            return fn
    return None


def _label_for(rel_path: str) -> str:
    """The repo-relative path, trimmed to whatever sits after the
    ``prism_service/`` package root -- so "services/prism-service/
    prism_service/models/x.py" and a bare-checkout fixture's "prism_
    service/models/x.py" both read "models/x.py", matching the bare
    directory names (arc_governance.PRISM_PRINCIPLES: "models", "services",
    ...) a promoted principle's SPARQL FILTER(STRSTARTS(...)) expects.
    A path with no "prism_service/" segment passes through unchanged."""
    path = rel_path.replace("\\", "/")
    marker = "prism_service/"
    idx = path.rfind(marker)
    if idx == -1:
        return path
    return path[idx + len(marker):]


def _iri_slug(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return value or "mod"


# ---------------------------------------------------------------------------
# Build the throwaway ABox from the diff -- one o:Module per file touched
# by an import (the changed file, or one of its resolved import targets),
# one o:imports edge per import that resolves inside the repo.
# ---------------------------------------------------------------------------

def _build_diff_graph(workspace: Path, changed: list[str]) -> tuple[rdflib.Graph, dict]:
    g = rdflib.Graph()
    O = rdflib.Namespace(NS)
    labels_by_iri: dict = {}
    node_by_label: dict = {}
    repo_files = _all_repo_files(workspace)

    def module_node(label: str):
        node = node_by_label.get(label)
        if node is not None:
            return node
        node = rdflib.URIRef(f"{NS}mod-{_iri_slug(label)}")
        node_by_label[label] = node
        labels_by_iri[str(node)] = label
        g.add((node, rdflib.RDF.type, O.Module))
        g.add((node, rdflib.RDFS.label, rdflib.Literal(label)))
        return node

    for rel in changed:
        full = workspace / rel
        try:
            source = full.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue

        from_node = module_node(_label_for(rel))

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    target = _resolve_dotted_to_repo_path(alias.name, repo_files)
                    if target:
                        g.add((from_node, O.imports,
                              module_node(_label_for(target))))
            elif isinstance(node, ast.ImportFrom):
                if not node.module or node.level:
                    continue
                resolved_submodule = False
                for alias in node.names:
                    target = _resolve_dotted_to_repo_path(
                        f"{node.module}.{alias.name}", repo_files)
                    if target:
                        resolved_submodule = True
                        g.add((from_node, O.imports,
                              module_node(_label_for(target))))
                if not resolved_submodule:
                    target = _resolve_dotted_to_repo_path(node.module, repo_files)
                    if target:
                        g.add((from_node, O.imports,
                              module_node(_label_for(target))))

    return g, labels_by_iri


# ---------------------------------------------------------------------------
# The project's own promoted law -- ONLY promoted-shapes.ttl, never the
# package's built-in shapes.ttl (see the module docstring).
# ---------------------------------------------------------------------------

def _project_promoted_shapes_path(project: str) -> Path | None:
    if not project:
        return None
    from prism_service.config import project_data_dir
    path = project_data_dir(project) / "ontology" / "promoted-shapes.ttl"
    return path if path.exists() else None


def _rule_catalog(shapes_graph: rdflib.Graph) -> dict:
    """name -> {"derived_from": "mx-..." or ""} for every rule declared in
    `shapes_graph` -- read straight off the shapes, same keying discipline
    as ontology_rules.rule_catalog (never a second hand-kept list)."""
    q = f"""
        PREFIX sh: <{_SH}>
        PREFIX o: <{NS}>
        SELECT ?rule ?derived WHERE {{
            {{ ?node sh:property ?rule }} UNION {{ ?node sh:sparql ?rule }}
            OPTIONAL {{ ?rule o:derivedFrom ?derived }}
        }}
    """
    out: dict = {}
    prefix = f"{NS}instance/memory/"
    for row in shapes_graph.query(q):
        name = str(row.rule)
        if name.startswith(NS):
            name = name[len(NS):]
        if name.endswith(".target"):
            name = name[: -len(".target")]
        derived = str(row.derived) if row.derived else ""
        if derived.startswith(prefix):
            derived = derived[len(prefix):]
        out[name] = {"derived_from": derived}
    return out


def _violations_from_report(report_graph: rdflib.Graph) -> dict:
    violations: dict = {}
    for result in report_graph.subjects(rdflib.RDF.type, _SH.ValidationResult):
        source = (report_graph.value(result, _SH.sourceConstraint)
                  or report_graph.value(result, _SH.sourceShape))
        focus = report_graph.value(result, _SH.focusNode)
        if source is None or focus is None:
            continue
        name = str(source)
        if name.startswith(NS):
            name = name[len(NS):]
        if name.endswith(".target"):
            name = name[: -len(".target")]
        violations.setdefault(name, []).append(str(focus))
    return violations


def _edges_for_focus(data_graph: rdflib.Graph, focus_iri: str,
                     labels_by_iri: dict) -> list[tuple[str, str]]:
    out: list = []
    imports_prop = rdflib.URIRef(f"{NS}imports")
    for _s, _p, o in data_graph.triples(
            (rdflib.URIRef(focus_iri), imports_prop, None)):
        to_label = labels_by_iri.get(str(o))
        from_label = labels_by_iri.get(focus_iri)
        if to_label and from_label:
            out.append((from_label, to_label))
    return out


# ---------------------------------------------------------------------------
# Public entry points.
# ---------------------------------------------------------------------------

def law_violation_reason_for_diff(workspace, baseline: str,
                                  project: str = "default") -> str:
    """The testable core: a real git worktree, no task lookup. Returns one
    STE line per violation (name, both module paths, the memory this rule
    came from), capped at 5 -- or "" when sound, or when there is nothing
    promoted, no Python change, or nothing this tooth can inspect."""
    workspace = Path(workspace)
    try:
        shapes_path = _project_promoted_shapes_path(project)
        if shapes_path is None:
            return ""
        changed = _changed_python_files(workspace, baseline)
        if not changed:
            return ""
        data_graph, labels_by_iri = _build_diff_graph(workspace, changed)
        if len(data_graph) == 0:
            return ""

        shapes_graph = rdflib.Graph()
        shapes_graph.parse(str(shapes_path), format="turtle")
        catalog = _rule_catalog(shapes_graph)

        import pyshacl
        _conforms, report_graph, _text = pyshacl.validate(
            data_graph=data_graph, shacl_graph=shapes_graph,
            data_graph_format=None, shacl_graph_format="turtle",
            advanced=True, meta_shacl=False, inference="none",
        )
        violations = _violations_from_report(report_graph)
    except Exception:
        logger.debug("law_check: could not evaluate the promoted rules",
                     exc_info=True)
        return ""
    if not violations:
        return ""

    lines: list[str] = []
    seen: set = set()
    for name, focus_list in violations.items():
        derived = catalog.get(name, {}).get("derived_from", "")
        for focus in focus_list:
            for from_label, to_label in _edges_for_focus(
                    data_graph, focus, labels_by_iri):
                key = (name, from_label, to_label)
                if key in seen:
                    continue
                seen.add(key)
                memory_clause = (
                    f" From memory {derived} (Understand)." if derived else "")
                lines.append(
                    f"Rule {name} fires on this diff. Module {from_label} "
                    f"imports {to_label}.{memory_clause} Fix the import or "
                    "ask for an exemption on the Queue.")
                if len(lines) >= _MAX_LINES:
                    return "\n".join(lines)
    return "\n".join(lines)


def law_violation_reason(task, project: str = "default") -> str:
    """Task-level entry point for ConductorService.adjudicate_green_gate.
    Resolves the task's real conductor worktree the way
    reachability_check.unreachable_entry_point_reason does (same fresh-
    merge-base self-heal) and diffs it against its baseline. A task with
    no resolvable workspace abstains -- "" -- rather than refuse work it
    cannot even inspect."""
    task_id = str(getattr(task, "id", "") or "").strip()
    if not task_id:
        return ""
    try:
        from prism_service.services import task_workspace
        ws = task_workspace.workspace_for(task_id)
    except Exception:
        return ""
    path = (ws or {}).get("path") if ws else None
    baseline = (ws or {}).get("baseline") if ws else None
    if not path or not baseline:
        return ""
    try:
        if not Path(path).is_dir():
            return ""
        from prism_service.services import reachability_check
        baseline = reachability_check._fresh_merge_base(
            Path(path), baseline) or baseline
        return law_violation_reason_for_diff(Path(path), baseline, project)
    except Exception:
        return ""
