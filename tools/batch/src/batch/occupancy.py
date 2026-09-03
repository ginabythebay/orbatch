from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path

from batch.models import OccupancyError

LSOF = ("lsof", "-a", "-d", "cwd", "-Fn")


def probe_output(command: Sequence[str]) -> str:
    """Stdout of `command`; raises OccupancyError if it cannot run or exits
    non-zero."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise OccupancyError(f"Could not run {command[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise OccupancyError(f"{command[0]} exited {result.returncode}: {detail}")
    return result.stdout


def parse_cwds(text: str) -> Iterator[Path]:
    """The cwd of every process in `lsof -Fn` output, one per `p`-headed record."""
    pending = False
    for line in text.splitlines():
        if line.startswith("p"):
            pending = True
        elif pending and line.startswith("n"):
            pending = False
            yield Path(line[1:])


def _lsof_cwds() -> Iterator[Path]:
    return parse_cwds(probe_output(LSOF))


def occupied_slots(
    root: Path, *, cwds: Callable[[], Iterable[Path]] = _lsof_cwds
) -> frozenset[str]:
    base = root.resolve()
    found = (_slot(base, cwd) for cwd in cwds())
    return frozenset(name for name in found if name is not None)


def _slot(base: Path, cwd: Path) -> str | None:
    resolved = cwd.resolve()
    if base not in resolved.parents:
        return None
    return resolved.relative_to(base).parts[0]
