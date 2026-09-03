from __future__ import annotations

import fcntl
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

LOCK_NAME = "run.lock"


class BatchInProgressError(RuntimeError):
    def __init__(self, path: Path, activity: str = "run") -> None:
        super().__init__(f"another batch {activity} holds {path}")
        self.path: Path = path


@contextmanager
def _lock(root: Path, name: str, activity: str) -> Generator[Path]:
    """One of each activity at a time.

    An advisory lock, not a state file: the kernel drops it when the
    process dies, so a crashed run never leaves a batch unstartable.
    """
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BatchInProgressError(path, activity) from exc
        yield path


def run_lock(root: Path) -> AbstractContextManager[Path]:
    return _lock(root, LOCK_NAME, "run")
