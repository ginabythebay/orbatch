"""Tests for orbit.core."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from orbit.core import _OPT_OUT_VARS, _spawned, open_url, run_attached, spawn

# _spawned is internal bookkeeping; the reaping behavior it exists for
# has no other observable surface.
# pyright: reportPrivateUsage=false


def _wait_for(marker: Path) -> None:
    """Block until a spawned command has created `marker`."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not marker.exists():
        time.sleep(0.05)
    assert marker.exists()


class TestRunAttached:
    def test_returns_the_commands_exit_code(self) -> None:
        # The suspend-mode status line reports failure off this code, so
        # a swallowed non-zero would report success on a failed command.
        assert run_attached("exit 3") == 3

    def test_returns_zero_on_success(self) -> None:
        assert run_attached("true") == 0

    def test_runs_through_a_shell(self, tmp_path: Path) -> None:
        # Shell syntax (redirection here) has to work: .orbit.toml
        # commands are written expecting a shell.
        marker = tmp_path / "out"
        assert run_attached(f"echo hi > {marker}") == 0
        assert marker.read_text().strip() == "hi"


class TestSpawn:
    def test_runs_the_command_without_waiting(self, tmp_path: Path) -> None:
        marker = tmp_path / "marker"
        spawn(f"sleep 0.2 && touch {marker}")
        # Returned before the command finished: that is the whole point
        # of spawn, and why it can never report an exit code.
        assert not marker.exists()
        _wait_for(marker)

    def test_finished_children_are_reaped(self, tmp_path: Path) -> None:
        # Without reaping, each spawn leaves a zombie for the session and
        # its Popen warns when collected mid-run.
        first = tmp_path / "first"
        spawn(f"touch {first}")
        _wait_for(first)
        second = tmp_path / "second"
        spawn(f"touch {second}")
        _wait_for(second)
        assert all(child.returncode is None for child in _spawned)


class TestOpenUrl:
    @pytest.fixture(autouse=True)
    def _browser_allowed(self) -> Iterator[None]:
        # The suite runs with browser launching opted out; these tests are
        # about what open_url does when it is not.
        with patch.dict(os.environ, dict.fromkeys(_OPT_OUT_VARS, "")):
            yield

    def test_darwin_uses_safari_explicitly(self) -> None:
        # webbrowser.open_new on macOS can hand the URL to a web app
        # instead of the browser; open_url must pin safari there.
        with (
            patch.dict(os.environ, {"BROWSER": ""}),
            patch("orbit.core.platform.system", return_value="Darwin"),
            patch("orbit.core.webbrowser.get") as mock_get,
        ):
            open_url("https://example.com/x")
        mock_get.assert_called_once_with("safari")

    def test_an_explicit_browser_wins_over_safari(self) -> None:
        with (
            patch.dict(os.environ, {"BROWSER": "firefox"}),
            patch("orbit.core.platform.system", return_value="Darwin"),
            patch("orbit.core.webbrowser.open_new") as mock_open,
        ):
            open_url("https://example.com/x")
        mock_open.assert_called_once_with("https://example.com/x")

    def test_other_platforms_use_default_browser(self) -> None:
        with (
            patch("orbit.core.platform.system", return_value="Linux"),
            patch("orbit.core.webbrowser.open_new") as mock_open,
        ):
            open_url("https://example.com/x")
        mock_open.assert_called_once_with("https://example.com/x")
