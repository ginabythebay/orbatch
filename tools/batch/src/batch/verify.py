from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta

from batch.github.client import BatchGitHub
from batch.models import CiStatus, Problem, PullRequest, Verdict

DEFAULT_INTERVAL = 30.0
_TERMINAL = frozenset({Problem.NO_PR, Problem.PR_CLOSED})


class Verifier:
    """The only verdict on whether an issue is done; see
    docs/adr/0008-completion-is-verified-not-reported.md."""

    def __init__(
        self,
        client: BatchGitHub,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        interval: float = DEFAULT_INTERVAL,
    ) -> None:
        self._client: BatchGitHub = client
        self._sleep: Callable[[float], None] = sleep
        self._monotonic: Callable[[], float] = monotonic
        self._interval: float = interval

    def merged(self, issue_number: int) -> bool:
        return any(
            pull.state == "MERGED"
            for pull in self._client.fetch_pull_requests(f"issue-{issue_number}")
        )

    def verify(
        self, issue_number: int, bases: tuple[str, ...], wait: timedelta | None = None
    ) -> Verdict:
        """Re-checks everything per poll: a wrong-base PR must not pass on going green.

        Several bases are accepted because a predecessor merging mid-run makes
        GitHub retarget this PR from `issue-<n>` to that branch's own base.
        """
        deadline = self._monotonic() + (wait.total_seconds() if wait else 0.0)
        while True:
            verdict = self._poll(issue_number, bases)
            if verdict.ci is not CiStatus.PENDING or _TERMINAL.intersection(
                verdict.problems
            ):
                return verdict
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return verdict
            self._sleep(min(self._interval, remaining))

    def _poll(self, issue_number: int, bases: tuple[str, ...]) -> Verdict:
        pulls = self._client.fetch_pull_requests(f"issue-{issue_number}")
        if not pulls:
            return Verdict(
                issue_number=issue_number,
                expected_bases=bases,
                problems=(Problem.NO_PR,),
            )
        newest, *rest = sorted(pulls, key=lambda pull: pull.created_at, reverse=True)
        extra = tuple(pull.number for pull in rest if pull.state == "OPEN")
        return self._judge(issue_number, bases, newest, extra)

    def _judge(
        self,
        issue_number: int,
        bases: tuple[str, ...],
        pull: PullRequest,
        extra: tuple[int, ...],
    ) -> Verdict:
        problems: list[Problem] = []
        if extra:
            problems.append(Problem.EXTRA_PRS)
        if pull.state == "CLOSED":
            problems.append(Problem.PR_CLOSED)
        if pull.base not in bases:
            problems.append(Problem.WRONG_BASE)
        if issue_number not in pull.closes:
            problems.append(Problem.MISSING_ISSUE_REFERENCE)
        return Verdict(
            issue_number=issue_number,
            expected_bases=bases,
            pr_number=pull.number,
            base=pull.base,
            problems=tuple(problems),
            extra_pr_numbers=extra,
            ci=pull.ci,
        )
