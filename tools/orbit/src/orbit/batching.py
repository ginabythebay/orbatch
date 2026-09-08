"""What orbit needs from batch, behind a protocol so the TUI can be driven
against a fake, plus the loader that says whether this repo is set up."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path
from subprocess import CalledProcessError
from typing import Protocol

from batch.config import ConfigError
from batch.models import ApproveResult, QueueResult
from batch.runtime import Drive, PlanOutcome, Runtime
from batch.stack import main_repo


class BatchVerb(StrEnum):
    QUEUE = "queue"
    PLAN = "plan"
    APPROVE = "approve"
    FAST_TRACK = "fast-track"
    UNQUEUE = "unqueue"
    RUN = "run"


class Batching(Protocol):
    run_root: Path
    prog: str

    def queue(self, targets: Sequence[int]) -> QueueResult: ...
    def unqueue(self, targets: Sequence[int]) -> QueueResult: ...
    def approve(self, targets: Sequence[int]) -> ApproveResult: ...
    def fast_track(self, targets: Sequence[int]) -> ApproveResult: ...
    def plan_session(self, targets: Sequence[int]) -> PlanOutcome: ...
    def drive(self, targets: Sequence[int], report: Callable[[str], None]) -> Drive: ...


def load_batching(repo: Path | None = None) -> Batching | None:
    """None when the checkout carries no usable `batch.toml`, or is no checkout."""
    try:
        return Runtime.load(main_repo(repo))
    except (ConfigError, CalledProcessError):
        return None
