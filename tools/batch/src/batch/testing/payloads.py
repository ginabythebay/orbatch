from __future__ import annotations

import shutil
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from subprocess import CalledProcessError

import pytest

from batch.config import CONFIG_FILENAME, BatchConfig, Commands
from batch.github.client import BatchGitHub
from batch.models import (
    Alignment,
    AlreadyRunningError,
    Batch,
    BatchIssue,
    BatchLabel,
    CiStatus,
    DroppedChild,
    LabelState,
    Problem,
    RemoveResult,
    Slot,
    StaleSlotError,
    TeardownSkip,
    UnsafeRemovalError,
    Verdict,
    VmSession,
    VmStatus,
)
from batch.polling import SettledTargets
from batch.state import BatchState
from batch.verify import Verifier
from batch.vm import GuestAccount, VmRunner
from ghgql.fake import FakeTransport, Response
from ghgql.repo import Repo
from ghgql.transport import GitHubGraphQL

REPO = Repo("acme", "widgets")


def fake_account(login: str = "acme") -> GuestAccount:
    return GuestAccount(login=lambda _token: login)


TEST_SLUG = "acme/widgets"
TEST_SEED = "/images/from-batch-toml.raw"
TEST_AUTHOR_NAME = "Ada Lovelace"
TEST_AUTHOR_EMAIL = "ada@example.com"
TEST_TOKEN_ITEM = "acme-guest-token"
TEST_REPO_TOML = (
    "[repo]\n"
    f'slug = "{TEST_SLUG}"\n'
    f'author_name = "{TEST_AUTHOR_NAME}"\n'
    f'author_email = "{TEST_AUTHOR_EMAIL}"\n'
    f'github_token_item = "{TEST_TOKEN_ITEM}"\n'
)
TEST_COMMANDS = Commands(
    cli="bin/acme",
    setup="tools/prepare",
    session="tools/session",
    agent="tools/drive",
    plan_batch="tools/plan",
)
TEST_COMMANDS_TOML = (
    "[commands]\n"
    f'cli = "{TEST_COMMANDS.cli}"\n'
    f'setup = "{TEST_COMMANDS.setup}"\n'
    f'session = "{TEST_COMMANDS.session}"\n'
    f'agent = "{TEST_COMMANDS.agent}"\n'
    f'plan_batch = "{TEST_COMMANDS.plan_batch}"\n'
)


def batch_config(
    slug: str = TEST_SLUG, commands: Commands = TEST_COMMANDS
) -> BatchConfig:
    return BatchConfig(
        seed_image=Path("/images/seed.raw"),
        slug=slug,
        author_name=TEST_AUTHOR_NAME,
        author_email=TEST_AUTHOR_EMAIL,
        github_token_item=TEST_TOKEN_ITEM,
        commands=commands,
    )


