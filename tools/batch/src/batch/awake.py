from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from typing import Protocol

CAFFEINATE = ("caffeinate", "-i", "-w")
TERMINATE_TIMEOUT = 5.0


class Child(Protocol):
    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


def _spawn(argv: Sequence[str]) -> Child:
    return subprocess.Popen(argv)


@contextmanager
def awake(
    report: Callable[[str], None],
    *,
    spawn: Callable[[Sequence[str]], Child] = _spawn,
    platform: str = sys.platform,
) -> Generator[None]:
    if platform != "darwin":
        yield
        return
    try:
        child = spawn([*CAFFEINATE, str(os.getpid())])
    except OSError as exc:
        report(f"caffeinate did not start ({exc}); idle sleep may stop the run")
        yield
        return
    try:
        yield
    finally:
        child.terminate()
        try:
            _ = child.wait(timeout=TERMINATE_TIMEOUT)
        except subprocess.TimeoutExpired:
            report(f"caffeinate did not exit within {TERMINATE_TIMEOUT}s")
