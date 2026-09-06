"""Archify service for building and rendering architecture diagrams."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from prism_service.config import project_data_dir
from prism_service.services.archify_maps import build_ir
from prism_service.vendor.archify_paths import ARCHIFY_BIN, ARCHIFY_DIR, node_executable

logger = logging.getLogger(__name__)


def _count(ir: dict, *keys: str) -> int:
    """Count the first present list among `keys`. An architecture map carries
    components/connections; a workflow map carries nodes/edges."""
    for key in keys:
        value = ir.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def stamp_edge_ids(ir: dict) -> dict:
    """Give every relationship a STABLE id, in place.

    Archify's compare refuses a base whose connections have no authored ids
    ("base connections require authored stable ids for comparison") — without
    them it cannot tell which line in the new map is which line in the old
    one. An id derived from the endpoints is stable across rebuilds, which is
    exactly what a diff needs, so the service guarantees it for every map
    rather than asking each builder to remember.
    """
    for key in ("connections", "edges"):
        rows = ir.get(key)
        if not isinstance(rows, list):
            continue
        seen: dict[str, int] = {}
        for row in rows:
            if not isinstance(row, dict) or row.get("id"):
                continue
            base = _edge_slug(f"{row.get('from', '')}-to-{row.get('to', '')}")
            n = seen.get(base, 0)
            seen[base] = n + 1
            row["id"] = base if n == 0 else f"{base}-{n + 1}"
    return ir


_EDGE_BAD = re.compile(r"[^a-zA-Z0-9_-]+")


def _edge_slug(text: str) -> str:
    s = _EDGE_BAD.sub("-", str(text)).strip("-") or "edge"
    return s if s[0].isalpha() else f"e-{s}"


def _changed_count(summary: dict) -> int:
    """How many authored facts the diff reports as moved.

    The receipt groups changes per collection (components, connections,
    boundaries, presentation), each with added/removed/changed lists. A
    count of zero means the publish did not move the architecture, which is
    itself worth saying.
    """
    total = 0
    for value in (summary or {}).values():
        if isinstance(value, dict):
            for bucket in ("added", "removed", "changed", "moved", "rerouted"):
                item = value.get(bucket)
                if isinstance(item, list):
                    total += len(item)
                elif isinstance(item, int):
                    total += item
        elif isinstance(value, int):
            total += value
    return total


def _ir_title(ir: dict, kind: str) -> str:
    """The diagram's own title, so the card names the map a person sees."""
    meta = ir.get("meta")
    if isinstance(meta, dict):
        title = meta.get("title")
        if isinstance(title, str) and title.strip():
            return title
    return kind.capitalize()


class ArchifyBuildError(Exception):
    """Raised when archify build/validate fails."""

    def __init__(self, diagnostics: list[dict] | str):
        self.diagnostics = diagnostics
        if isinstance(diagnostics, list):
            msg = "; ".join(d.get("message", str(d)) for d in diagnostics)
        else:
            msg = diagnostics
        super().__init__(msg)


