from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

import pytest

from batch.models import CiStatus, Problem
from batch.testing.payloads import (
    FakeClock,
    pull_request,
    pull_requests,
    transport,
    verifier,
    verifier_over,
)
from ghgql.fake import Errors


class TestPullRequestDiscovery:
    def test_an_open_pr_on_the_right_base_with_green_ci_verifies(self) -> None:
        response = pull_requests(
            pull_request(101, base="issue-8", body="Fixes #9", ci="SUCCESS")
        )

        verdict = verifier(response).verify(9, ("issue-8",))

        assert verdict.ok
        assert verdict.pr_number == 101
        assert verdict.problems == ()
        assert verdict.ci is CiStatus.GREEN

    def test_no_pr_on_the_branch_leaves_everything_else_unjudged(self) -> None:
        verdict = verifier(pull_requests()).verify(9, ("issue-8",))

        assert not verdict.ok
        assert verdict.problems == (Problem.NO_PR,)
        assert verdict.pr_number is None
        assert verdict.ci is CiStatus.UNKNOWN

    def test_the_branch_queried_is_the_stack_managers_issue_branch(self) -> None:
        fake = transport(
            pull_requests(pull_request(101, body="Fixes #9", ci="SUCCESS"))
        )

        _ = verifier_over(fake).verify(9, ("issue-8",))

        assert [call.variables["headRefName"] for call in fake.calls] == ["issue-9"]
        assert "body" in fake.calls[0].query_text

    def test_the_newest_of_several_prs_is_judged_and_the_rest_reported(self) -> None:
        response = pull_requests(
            pull_request(
                101,
                base="issue-8",
                body="Fixes #9",
                ci="FAILURE",
                created_at="2026-08-01T00:00:00Z",
            ),
            pull_request(
                102,
                base="issue-8",
                body="Fixes #9",
                ci="SUCCESS",
                created_at="2026-08-06T00:00:00Z",
            ),
        )

        verdict = verifier(response).verify(9, ("issue-8",))

        assert verdict.pr_number == 102
        assert verdict.ci is CiStatus.GREEN
        assert verdict.problems == (Problem.EXTRA_PRS,)
        assert verdict.extra_pr_numbers == (101,)

    def test_a_merged_pr_still_verifies(self) -> None:
        response = pull_requests(
            pull_request(
                101, base="issue-8", state="MERGED", body="Fixes #9", ci="SUCCESS"
            )
        )

        verdict = verifier(response).verify(9, ("issue-8",))

        assert verdict.ok
        assert verdict.pr_number == 101

    def test_a_closed_pr_is_reported_as_closed_not_missing(self) -> None:
        response = pull_requests(
            pull_request(
                101, base="issue-8", state="CLOSED", body="Fixes #9", ci="SUCCESS"
            )
        )

        verdict = verifier(response).verify(9, ("issue-8",))

        assert verdict.problems == (Problem.PR_CLOSED,)
        assert verdict.pr_number == 101


class TestStaticChecks:
    def test_a_pr_based_on_main_instead_of_the_predecessor_is_flagged(self) -> None:
        response = pull_requests(
            pull_request(101, base="main", body="Fixes #9", ci="SUCCESS")
        )

        verdict = verifier(response).verify(9, ("issue-8",))

        assert verdict.problems == (Problem.WRONG_BASE,)
        assert verdict.base == "main"
        assert verdict.expected_bases == ("issue-8",)

    def test_a_pr_that_closes_only_another_issue_is_flagged(self) -> None:
        response = pull_requests(
            pull_request(101, base="issue-8", body="Fixes #7", ci="SUCCESS")
        )

        verdict = verifier(response).verify(9, ("issue-8",))

        assert verdict.problems == (Problem.MISSING_ISSUE_REFERENCE,)

    def test_a_stacked_pr_is_judged_by_its_body_not_githubs_link(self) -> None:
        response = pull_requests(
            pull_request(
                101, base="issue-8", body="Fixes #9\n\nA summary.", ci="SUCCESS"
            )
        )

        verdict = verifier(response).verify(9, ("issue-8",))

        assert verdict.ok
        assert Problem.MISSING_ISSUE_REFERENCE not in verdict.problems

    def test_a_stacked_pr_that_only_backticks_the_number_is_flagged(self) -> None:
        response = pull_requests(
            pull_request(101, base="issue-8", body="Closes `#9`.", ci="SUCCESS")
        )

        verdict = verifier(response).verify(9, ("issue-8",))

        assert verdict.problems == (Problem.MISSING_ISSUE_REFERENCE,)

    def test_a_pr_closing_several_issues_including_ours_is_accepted(self) -> None:
        response = pull_requests(
            pull_request(
                101, base="issue-8", body="Fixes #7, fixes #9, fixes #11", ci="SUCCESS"
            )
        )

        verdict = verifier(response).verify(9, ("issue-8",))

        assert verdict.ok

    def test_every_static_problem_is_reported_together(self) -> None:
        response = pull_requests(
            pull_request(101, base="main", body="Fixes #7", ci="SUCCESS")
        )

        verdict = verifier(response).verify(9, ("issue-8",))

        assert set(verdict.problems) == {
            Problem.WRONG_BASE,
            Problem.MISSING_ISSUE_REFERENCE,
        }

    def test_a_static_problem_does_not_suppress_the_ci_status(self) -> None:
        response = pull_requests(
            pull_request(101, base="main", body="Fixes #9", ci="FAILURE")
        )

        verdict = verifier(response).verify(9, ("issue-8",))

        assert verdict.problems == (Problem.WRONG_BASE,)
        assert verdict.ci is CiStatus.FAILED


