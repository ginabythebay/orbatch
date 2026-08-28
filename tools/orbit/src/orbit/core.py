"""Shared low-level helpers usable from any orbit module.

Home for utilities that both the CLI and the TUI need; keeping them
here avoids importing one entry point from the other.
"""

from __future__ import annotations

import os
import platform
import subprocess
import webbrowser

# Spawned children, kept alive deliberately: a Popen collected while
# its child still runs emits ResourceWarning, and an unwaited child
# stays a zombie. Holding them lets each spawn reap its predecessors.
_spawned: list[subprocess.Popen[bytes]] = []


def spawn(command: str) -> None:
    """Launch `command` in the background and return immediately.

    `shell=True` is the feature, not an oversight: `command` comes from
    a project's `.orbit.toml` and is expected to use quoting, `~` and
    redirection. `start_new_session` detaches it so it outlives orbit.

    Nothing is reported back: by the time the command fails, orbit has
    long since returned. Callers that need an exit code want
    `run_attached`.
    """
    # poll() reaps whatever has exited, so the list tracks only live
    # children rather than growing for the whole session.
    _spawned[:] = [child for child in _spawned if child.poll() is None]
    _spawned.append(subprocess.Popen(command, shell=True, start_new_session=True))


def run_attached(command: str) -> int:
    """Run `command` on the current tty, wait, and return its exit code."""
    return subprocess.run(command, shell=True, check=False).returncode


NO_BROWSER_VAR = "ORBIT_NO_BROWSER"
_OPT_OUT_VARS = (NO_BROWSER_VAR, "CI")


def open_url(url: str) -> None:
    """Open `url` in a browser, unless browser launching is opted out of.

    Suppression has to happen here rather than through `$BROWSER`: when
    the browser named there is unusable, `webbrowser.open` falls through
    to the next entry of its `_tryorder`, which on macOS drives
    `osascript` and fronts Safari.
    """
    if any(os.environ.get(name) for name in _OPT_OUT_VARS):
        return
    if platform.system() == "Darwin" and not os.environ.get("BROWSER"):
        # sometimes webbrowser.open_new(url) opens a web app instead 'the browser'
        webbrowser.get("safari").open_new(url)
    else:
        webbrowser.open_new(url)