def write_config(root: Path, text: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _ = (root / CONFIG_FILENAME).write_text(
        text
        if text is not None
        else f'[vm]\nseed_image = "{TEST_SEED}"\n' + TEST_REPO_TOML + TEST_COMMANDS_TOML
    )
    return root


def _repo_is(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    def resolved(_repo: Path | None = None) -> Path:
        return root

    monkeypatch.setattr("batch.cli.main_repo", resolved)


def config_at(
    monkeypatch: pytest.MonkeyPatch, root: Path, text: str | None = None
) -> Path:
    """Point `main_repo` at `root` and give it a batch.toml to read."""
    _ = write_config(root, text)
    _repo_is(monkeypatch, root)
    return root


def no_config_at(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    """Point `main_repo` at a directory holding no batch.toml."""
    root.mkdir(parents=True, exist_ok=True)
    _repo_is(monkeypatch, root)
    return root


def no_git_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point `repo` at the failure it raises when `git remote get-url` fails."""

    def no_remote() -> Repo:
        message = "fatal: not a git repository (or any of the parent directories)"
        raise RuntimeError(f"Failed to determine repository: {message}")

    monkeypatch.setattr("batch.cli.repo", no_remote)


def outside_a_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both git probes fail, as they do in a real invocation from outside a
    checkout: stubbing only `main_repo` lets `repo()` succeed against the
    checkout pytest itself runs in."""

    def absent(_repo: Path | None = None) -> Path:
        raise CalledProcessError(128, ("git", "rev-parse", "--show-toplevel"))

    monkeypatch.setattr("batch.cli.main_repo", absent)
    no_git_remote(monkeypatch)


EPIC = 1492
EPIC_TITLE = "Batch workflow"


def child(
    number: int,
    *,
    state: str = "OPEN",
    title: str = "",
    labels: Sequence[str] = (),
    body: str = "",
    closing_prs: Sequence[bool] = (),
) -> Mapping[str, object]:
    """`closing_prs` is one `merged` flag per linked closing PR."""
    return {
        "id": f"I_{number}",
        "number": number,
        "state": state,
        "title": title or f"Issue {number}",
        "body": body,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "closedByPullRequestsReferences": {
            "nodes": [{"merged": merged} for merged in closing_prs]
        },
    }


def target(
    node: Mapping[str, object], *members: Mapping[str, object]
) -> Mapping[str, object]:
    """One target node: an epic when members are given, standalone otherwise."""
    return {
        **node,
        "subIssues": {"totalCount": len(members), "nodes": list(members)},
    }


def epic(
    *members: Mapping[str, object],
    number: int = EPIC,
    title: str = EPIC_TITLE,
    state: str = "OPEN",
) -> Mapping[str, object]:
    return target(child(number, title=title, state=state), *members)


BUDGET = {
    "cost": 2,
    "remaining": 4998,
    "limit": 5000,
    "resetAt": "2026-08-21T13:00:00Z",
}


def targets(*nodes: Mapping[str, object]) -> Mapping[str, object]:
    return {
        "rateLimit": BUDGET,
        "repository": {f"t{node['number']}": node for node in nodes},
    }


def children(
    *nodes: Mapping[str, object], title: str = EPIC_TITLE, state: str = "OPEN"
) -> Mapping[str, object]:
    """The one-epic-target response most tests want."""
    return targets(epic(*nodes, title=title, state=state))


def standalone(*nodes: Mapping[str, object]) -> Mapping[str, object]:
    return targets(*(target(node) for node in nodes))


def issue(node: Mapping[str, object]) -> Mapping[str, object]:
    return {"repository": {"issue": node}}


def missing(*numbers: int) -> Mapping[str, object]:
    return {"repository": {f"t{number}": None for number in numbers}}


def label_ids(missing: Sequence[str] = ()) -> Mapping[str, object]:
    aliases = ("queued", "planned", "implementing", "readyForReview", "stuck")
    return {
        "repository": {
            alias: None if alias in missing else {"id": f"LA_{alias}"}
            for alias in aliases
        }
    }


def fetches(fake: FakeTransport) -> int:
    """How many target queries the fake saw."""
    return sum("fragment core" in call.query_text for call in fake.calls)


def label_writes(fake: FakeTransport) -> list[tuple[str, str, str]]:
    """Every label mutation the fake saw, as (verb, issue node id, label id)."""
    writes: list[tuple[str, str, str]] = []
    for call in fake.calls:
        if "addLabelsToLabelable" in call.query_text:
            verb = "add"
        elif "removeLabelsFromLabelable" in call.query_text:
            verb = "remove"
        else:
            continue
        writes.append(
            (verb, str(call.variables["labelableId"]), str(call.variables["labelId"]))
        )
    return writes


def body_writes(fake: FakeTransport) -> list[tuple[str, str]]:
    """Every body mutation the fake saw, as (issue node id, body)."""
    return [
        (str(call.variables["issueId"]), str(call.variables["body"]))
        for call in fake.calls
        if "updateIssue" in call.query_text
    ]


def pull_request(
    number: int,
    *,
    base: str = "issue-8",
    state: str = "OPEN",
    body: str = "",
    ci: str | None = None,
    created_at: str = "2026-08-07T12:00:00Z",
) -> Mapping[str, object]:
    rollup = None if ci is None else {"state": ci}
    return {
        "number": number,
        "state": state,
        "baseRefName": base,
        "createdAt": created_at,
        "body": body,
        "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]},
    }


def pull_requests(*nodes: Mapping[str, object]) -> Mapping[str, object]:
    return {"repository": {"pullRequests": {"nodes": list(nodes)}}}


def transport(*responses: Response) -> FakeTransport:
    return FakeTransport(responses)


def client_over(fake: FakeTransport) -> BatchGitHub:
    return BatchGitHub(GitHubGraphQL(fake), REPO)


def state_over(fake: FakeTransport) -> BatchState:
    return BatchState(BatchGitHub(GitHubGraphQL(fake), REPO))


def state(*responses: Response) -> BatchState:
    return state_over(transport(*responses))


class FakeClock:
    def __init__(self) -> None:
        self.now: float = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def verifier_over(
    fake: FakeTransport,
    clock: FakeClock | None = None,
    interval: float = 30.0,
) -> Verifier:
    ticker = clock or FakeClock()
    return Verifier(
        BatchGitHub(GitHubGraphQL(fake), REPO),
        sleep=ticker.sleep,
        monotonic=ticker.monotonic,
        interval=interval,
    )


def verifier(*responses: Response) -> Verifier:
    return verifier_over(transport(*responses))


def batch_issue(
    number: int,
    state: BatchLabel = BatchLabel.PLANNED,
    *,
    title: str = "",
    body: str = "",
) -> BatchIssue:
    return BatchIssue(
        number=number, title=title or f"Issue {number}", state=state, body=body
    )


def closed_child(number: int, label: BatchLabel = BatchLabel.PLANNED) -> DroppedChild:
    return DroppedChild(
        number=number,
        title=f"Issue {number}",
        state="CLOSED",
        labels=(label,),
        reason=f"closed, labelled {label}",
    )


def unlabeled_child(number: int) -> DroppedChild:
    return DroppedChild(
        number=number,
        title=f"Issue {number}",
        state="OPEN",
        labels=(),
        reason="no batch label",
    )


class FakeState:
    """The batch as the orchestrator sees it, with transitions applied in place."""

    def __init__(
        self,
        *issues: BatchIssue,
        journal: list[str] | None = None,
        dropped: Sequence[DroppedChild] = (),
        queued_targets: Sequence[int] = (),
    ) -> None:
        self.issues: list[BatchIssue] = list(issues)
        self.queued_targets: tuple[int, ...] = tuple(queued_targets)
        self.dropped: tuple[DroppedChild, ...] = tuple(dropped)
        self.closed: list[BatchIssue] = []
        self.merged: set[int] = set()
        self.swept: list[int] = []
        self.transitions: list[tuple[int, BatchLabel]] = []
        self.fetches: int = 0
        self.settled: list[SettledTargets | None] = []
        self.on_fetch: Callable[[FakeState], None] | None = None
        self.journal: list[str] = [] if journal is None else journal

    def batch(
        self, targets: Sequence[int], *, settled: SettledTargets | None = None
    ) -> Batch:
        self.settled.append(settled)
        self.fetches += 1
        if self.on_fetch is not None:
            self.on_fetch(self)
        return Batch(
            targets=tuple(targets), issues=tuple(self.issues), dropped=self.dropped
        )

    def waiting_targets(self, targets: Sequence[int]) -> tuple[int, ...]:
        return tuple(number for number in targets if number in self.queued_targets)

    def set_state(self, issue_number: int, label: BatchLabel) -> None:
        self.transitions.append((issue_number, label))
        self.journal.append(f"label #{issue_number} {label}")
        self.issues = [
            issue.model_copy(update={"state": label})
            if issue.number == issue_number
            else issue
            for issue in self.issues
        ]

    def label_state(self, issue_number: int) -> LabelState:
        closed = next(
            (issue for issue in self.closed if issue.number == issue_number), None
        )
        if closed is not None:
            return LabelState(
                label=closed.state,
                closed=True,
                closed_by_merge=issue_number in self.merged,
            )
        return LabelState(label=self.states().get(issue_number))

    def clear_state(self, issue_number: int) -> None:
        self.journal.append(f"clear #{issue_number}")
        self.issues = [issue for issue in self.issues if issue.number != issue_number]
        self.closed = [issue for issue in self.closed if issue.number != issue_number]
        self.merged.discard(issue_number)

    def close(self, issue_number: int, *, merged: bool = False) -> None:
        """Closing takes the issue out of the batch but leaves it labeled;
        `merged` is what tells a merge-close from a hand-close."""
        if merged:
            self.merged.add(issue_number)
        self.closed += [issue for issue in self.issues if issue.number == issue_number]
        self.issues = [issue for issue in self.issues if issue.number != issue_number]

    def finished(self, targets: Sequence[int]) -> tuple[BatchIssue, ...]:
        self.swept += list(targets)
        return tuple(self.closed)

    def states(self) -> dict[int, BatchLabel]:
        return {issue.number: issue.state for issue in self.issues}

    def write_body(self, issue_number: int, body: str) -> None:
        self.issues = [
            issue.model_copy(update={"body": body})
            if issue.number == issue_number
            else issue
            for issue in self.issues
        ]


class FakeStack:
    def __init__(
        self,
        root: Path,
        *,
        dirty: Sequence[str] = (),
        unpushed: Sequence[str] = (),
        absent: Sequence[str] = (),
        slots: Sequence[str] = (),
        unmerged: Sequence[str] = (),
        journal: list[str] | None = None,
        seed_image: Path | None = None,
        switched: Mapping[str, str] | None = None,
    ) -> None:
        self.root: Path = root
        self.seed_image: Path | None = seed_image
        self._switched: dict[str, str] = dict(switched or {})
        self.ensured: list[tuple[int, str]] = []
        self.branches: list[tuple[str, str]] = []
        self.currented: list[tuple[str, str]] = []
        self.found: list[tuple[str, str]] = []
        self.stale: str | None = None
        self.alignment: Alignment = Alignment.ALIGNED
        self.removed: list[int] = []
        self.removed_branches: list[str] = []
        self.journal: list[str] = [] if journal is None else journal
        self._dirty: set[str] = set(dirty)
        self._unpushed: set[str] = set(unpushed)
        self._absent: set[str] = set(absent)
        self._slots: tuple[str, ...] = tuple(slots)
        self._unmerged: set[str] = set(unmerged)
        self._gone: set[str] = set()
        self.bases: list[str] = []

    @property
    def worktree_root(self) -> Path:
        return self.root / "widgets" / "worktrees"

    @property
    def mount_root(self) -> Path:
        return self.root

    def slot_names(self) -> tuple[str, ...]:
        return tuple(name for name in self._slots if name not in self._gone)

    def merged_into(self, branch: str, base: str) -> bool:
        self.bases.append(base)
        return branch not in self._unmerged

    def dirty(self, branch: str) -> bool:
        return branch in self._dirty

    def unpushed(self, branch: str) -> bool:
        return branch in self._unpushed

    def checked_out(self, branch: str) -> str | None:
        return self._switched.get(branch, branch)

    def missing(self, branch: str) -> tuple[str, ...]:
        if branch not in self._absent:
            return ()
        return (
            f"no worktree at {self.worktree_root / branch}",
            f"no disk at {self.worktree_root / f'{branch}.raw'}",
        )

    def find(self, branch: str, base: str) -> Slot | None:
        self.found.append((branch, base))
        if self.missing(branch):
            return None
        return Slot(
            branch=branch,
            worktree=self.worktree_root / branch,
            disk=self.worktree_root / f"{branch}.raw",
            alignment=self.alignment,
        )

    def remove(self, issue: int, *, force: bool = False) -> RemoveResult:
        if not force:
            self._refuse_if_unsafe(f"issue-{issue}")
        self.removed.append(issue)
        self.journal.append(f"remove #{issue}{' forced' if force else ''}")
        return RemoveResult(
            branch=f"issue-{issue}",
            removed_worktree=True,
            removed_branch=True,
            removed_disk=True,
        )

    def _refuse_if_unsafe(self, branch: str) -> None:
        if branch in self._dirty:
            raise UnsafeRemovalError(
                branch,
                TeardownSkip.DIRTY_WORKTREE,
                "the worktree has local changes",
            )
        if branch in self._unpushed:
            raise UnsafeRemovalError(
                branch,
                TeardownSkip.UNPUSHED_COMMITS,
                "the branch has unpushed commits",
            )

    def remove_branch(self, branch: str, *, force: bool = False) -> RemoveResult:
        if not force:
            self._refuse_if_unsafe(branch)
        self.removed_branches.append(branch)
        self.journal.append(f"remove {branch}{' forced' if force else ''}")
        present = branch not in self._absent and branch not in self._gone
        self._gone.add(branch)
        return RemoveResult(
            branch=branch,
            removed_worktree=present,
            removed_branch=present,
            removed_disk=present,
        )

    def ensure_branch(self, branch: str, base: str) -> Slot:
        self.branches.append((branch, base))
        return Slot(
            branch=branch,
            worktree=self.worktree_root / branch,
            disk=self.worktree_root / f"{branch}.raw",
            alignment=self.alignment,
        )

    def ensure_current(self, branch: str, base: str) -> Slot:
        self.currented.append((branch, base))
        if self.stale is not None:
            raise StaleSlotError(branch, self.stale)
        return Slot(
            branch=branch,
            worktree=self.worktree_root / branch,
            disk=self.worktree_root / f"{branch}.raw",
            alignment=self.alignment,
        )

    def ensure(self, issue: int, base: str) -> Slot:
        self.ensured.append((issue, base))
        branch = f"issue-{issue}"
        return Slot(
            branch=branch,
            worktree=self.worktree_root / branch,
            disk=self.worktree_root / f"{branch}.raw",
            alignment=Alignment.ALIGNED,
        )


class FakeRunner:
    """Reports RUNNING for a per-issue number of polls, then EXITED."""

    def __init__(
        self,
        root: Path,
        *,
        polls: Mapping[int, int] | None = None,
        live: Sequence[int] = (),
        live_branches: Sequence[str] = (),
        staging_error: OSError | None = None,
        journal: list[str] | None = None,
        claims: Mapping[str, int] | None = None,
    ) -> None:
        self.root: Path = root
        self._claims: dict[str, int] = dict(claims or {})
        self._staging_error: OSError | None = staging_error
        self._polls: dict[int, int] = dict(polls or {})
        self._live: set[int] = set(live)
        self._live_branches: set[str] = set(live_branches)
        self.launched: list[tuple[int, VmSession]] = []
        self.staged: list[tuple[Path, bool]] = []
        self.cleaned: list[int] = []
        self.cleaned_configs: list[Path] = []
        self.journal: list[str] = [] if journal is None else journal

    def config_dir(self, issue: int) -> Path:
        return self.root / f"issue-{issue}.config"

    def log(self, issue: int) -> Path:
        return self.root / f"issue-{issue}.log"

    def attach_command(self, issue: int) -> tuple[str, ...]:
        return ("dtach", "-a", str(self.root / f"issue-{issue}.sock"), "-r", "none")

    def debug_command(self, issue: int, session: VmSession) -> tuple[str, ...]:
        """Argv from the real builder: what the fake stands in for is the spawn."""
        return VmRunner(self.root, config=batch_config).debug_command(issue, session)

    def clean(self, issue: int) -> bool:
        self.cleaned.append(issue)
        self.journal.append(f"clean #{issue}")
        return True

    def named_config_dir(self, name: str) -> Path:
        return self.root / f"{name}.config"

    def clean_config(self, config_dir: Path) -> bool:
        self.cleaned_configs.append(config_dir)
        self.journal.append(f"clean {config_dir.name}")
        if not config_dir.is_dir():
            return False
        shutil.rmtree(config_dir)
        return True

    def write_config(self, config_dir: Path, *, headless: bool = False) -> None:
        if self._staging_error is not None:
            raise self._staging_error
        self.staged.append((config_dir, headless))

    def launch(self, issue: int, session: VmSession) -> None:
        if issue in self._live:
            raise AlreadyRunningError(issue)
        self.launched.append((issue, session))
        self.journal.append(f"launch #{issue}")

    def status(self, issue: int) -> VmStatus:
        remaining = self._polls.get(issue, 1)
        if remaining <= 0:
            return VmStatus.EXITED
        self._polls[issue] = remaining - 1
        return VmStatus.RUNNING

    def status_branch(self, branch: str) -> VmStatus:
        return VmStatus.RUNNING if branch in self._live_branches else VmStatus.EXITED

    def claim_pid(self, branch: str) -> int | None:
        return self._claims.get(branch)

    def release_claim(self, branch: str) -> None:
        _ = self._claims.pop(branch, None)

    def agents(self) -> list[str]:
        return [session.agent for _, session in self.launched]


class FakeVms:
    """Liveness alone: the socket check recovery and crash detection consult."""

    def __init__(self, live: Sequence[int] = ()) -> None:
        self.live: set[int] = set(live)

    def status(self, issue: int) -> VmStatus:
        return VmStatus.RUNNING if issue in self.live else VmStatus.EXITED


def green(issue_number: int, bases: tuple[str, ...]) -> Verdict:
    return Verdict(
        issue_number=issue_number,
        expected_bases=bases,
        pr_number=100 + issue_number,
        base=bases[0],
        ci=CiStatus.GREEN,
    )


def red(issue_number: int, bases: tuple[str, ...]) -> Verdict:
    return Verdict(
        issue_number=issue_number,
        expected_bases=bases,
        problems=(Problem.NO_PR,),
    )


def still_running(issue_number: int, bases: tuple[str, ...]) -> Verdict:
    return Verdict(
        issue_number=issue_number,
        expected_bases=bases,
        pr_number=100 + issue_number,
        base=bases[0],
        ci=CiStatus.PENDING,
    )


class FakeVerifier:
    def __init__(
        self,
        failing: Sequence[int] = (),
        pending: Sequence[int] = (),
        merged: Sequence[int] = (),
    ) -> None:
        self._failing: set[int] = set(failing)
        self._pending: set[int] = set(pending)
        self._merged: set[int] = set(merged)
        self.asked: list[tuple[int, tuple[str, ...]]] = []
        self.merge_queries: list[int] = []
        self.waits: list[timedelta | None] = []

    def merged(self, issue_number: int) -> bool:
        self.merge_queries.append(issue_number)
        return issue_number in self._merged

    def verify(
        self,
        issue_number: int,
        bases: tuple[str, ...],
        wait: timedelta | None = None,
    ) -> Verdict:
        self.asked.append((issue_number, bases))
        self.waits.append(wait)
        if issue_number in self._failing:
            return red(issue_number, bases)
        if issue_number in self._pending:
            return still_running(issue_number, bases)
        return green(issue_number, bases)