class ArchifyService:
    """Service for building and managing archify architecture diagrams."""

    def __init__(self, project: str):
        """Initialize the service for a project.

        Args:
            project: Project ID. Root data directory is project_data_dir(project) / "archify"
        """
        self.project = project
        self.root = project_data_dir(project) / "archify"
        self.root.mkdir(parents=True, exist_ok=True)

    def map_dir(self, kind: str, task_id: str | None = None) -> Path:
        """Return the directory for a map of the given kind.

        For kind="task", uses archify/task/<task_id>/; otherwise archify/<kind>/.
        """
        if kind == "task":
            if not task_id:
                raise ValueError("task_id required for kind='task'")
            d = self.root / "task" / task_id
        else:
            d = self.root / kind
        d.mkdir(parents=True, exist_ok=True)
        return d

    def validate(self, diagram_type: str, ir: dict) -> dict:
        """Validate an IR against archify's validate command.

        Runs: node ARCHIFY_BIN validate <type> <ir.json> --quality standard --json
        Returns: parsed JSON (ok/diagnostics)
        """
        # A scratch file, NEVER a map's own ir.json: validating any kind used
        # to write through the code map's ir.json and clobber it.
        tmp = tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="archify-validate-", delete=False,
        )
        ir_path = Path(tmp.name)

        try:
            with tmp:
                json.dump(ir, tmp, indent=2)

            result = subprocess.run(
                [
                    node_executable(),
                    str(ARCHIFY_BIN),
                    "validate",
                    diagram_type,
                    str(ir_path),
                    "--quality",
                    "standard",
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=180,
                cwd=str(ARCHIFY_DIR),
            )

            if result.returncode != 0:
                # Parse stderr or stdout for error details
                try:
                    output = json.loads(result.stdout) if result.stdout else {}
                except json.JSONDecodeError:
                    output = {"error": result.stderr or result.stdout or "unknown error"}
                return output

            return json.loads(result.stdout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "diagnostics": [{"message": "validation timeout"}]}
        except json.JSONDecodeError as e:
            return {
                "ok": False,
                "diagnostics": [{"message": f"invalid JSON response: {str(e)}"}],
            }
        except Exception as e:
            return {"ok": False, "diagnostics": [{"message": str(e)}]}
        finally:
            try:
                ir_path.unlink()
            except OSError:
                pass

    def render(
        self, kind: str, diagram_type: str, ir: dict, task_id: str | None = None
    ) -> dict:
        """Render an IR to map.html and return metadata.

        Writes ir.json, runs deliver, writes receipt.json + meta.json.
        Returns: meta dict
        """
        map_dir = self.map_dir(kind, task_id)
        ir_path = map_dir / "ir.json"
        html_path = map_dir / "map.html"
        receipt_path = map_dir / "receipt.json"
        meta_path = map_dir / "meta.json"

        # ir.json IS the base the NEXT publish diffs against, so the stamp
        # belongs here rather than in build(): everything this service
        # writes must be comparable, however it was produced.
        stamp_edge_ids(ir)

        try:
            # Write IR
            ir_path.write_text(json.dumps(ir, indent=2))

            # Run deliver command
            result = subprocess.run(
                [
                    node_executable(),
                    str(ARCHIFY_BIN),
                    "deliver",
                    diagram_type,
                    str(ir_path),
                    str(html_path),
                    "--quality",
                    "standard",
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=180,
                cwd=str(ARCHIFY_DIR),
            )

            try:
                receipt = json.loads(result.stdout) if result.stdout else {}
            except json.JSONDecodeError:
                receipt = {
                    "ok": False,
                    "error": result.stderr or result.stdout or "render failed",
                }

            # Write receipt
            receipt_path.write_text(json.dumps(receipt, indent=2))

            # Counts come from the IR, never from the deliver receipt: the
            # receipt's keys are schemaVersion/ok/command/type/input/output/
            # specification/artifact/validation, so reading "components" off
            # it silently reported 0 for every map.
            meta = {
                "kind": kind,
                "diagram_type": diagram_type,
                "task_id": task_id,
                "title": _ir_title(ir, kind),
                "built_at": datetime.now(timezone.utc).isoformat(),
                "ok": bool(receipt.get("ok", False)) and html_path.exists(),
                "components": _count(ir, "components", "nodes"),
                "connections": _count(ir, "connections", "edges"),
                "error": receipt.get("error", ""),
                "html_url": f"/api/archify/maps/{kind}/html?project={self.project}"
                + (f"&task_id={task_id}" if task_id else ""),
            }
            meta_path.write_text(json.dumps(meta, indent=2))

            return meta
        except subprocess.TimeoutExpired:
            return {
                "kind": kind,
                "diagram_type": diagram_type,
                "ok": False,
                "error": "render timeout",
                "task_id": task_id,
                "title": kind.capitalize(),
                "built_at": datetime.now(timezone.utc).isoformat(),
                "components": 0,
                "connections": 0,
            }
        except Exception as e:
            return {
                "kind": kind,
                "diagram_type": diagram_type,
                "ok": False,
                "error": str(e),
                "task_id": task_id,
                "title": kind.capitalize(),
                "built_at": datetime.now(timezone.utc).isoformat(),
                "components": 0,
                "connections": 0,
            }

    def build(self, kind: str, task_id: str | None = None) -> dict:
        """Build and render a map: build_ir() -> validate() -> render().

        On validate failure, store receipt with ok=false and raise ArchifyBuildError.
        Returns: meta dict (with ok=True if successful)
        """
        try:
            diagram_type, ir = build_ir(self.project, kind, task_id=task_id)
        except KeyError:
            raise ArchifyBuildError(f"unknown kind: {kind}")

        # Validate
        validation = self.validate(diagram_type, ir)
        if not validation.get("ok", False):
            diagnostics = validation.get("diagnostics", [])
            # Store receipt with ok=false
            map_dir = self.map_dir(kind, task_id)
            receipt_path = map_dir / "receipt.json"
            receipt_path.write_text(json.dumps(validation, indent=2))

            raise ArchifyBuildError(diagnostics)

        # Render
        meta = self.render(kind, diagram_type, ir, task_id)
        return meta

    def meta(self, kind: str, task_id: str | None = None) -> dict | None:
        """Return meta.json for a map, or None if not found.

        Never raises.
        """
        try:
            meta_path = self.map_dir(kind, task_id) / "meta.json"
            if meta_path.exists():
                return json.loads(meta_path.read_text())
        except Exception:
            pass
        return None

    def html(self, kind: str, task_id: str | None = None) -> str | None:
        """Return map.html content, or None if not found.

        Never raises.
        """
        try:
            html_path = self.map_dir(kind, task_id) / "map.html"
            if html_path.exists():
                return html_path.read_text()
        except Exception:
            pass
        return None

    def compare(self, kind: str, base_ir: dict,
                task_id: str | None = None) -> dict:
        """Diff the map's CURRENT ir.json against `base_ir` (the previous one).

        Writes `delta.html` and `delta.receipt.json` beside the map, so a
        publish can show WHAT CHANGED in the architecture rather than only a
        redrawn picture. Architecture maps only — archify's compare command
        takes `compare architecture <base> <head>`.

        Returns {"ok", "changed", "summary", "error"}. Never raises.
        """
        map_dir = self.map_dir(kind, task_id)
        head_path = map_dir / "ir.json"
        if not head_path.exists():
            return {"ok": False, "changed": 0, "summary": {},
                    "error": "no current map to compare against"}

        base_path = map_dir / "previous.ir.json"
        delta_path = map_dir / "delta.html"
        receipt_path = map_dir / "delta.receipt.json"
        try:
            base_path.write_text(json.dumps(base_ir, indent=2))
            result = subprocess.run(
                [node_executable(), str(ARCHIFY_BIN), "compare", "architecture",
                 str(base_path), str(head_path), str(delta_path),
                 "--receipt", str(receipt_path), "--json"],
                capture_output=True, text=True, timeout=180,
                cwd=str(ARCHIFY_DIR),
            )
            try:
                out = json.loads(result.stdout) if result.stdout else {}
            except json.JSONDecodeError:
                return {"ok": False, "changed": 0, "summary": {},
                        "error": (result.stderr or result.stdout
                                  or "compare produced no JSON")[:200]}
            if not out.get("ok"):
                return {"ok": False, "changed": 0, "summary": {},
                        "error": str(out.get("error") or "compare refused")[:200]}
            summary = out.get("summary") or {}
            if not summary and receipt_path.exists():
                try:
                    summary = json.loads(
                        receipt_path.read_text()).get("summary") or {}
                except Exception:  # noqa: BLE001
                    summary = {}
            return {"ok": True, "changed": _changed_count(summary),
                    "summary": summary, "error": ""}
        except subprocess.TimeoutExpired:
            return {"ok": False, "changed": 0, "summary": {},
                    "error": "compare timeout"}
        except Exception as exc:  # noqa: BLE001 - a diff never fails a publish
            return {"ok": False, "changed": 0, "summary": {},
                    "error": str(exc)[:200]}

    def delta_html(self, kind: str, task_id: str | None = None) -> str | None:
        """The rendered Before / Delta / After page, or None. Never raises."""
        try:
            path = self.map_dir(kind, task_id) / "delta.html"
            if path.exists():
                return path.read_text()
        except Exception:
            pass
        return None

    def delta_receipt(self, kind: str, task_id: str | None = None) -> dict | None:
        """The machine receipt for the last diff, or None. Never raises."""
        try:
            path = self.map_dir(kind, task_id) / "delta.receipt.json"
            if path.exists():
                return json.loads(path.read_text())
        except Exception:
            pass
        return None

    def ir(self, kind: str, task_id: str | None = None) -> dict | None:
        """Return IR dict, or None if not found.

        Never raises.
        """
        try:
            ir_path = self.map_dir(kind, task_id) / "ir.json"
            if ir_path.exists():
                return json.loads(ir_path.read_text())
        except Exception:
            pass
        return None

    def receipt(self, kind: str, task_id: str | None = None) -> dict | None:
        """Return receipt.json, or None if not found.

        Never raises.
        """
        try:
            receipt_path = self.map_dir(kind, task_id) / "receipt.json"
            if receipt_path.exists():
                return json.loads(receipt_path.read_text())
        except Exception:
            pass
        return None

    def list_maps(self) -> list[dict]:
        """List all meta.json files under archify/ (kinds + tasks).

        Each entry is a meta dict.
        """
        maps = []
        if not self.root.exists():
            return maps

        # Scan top-level kind directories
        for kind in ["code", "concepts", "language"]:
            kind_dir = self.root / kind
            if kind_dir.exists():
                meta_file = kind_dir / "meta.json"
                if meta_file.exists():
                    try:
                        maps.append(json.loads(meta_file.read_text()))
                    except Exception:
                        pass

        # Scan task-specific maps
        task_dir = self.root / "task"
        if task_dir.exists():
            for task_subdir in task_dir.iterdir():
                if task_subdir.is_dir():
                    meta_file = task_subdir / "meta.json"
                    if meta_file.exists():
                        try:
                            maps.append(json.loads(meta_file.read_text()))
                        except Exception:
                            pass

        return maps

    def doctor(self) -> dict:
        """Check archify health: {"ok": bool, "node": str, "output": str}"""
        try:
            node = node_executable()
            result = subprocess.run(
                [node, str(ARCHIFY_BIN), "doctor", "--json"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(ARCHIFY_DIR),
            )
            try:
                output = json.loads(result.stdout) if result.stdout else {}
            except json.JSONDecodeError:
                output = {"error": result.stderr or result.stdout}

            return {"ok": result.returncode == 0, "node": node, "output": output}
        except Exception as e:
            return {"ok": False, "node": "", "output": str(e)}
