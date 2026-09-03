from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from ghgql.transport import RateLimit


class BatchLabel(StrEnum):
    QUEUED = "queued"
    PLANNED = "planned"
    IMPLEMENTING = "implementing"
    READY_FOR_REVIEW = "ready-for-review"
    STUCK = "stuck"


class ConflictingLabelsError(RuntimeError):
    def __init__(self, number: int, labels: Sequence[str]) -> None:
        super().__init__(
            f"#{number} carries more than one batch label: {', '.join(labels)}"
        )
        self.number: int = number


class NotAChildError(RuntimeError):
    def __init__(self, number: int, epic_number: int) -> None:
        super().__init__(f"#{number} is not a child of #{epic_number}")
        self.number: int = number


class Epic(BaseModel, frozen=True):
    number: int
    title: str
    state: str


class NoTargetsError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("name at least one epic or issue number")


class ChildIssue(BaseModel, frozen=True):
    node_id: str
    number: int
    state: str
    title: str
    body: str
    labels: tuple[str, ...]
    closed_by_merge: bool


class Target(BaseModel, frozen=True):
    """A number a batch verb was given: an epic, or a standalone issue.

    `members` is what it contributes to the batch — an epic's children, or the
    standalone issue itself.
    """

    number: int
    title: str
    state: str
    members: tuple[ChildIssue, ...] = ()
    epic: bool = False


class BatchIssue(BaseModel, frozen=True):
    number: int
    title: str
    state: BatchLabel
    body: str = ""


class DroppedChild(BaseModel, frozen=True):
    number: int
    title: str
    state: str
    labels: tuple[BatchLabel, ...]
    reason: str
    closed_by_merge: bool = False


class Batch(BaseModel, frozen=True):
    targets: tuple[int, ...]
    issues: tuple[BatchIssue, ...]
    dropped: tuple[DroppedChild, ...] = ()
    rate_limit: RateLimit | None = None

    @property
    def anomalies(self) -> tuple[DroppedChild, ...]:
        """Closed children whose labels contradict their state. A merged close
        explains one leftover label — teardown clears it — but never two."""
        return tuple(
            child
            for child in self.dropped
            if child.state != "OPEN"
            and child.labels
            and not (child.closed_by_merge and len(child.labels) == 1)
        )


class SkippedIssue(BaseModel, frozen=True):
    number: int
    reason: str


class QueueResult(BaseModel, frozen=True):
    epic: Epic | None = None
    labeled: tuple[int, ...]
    skipped: tuple[SkippedIssue, ...]


class ApproveResult(BaseModel, frozen=True):
    epic: Epic | None = None
    approved: tuple[int, ...]
    skipped: tuple[SkippedIssue, ...]
    guidance_refused: tuple[int, ...] = ()


class Problem(StrEnum):
    NO_PR = "no-pr"
    PR_CLOSED = "pr-closed"
    WRONG_BASE = "wrong-base"
    MISSING_ISSUE_REFERENCE = "missing-issue-reference"
    EXTRA_PRS = "extra-prs"


class CiStatus(StrEnum):
    GREEN = "green"
    PENDING = "pending"
    FAILED = "failed"
    UNKNOWN = "unknown"


class PullRequest(BaseModel, frozen=True):
    number: int
    state: str
    base: str
    created_at: str
    closes: tuple[int, ...]
    ci: CiStatus


class Verdict(BaseModel, frozen=True):
    issue_number: int
    expected_bases: tuple[str, ...]
    pr_number: int | None = None
    base: str | None = None
    problems: tuple[Problem, ...] = ()
    extra_pr_numbers: tuple[int, ...] = ()
    ci: CiStatus = CiStatus.UNKNOWN

    @property
    def ok(self) -> bool:
        return not self.problems and self.ci is CiStatus.GREEN


DEFAULT_RAM = 3072


class VmSession(BaseModel, frozen=True):
    worktree: str
    disk: Path
    config_dir: Path
    agent: str
    ram: int = DEFAULT_RAM
    cwd: Path | None = None


class VmFacts(BaseModel, frozen=True):
    live: bool = False
    log: Path | None = None
    config_dir: Path | None = None

    @property
    def any(self) -> bool:
        return self.live or self.log is not None or self.config_dir is not None


class DashboardRow(BaseModel, frozen=True):
    number: int
    title: str
    state: BatchLabel
    live: bool = False
    elapsed: str = ""
    last_line: str = ""


class VmStatus(StrEnum):
    RUNNING = "running"
    EXITED = "exited"


class AlreadyRunningError(RuntimeError):
    def __init__(self, issue: int) -> None:
        super().__init__(f"a VM for #{issue} is already running")
        self.issue: int = issue


class KeychainError(RuntimeError):
    def __init__(self, item: str) -> None:
        remedy = f"security add-generic-password -s {item} -a $USER -w"
        super().__init__(f"no keychain item named {item}; create it with {remedy}")
        self.item: str = item


class EmptyTokenError(RuntimeError):
    def __init__(self, item: str) -> None:
        remedy = f"security add-generic-password -U -s {item} -a $USER -w"
        super().__init__(
            f"keychain item {item} holds an empty password; set it with {remedy}"
        )
        self.item: str = item


