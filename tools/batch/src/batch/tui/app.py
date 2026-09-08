from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import override

from textual.app import App
from textual.screen import Screen

from batch.dashboard import Driving, Keying
from batch.models import RunResult
from batch.tui.screen import FETCH_INTERVAL, REFRESH_INTERVAL, DashboardScreen


class DashboardApp(App[None]):
    """`batch run`'s host for the dashboard: the screen is the whole app."""

    def __init__(
        self,
        targets: Sequence[int],
        orchestrator: Driving,
        *,
        prog: str,
        verbs: Keying | None = None,
        narration: Sequence[str] = (),
        drive: Callable[[], RunResult] | None = None,
        interval: float = REFRESH_INTERVAL,
        fetch_interval: float = FETCH_INTERVAL,
    ) -> None:
        super().__init__()
        self.dashboard: DashboardScreen = DashboardScreen(
            targets,
            orchestrator,
            prog=prog,
            verbs=verbs,
            narration=narration,
            drive=drive,
            interval=interval,
            fetch_interval=fetch_interval,
        )

    @override
    def get_default_screen(self) -> Screen[None]:
        return self.dashboard

    @property
    def result(self) -> RunResult | None:
        return self.dashboard.result

    @property
    def failure(self) -> BaseException | None:
        return self.dashboard.failure


def run_dashboard(
    targets: Sequence[int],
    orchestrator: Driving,
    narration: Sequence[str] = (),
    drive: Callable[[], RunResult] | None = None,
    verbs: Keying | None = None,
    *,
    prog: str,
) -> RunResult | None:
    """None means the developer quit before the batch finished."""
    app = DashboardApp(
        targets, orchestrator, prog=prog, verbs=verbs, narration=narration, drive=drive
    )
    app.run()
    if app.failure is not None:
        raise app.failure
    return app.result