class TestCiStatus:
    @pytest.mark.parametrize(
        ("rollup", "expected"),
        [
            ("SUCCESS", CiStatus.GREEN),
            ("FAILURE", CiStatus.FAILED),
            ("ERROR", CiStatus.FAILED),
            ("PENDING", CiStatus.PENDING),
            ("EXPECTED", CiStatus.PENDING),
        ],
    )
    def test_each_rollup_state_maps_to_a_ci_status(
        self, rollup: str, expected: CiStatus
    ) -> None:
        response = pull_requests(
            pull_request(101, base="issue-8", body="Fixes #9", ci=rollup)
        )

        verdict = verifier(response).verify(9, ("issue-8",))

        assert verdict.ci is expected

    def test_a_pr_with_no_checks_at_all_is_pending_not_green(self) -> None:
        response = pull_requests(
            pull_request(101, base="issue-8", body="Fixes #9", ci=None)
        )

        verdict = verifier(response).verify(9, ("issue-8",))

        assert verdict.ci is CiStatus.PENDING
        assert not verdict.ok


def _pending() -> Mapping[str, object]:
    return pull_requests(
        pull_request(101, base="issue-8", body="Fixes #9", ci="PENDING")
    )


def _green() -> Mapping[str, object]:
    return pull_requests(
        pull_request(101, base="issue-8", body="Fixes #9", ci="SUCCESS")
    )


class TestWaiting:
    def test_without_a_wait_a_pending_verdict_returns_at_once(self) -> None:
        clock = FakeClock()
        fake = transport(_pending())

        verdict = verifier_over(fake, clock).verify(9, ("issue-8",))

        assert verdict.ci is CiStatus.PENDING
        assert len(fake.calls) == 1
        assert clock.sleeps == []

    def test_ci_that_turns_green_inside_the_timeout_is_awaited(self) -> None:
        clock = FakeClock()
        fake = transport(_pending(), _green())

        verdict = verifier_over(fake, clock, interval=30.0).verify(
            9, ("issue-8",), wait=timedelta(minutes=5)
        )

        assert verdict.ok
        assert len(fake.calls) == 2
        assert clock.sleeps == [30.0]

    def test_ci_still_pending_at_the_deadline_is_a_verdict_not_an_error(self) -> None:
        clock = FakeClock()
        fake = transport(*[_pending()] * 5)

        verdict = verifier_over(fake, clock, interval=30.0).verify(
            9, ("issue-8",), wait=timedelta(seconds=120)
        )

        assert verdict.ci is CiStatus.PENDING
        assert len(fake.calls) == 5
        assert sum(clock.sleeps) == 120.0

    def test_a_failure_ends_the_wait_immediately(self) -> None:
        clock = FakeClock()
        failed = pull_requests(
            pull_request(101, base="issue-8", body="Fixes #9", ci="FAILURE")
        )
        fake = transport(_pending(), failed)

        verdict = verifier_over(fake, clock, interval=30.0).verify(
            9, ("issue-8",), wait=timedelta(hours=1)
        )

        assert verdict.ci is CiStatus.FAILED
        assert len(fake.calls) == 2

    def test_each_poll_rechecks_the_static_problems_too(self) -> None:
        clock = FakeClock()
        wrong_base = pull_requests(
            pull_request(101, base="main", body="Fixes #9", ci="PENDING")
        )
        fake = transport(wrong_base, _green())

        verdict = verifier_over(fake, clock, interval=30.0).verify(
            9, ("issue-8",), wait=timedelta(minutes=5)
        )

        assert verdict.ok
        assert verdict.problems == ()

    def test_the_last_poll_is_not_followed_by_a_sleep(self) -> None:
        clock = FakeClock()
        fake = transport(_pending(), _pending())

        verdict = verifier_over(fake, clock, interval=30.0).verify(
            9, ("issue-8",), wait=timedelta(seconds=10)
        )

        assert verdict.ci is CiStatus.PENDING
        assert clock.sleeps == [10.0]


