"""StoryPanel — AC list, plan coverage summary."""

from __future__ import annotations

from textual.widgets import Static

from models import StoryInfo


class StoryPanel(Static):
    """Displays story file info: path, ACs, plan coverage."""

    DEFAULT_CSS = """
    StoryPanel {
        height: auto;
        min-height: 6;
        padding: 1;
        border: round $primary;
    }
    """

    def on_mount(self) -> None:
        self._refresh_content(None)

    def update_story(self, story: StoryInfo | None) -> None:
        self._refresh_content(story)

    def _refresh_content(self, story: StoryInfo | None) -> None:
        if not story or not story.exists:
            self.update("[bold]Story File[/]\n[dim]No story file[/]")
            return

        lines = ["[bold]Story File[/]"]
        lines.append(f"[dim]{story.path}[/]")

        # ACs
        ac_count = len(story.acceptance_criteria)
        lines.append(f"ACs: {ac_count} found")
        for ac in story.acceptance_criteria[:8]:
            # Truncate long AC text
            display = ac if len(ac) <= 50 else ac[:47] + "..."
            lines.append(f"  {display}")
        if ac_count > 8:
            lines.append(f"  [dim]... and {ac_count - 8} more[/]")

        # Plan coverage
        if story.has_plan_coverage:
            covered = story.covered_count
            missing = story.missing_count
            if missing > 0:
                cov_str = f"[green]{covered} covered[/], [red]{missing} missing[/]"
            else:
                cov_str = f"[green]{covered} covered[/], 0 missing"
            lines.append(f"Coverage: {cov_str}")
        else:
            lines.append("[dim]No plan coverage section[/]")

        self.update("\n".join(lines))
