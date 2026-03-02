"""PrismDashboard — main Textual TUI application."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Footer, Header, Static

from models import StoryInfo, WorkflowState
from parsing import parse_state_file, parse_story_file
from widgets import (
    AgentRoster,
    GatePanel,
    StepDetail,
    StoryPanel,
    TimingPanel,
    WorkflowTable,
)


class PrismDashboard(App):
    """Live TUI dashboard for the PRISM workflow engine."""

    TITLE = "PRISM Dashboard"
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        path: str | Path | None = None,
        interval: float = 1.0,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._work_dir = Path(path) if path else Path.cwd()
        self._state_file = self._work_dir / ".claude" / "prism-loop.local.md"
        self._interval = interval
        self._state: WorkflowState | None = None
        self._story: StoryInfo | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Container(id="body"):
            yield AgentRoster()
            yield WorkflowTable()
            with Horizontal(id="details"):
                with Vertical(id="left-col"):
                    yield StepDetail()
                    yield StoryPanel()
                with Vertical(id="right-col"):
                    yield TimingPanel()
                    yield GatePanel()
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"Watching: {self._state_file.relative_to(self._work_dir)}"
        self.set_interval(self._interval, self._poll_state)
        self._poll_state()

    def _poll_state(self) -> None:
        """Re-read state file and push to ALL widgets every tick.

        Bypasses Textual's reactive equality check so timers,
        durations, and staleness indicators update in real-time.
        """
        self._state = parse_state_file(self._state_file)

        if self._state and self._state.story_file:
            story_path = Path(self._state.story_file)
            if not story_path.is_absolute():
                story_path = self._work_dir / story_path
            self._story = parse_story_file(story_path)
        else:
            self._story = None

        # Push to every widget on every tick — no reactive gating
        try:
            self.query_one(AgentRoster).update_state(self._state)
            self.query_one(WorkflowTable).update_state(self._state)
            self.query_one(StepDetail).update_state(self._state)
            self.query_one(TimingPanel).update_state(self._state)
            self.query_one(GatePanel).update_state(self._state)
            self.query_one(StoryPanel).update_story(self._story)
        except Exception:
            pass  # Widgets not mounted yet during startup