class TestTransport:
    def test_a_graphql_error_propagates_instead_of_becoming_a_verdict(self) -> None:
        fake = transport(Errors([{"message": "Bad credentials"}]))

        with pytest.raises(RuntimeError, match="Bad credentials"):
            _ = verifier_over(fake).verify(9, ("issue-8",))


class TestTerminalVerdicts:
    def test_a_closed_pr_without_checks_is_not_waited_on(self) -> None:
        clock = FakeClock()
        closed = pull_requests(
            pull_request(101, base="issue-8", state="CLOSED", body="Fixes #9", ci=None)
        )
        fake = transport(closed)

        verdict = verifier_over(fake, clock, interval=30.0).verify(
            9, ("issue-8",), wait=timedelta(hours=1)
        )

        assert verdict.problems == (Problem.PR_CLOSED,)
        assert len(fake.calls) == 1
        assert clock.sleeps == []

    def test_no_pr_at_all_is_not_waited_on(self) -> None:
        clock = FakeClock()
        fake = transport(pull_requests())

        verdict = verifier_over(fake, clock).verify(
            9, ("issue-8",), wait=timedelta(hours=1)
        )

        assert verdict.problems == (Problem.NO_PR,)
        assert clock.sleeps == []

    def test_a_superseded_closed_pr_is_not_an_extra(self) -> None:
        response = pull_requests(
            pull_request(
                101,
                base="issue-8",
                state="CLOSED",
                body="Fixes #9",
                created_at="2026-08-01T00:00:00Z",
            ),
            pull_request(
                102,
                base="issue-8",
                body="Fixes #9",
                ci="SUCCESS",
                created_at="2026-08-06T00:00:00Z",
            ),
        )

        verdict = verifier(response).verify(9, ("issue-8",))

        assert verdict.ok
        assert verdict.pr_number == 102
        assert verdict.extra_pr_numbers == ()


class TestMergeDetection:
    def test_a_merged_pr_on_the_issue_branch_reports_merged(self) -> None:
        response = pull_requests(pull_request(101, state="MERGED", body="Fixes #9"))

        assert verifier(response).merged(9)

    def test_a_pr_closed_without_merging_is_not_merged(self) -> None:
        response = pull_requests(pull_request(101, state="CLOSED", body="Fixes #9"))

        assert not verifier(response).merged(9)

    def test_an_open_pr_is_not_merged(self) -> None:
        response = pull_requests(pull_request(101, body="Fixes #9", ci="SUCCESS"))

        assert not verifier(response).merged(9)

    def test_no_pr_at_all_is_not_merged(self) -> None:
        assert not verifier(pull_requests()).merged(9)

    def test_a_superseded_closed_pr_does_not_hide_the_merged_one(self) -> None:
        response = pull_requests(
            pull_request(
                101, state="CLOSED", body="Fixes #9", created_at="2026-08-06T00:00:00Z"
            ),
            pull_request(
                102, state="MERGED", body="Fixes #9", created_at="2026-08-01T00:00:00Z"
            ),
        )

        assert verifier(response).merged(9)


class TestSeveralAcceptedBases:
    def test_a_pr_retargeted_to_a_merged_predecessors_base_is_accepted(self) -> None:
        response = pull_requests(
            pull_request(101, base="main", body="Fixes #9", ci="SUCCESS")
        )

        verdict = verifier(response).verify(9, ("issue-8", "main"))

        assert verdict.ok

    def test_a_pr_still_on_the_launch_base_is_accepted_too(self) -> None:
        response = pull_requests(
            pull_request(101, base="issue-8", body="Fixes #9", ci="SUCCESS")
        )

        verdict = verifier(response).verify(9, ("issue-8", "main"))

        assert verdict.ok

    def test_a_base_matching_none_of_them_is_still_wrong(self) -> None:
        response = pull_requests(
            pull_request(101, base="issue-7", body="Fixes #9", ci="SUCCESS")
        )

        verdict = verifier(response).verify(9, ("issue-8", "main"))

        assert verdict.problems == (Problem.WRONG_BASE,)
        assert verdict.expected_bases == ("issue-8", "main")
