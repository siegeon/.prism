"""WorkflowTable — 8-step progress DataTable widget."""

from __future__ import annotations

from datetime import datetime

from textual.widgets import DataTable, Static

from models import WORKFLOW_STEPS, WorkflowState


def _fmt_duration(seconds: int) -> str:
    """Format seconds into compact duration."""
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


class WorkflowTable(Static):
    """Displays the 8-step workflow progress as a colored table."""

    DEFAULT_CSS = """
    WorkflowTable {
        height: auto;
        max-height: 14;
        padding: 0 1;
    }
    """

    def compose(self):
        yield DataTable(id="wf-table")

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "none"
        table.zebra_stripes = True
        table.add_columns("#", "Step", "Phase", "Agent", "Type", "Model", "Status")
        self._populate(None)

    def update_state(self, state: WorkflowState | None) -> None:
        self._populate(state)

    def _populate(self, state: WorkflowState | None) -> None:
        table = self.query_one(DataTable)
        table.clear()

        current_idx = state.current_step_index if state and state.active else -1

        # Compute live duration for current step
        duration_str = ""
        is_stale = False
        if state and state.active and state.started_at_dt:
            elapsed = max(0, int((datetime.now() - state.started_at_dt).total_seconds()))
            duration_str = _fmt_duration(elapsed)
            # Detect orphaned/stale state: no last_activity or very old
            if state.last_activity_dt:
                stale_secs = int((datetime.now() - state.last_activity_dt).total_seconds())
                is_stale = stale_secs > 600  # 10+ min without activity
            elif elapsed > 300:
                # No last_activity at all and started >5min ago → orphaned
                is_stale = True

        for step in WORKFLOW_STEPS:
            if state and state.active:
                if step.index < current_idx:
                    status = "[green]\u2713 DONE[/]"
                elif step.index == current_idx:
                    if is_stale:
                        status = f"[bold red]\u25a0 STALE[/] [dim]{duration_str}[/]"
                    elif state.paused_for_manual and step.step_type == "gate":
                        status = f"[bold yellow]\u25b6 GATE[/] [dim]{duration_str}[/]"
                    else:
                        status = f"[bold yellow]\u25b6 RUNNING[/] [green]{duration_str}[/]"
                else:
                    status = "[dim]\u00b7[/]"
            else:
                status = "[dim]\u00b7[/]"

            # Phase color
            if step.phase == "Planning":
                phase = f"[blue]{step.phase}[/]"
            elif step.phase == "TDD RED":
                phase = f"[red]{step.phase}[/]"
            else:
                phase = f"[green]{step.phase}[/]"

            # Model column — show active model on current row
            model = "[dim]-[/]"
            if state and state.active and state.model:
                if step.index == current_idx:
                    model = f"[bold cyan]{state.model}[/]"
                elif step.index < current_idx:
                    model = f"[dim]{state.model}[/]"

            # Bold current row
            idx_str = str(step.index + 1)
            name = step.id
            agent = step.agent
            stype = step.step_type

            if state and state.active and step.index == current_idx:
                idx_str = f"[bold yellow]{idx_str}[/]"
                name = f"[bold yellow]{name}[/]"
                agent = f"[bold yellow]{agent}[/]"
                stype = f"[bold yellow]{stype}[/]"
            elif state and state.active and step.index < current_idx:
                name = f"[dim]{name}[/]"
                agent = f"[dim]{agent}[/]"
                stype = f"[dim]{stype}[/]"

            table.add_row(idx_str, name, phase, agent, stype, model, status)