class AccountCheckError(RuntimeError):
    """The guest token's account could not be established; a launch must refuse
    rather than boot on an unverified identity."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"could not check the guest token's GitHub account: {reason}")


class WrongAccountError(RuntimeError):
    def __init__(self, item: str, login: str, owner: str) -> None:
        whose = f"{login}, not to {owner}, the owner of this repository"
        super().__init__(f"the token in keychain item {item} belongs to {whose}")
        self.item: str = item
        self.login: str = login
        self.owner: str = owner


class Alignment(StrEnum):
    ALIGNED = "aligned"
    BEHIND = "behind"
    UNRELATED = "unrelated"


class TeardownSkip(StrEnum):
    NOT_MERGED = "not-merged"
    VM_LIVE = "vm-live"
    DIRTY_WORKTREE = "dirty-worktree"
    UNPUSHED_COMMITS = "unpushed-commits"
    OCCUPIED = "occupied"
    CLAIMED = "claimed"
    BRANCH_SWITCHED = "branch-switched"


class UnsafeRemovalError(RuntimeError):
    def __init__(self, branch: str, skip: TeardownSkip, reason: str) -> None:
        super().__init__(f"{branch} is not safe to remove: {reason}")
        self.branch: str = branch
        self.skip: TeardownSkip = skip
        self.reason: str = reason


class UnmountedWorktreeError(RuntimeError):
    def __init__(self, worktree: Path, mount_root: Path) -> None:
        super().__init__(
            f"{worktree} is outside {mount_root}, so the guest would never see it"
        )
        self.worktree: Path = worktree
        self.mount_root: Path = mount_root


class OccupancyError(RuntimeError):
    """A liveness probe could not answer; a sweep must refuse rather than guess."""


class StaleSlotError(RuntimeError):
    def __init__(self, branch: str, reason: str) -> None:
        super().__init__(f"{branch} {reason}")
        self.branch: str = branch
        self.reason: str = reason


class RemoveResult(BaseModel, frozen=True):
    branch: str
    removed_worktree: bool
    removed_branch: bool
    removed_disk: bool

    @property
    def reclaimed(self) -> bool:
        return self.removed_worktree or self.removed_branch or self.removed_disk


class Slot(BaseModel, frozen=True):
    branch: str
    worktree: Path
    disk: Path
    alignment: Alignment


class HaltReason(StrEnum):
    VERIFICATION_FAILED = "verification-failed"
    TIMED_OUT = "timed-out"
    VM_ALREADY_RUNNING = "vm-already-running"
    ORPHANED_VM = "orphaned-vm"
    STUCK_ISSUE = "stuck-issue"


class RecoveryAction(StrEnum):
    REWORK = "rework"
    SKIP = "skip"
    RELAUNCH = "relaunch"


class RecoveryRefusal(StrEnum):
    NOT_IN_BATCH = "not-in-batch"
    NO_BRANCH = "no-branch"
    WRONG_STATE = "wrong-state"
    VM_LIVE = "vm-live"
    CLOSED = "closed"
    MERGED = "merged"


class LabelState(BaseModel, frozen=True):
    label: BatchLabel | None = None
    closed: bool = False
    closed_by_merge: bool = False


class RecoveryResult(BaseModel, frozen=True):
    number: int
    action: RecoveryAction
    found: BatchLabel | None = None
    refusal: RecoveryRefusal | None = None


class IssueOutcome(BaseModel, frozen=True):
    number: int
    base: str
    state: BatchLabel
    verdict: Verdict | None = None
    halt: HaltReason | None = None


class TeardownOutcome(BaseModel, frozen=True):
    number: int
    skip: TeardownSkip | None = None


class TeardownResult(BaseModel, frozen=True):
    targets: tuple[int, ...] = ()
    outcomes: tuple[TeardownOutcome, ...] = ()

    @property
    def cleaned(self) -> tuple[int, ...]:
        return tuple(
            outcome.number for outcome in self.outcomes if outcome.skip is None
        )


class ReclaimOutcome(BaseModel, frozen=True):
    branch: str
    skip: TeardownSkip | None = None


class ReclaimResult(BaseModel, frozen=True):
    outcomes: tuple[ReclaimOutcome, ...] = ()
    dry_run: bool = False


class NextIssue(BaseModel, frozen=True):
    number: int
    title: str
    body: str
    predecessors: tuple[int, ...] = ()


class DebugRefusal(StrEnum):
    NO_SLOT = "no-slot"
    BOOT_FAILED = "boot-failed"


class DebugEntry(BaseModel, frozen=True):
    """`boot` is the argv the VM was started with, empty when it was already up."""

    number: int
    command: tuple[str, ...] = ()
    boot: tuple[str, ...] = ()
    refusal: DebugRefusal | None = None
    missing: tuple[str, ...] = ()


class PlanRefusal(StrEnum):
    NO_PLAN = "no-plan"
    WRONG_STATE = "wrong-state"
    NOT_IN_BATCH = "not-in-batch"


class PlanWritten(BaseModel, frozen=True):
    number: int
    state: BatchLabel | None = None
    refusal: PlanRefusal | None = None


class RunResult(BaseModel, frozen=True):
    targets: tuple[int, ...] = ()
    outcomes: tuple[IssueOutcome, ...] = ()
    anomalies: tuple[DroppedChild, ...] = ()

    @property
    def halted(self) -> HaltReason | None:
        return next(
            (outcome.halt for outcome in self.outcomes if outcome.halt is not None),
            None,
        )
