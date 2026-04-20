"""Retrievals observability page — recent searches with per-query details.

Lets you answer: why did retrieval miss (or hit) on a given query, which
stack flags were in effect, and how long did it take. Data comes from
the ``searches`` table populated by ``Brain.search`` on every call.
"""

import json

from nicegui import ui, app

from app.project_context import get_project
from app.ui.components.nav import create_nav, page_container


def _brain_svc():
    return get_project(app.storage.user.get("project", "default")).brain_svc


def _fmt_ts(ts: str) -> str:
    if not ts:
        return "-"
    return ts[:19].replace("T", " ")


def _flags(row: dict) -> str:
    bits = []
    bits.append("prefix" if row.get("context_prefix") else "no-prefix")
    bits.append("agg" if row.get("chunk_agg") else "no-agg")
    rr = row.get("rerank") or "off"
    if rr != "off":
        bits.append(f"rerank={rr}")
    mode = row.get("mode") or "hybrid"
    if mode != "hybrid":
        bits.append(f"mode={mode}")
    return " · ".join(bits)


def _build_summary(container, rows: list[dict]) -> None:
    container.clear()
    with container:
        n = len(rows)
        lats = [r.get("latency_ms", 0) or 0 for r in rows]
        lats = [x for x in lats if x]
        avg = sum(lats) / len(lats) if lats else 0
        p95 = sorted(lats)[int(len(lats) * 0.95)] if len(lats) >= 20 else (
            max(lats) if lats else 0
        )
        empty = sum(1 for r in rows if (r.get("n_results") or 0) == 0)
        cards = [
            ("Searches", str(n), "search", "#4f46e5"),
            ("Avg latency", f"{avg:.0f} ms", "timer", "#0d9488"),
            ("p95 latency", f"{p95} ms", "speed", "#7c3aed"),
            ("Empty result", f"{empty}", "inbox", "#dc2626"),
        ]
        with ui.row().classes("w-full gap-5 flex-wrap"):
            for title, value, icon, color in cards:
                with ui.card().classes(
                    "flex-1 min-w-[180px] bg-white shadow-sm rounded-lg p-5"
                ):
                    with ui.row().classes("items-center gap-2 mb-3"):
                        ui.icon(icon, color=color).classes("text-2xl")
                        ui.label(title).classes(
                            "text-xs text-gray-500 uppercase tracking-wide"
                            " font-medium"
                        )
                    ui.label(value).classes(
                        "text-2xl font-bold text-gray-900"
                    )


def _build_table(container, rows: list[dict]) -> None:
    container.clear()
    with container:
        if not rows:
            with ui.card().classes(
                "w-full bg-white shadow-sm rounded-lg p-8 text-center"
            ):
                ui.icon("search_off", color="gray").classes(
                    "text-4xl mb-3"
                )
                ui.label("No searches recorded yet").classes(
                    "text-base font-medium text-gray-500"
                )
                ui.label(
                    "Any brain_search MCP call will appear here."
                ).classes("text-sm text-gray-400 mt-2")
            return
        columns = [
            {"name": "ts", "label": "When", "field": "ts", "align": "left"},
            {"name": "query", "label": "Query", "field": "query",
             "align": "left"},
            {"name": "flags", "label": "Flags", "field": "flags",
             "align": "left"},
            {"name": "n", "label": "N", "field": "n", "align": "right"},
            {"name": "lat", "label": "Latency", "field": "lat",
             "align": "right"},
            {"name": "top", "label": "Top hit", "field": "top",
             "align": "left"},
        ]
        data = []
        for r in rows:
            top_hit = "-"
            try:
                parsed = json.loads(r.get("final_top") or "[]")
                if parsed:
                    top_hit = parsed[0].get("doc_id") or "-"
            except Exception:
                pass
            data.append({
                "id": r.get("id"),
                "ts": _fmt_ts(r.get("ts") or ""),
                "query": (r.get("query") or "")[:80],
                "flags": _flags(r),
                "n": r.get("n_results", 0),
                "lat": f"{r.get('latency_ms', 0)} ms",
                "top": top_hit[:80] if top_hit else "-",
            })
        ui.table(columns=columns, rows=data, row_key="id").classes("w-full")


@ui.page("/retrievals")
def retrievals_page():
    """Recent brain_search events with per-query details."""
    ui.colors(primary="#4f46e5")
    create_nav()

    with page_container():
        ui.label("Retrievals").classes(
            "text-2xl font-semibold text-gray-900"
        )
        summary_box = ui.column().classes("w-full")
        with ui.card().classes("w-full bg-white shadow-sm rounded-lg p-5"):
            ui.label("Recent searches").classes(
                "text-lg font-semibold text-gray-900 mb-4"
            )
            table_box = ui.column().classes("w-full")

    def refresh():
        rows = _brain_svc().recent_searches(limit=100)
        _build_summary(summary_box, rows)
        _build_table(table_box, rows)

    refresh()
    ui.timer(5.0, refresh)
