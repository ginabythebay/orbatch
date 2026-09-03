from __future__ import annotations

import pytest

from batch.testing.payloads import (
    EPIC,
    EPIC_TITLE,
    child,
    children,
    client_over,
    issue,
    missing,
    pull_request,
    pull_requests,
    standalone,
    target,
    targets,
    transport,
)
from batch.testing.payloads import (
    epic as epic_node,
)
from ghgql.errors import IssueNotFoundError

_SELECTION = "closedByPullRequestsReferences"


class TestClosedByMerge:
    def test_fetch_issue_reports_a_merged_and_closed_issue_as_merged(self) -> None:
        fake = transport(issue(child(1600, state="CLOSED", closing_prs=[True])))

        fetched = client_over(fake).fetch_issue(1600)

        assert fetched.closed_by_merge is True
        assert _SELECTION in fake.calls[0].query_text

    def test_fetch_issue_reports_a_hand_closed_issue_as_unmerged(self) -> None:
        fake = transport(issue(child(1600, state="CLOSED")))

        assert client_over(fake).fetch_issue(1600).closed_by_merge is False

    def test_standalone_targets_report_merge_truthfully(self) -> None:
        fake = transport(
            standalone(
                child(1600, state="CLOSED", closing_prs=[True]),
                child(1601, state="CLOSED"),
            )
        )

        fetched = client_over(fake).fetch_targets([1600, 1601])

        assert [found.members[0].closed_by_merge for found in fetched] == [True, False]
        assert _SELECTION in fake.calls[0].query_text

    def test_epic_members_report_merge_truthfully(self) -> None:
        fake = transport(
            children(
                child(1600, state="CLOSED", closing_prs=[True]),
                child(1601, state="CLOSED", closing_prs=[False]),
            )
        )

        fetched = client_over(fake).fetch_targets([EPIC])[0].members

        assert [found.closed_by_merge for found in fetched] == [True, False]
        assert _SELECTION in fake.calls[0].query_text


class TestTargets:
    def test_an_issue_with_sub_issues_is_an_epic_and_contributes_them(self) -> None:
        fake = transport(children(child(1600), child(1601)))

        found = client_over(fake).fetch_targets([EPIC])[0]

        assert found.epic is True
        assert found.title == EPIC_TITLE
        assert [member.number for member in found.members] == [1600, 1601]

    def test_an_issue_with_no_sub_issues_contributes_itself(self) -> None:
        fake = transport(standalone(child(1769, title="One-off")))

        found = client_over(fake).fetch_targets([1769])[0]

        assert found.epic is False
        assert found.title == "One-off"
        assert [member.number for member in found.members] == [1769]

    def test_targets_keep_the_order_they_were_named_in(self) -> None:
        fake = transport(
            {
                "repository": {
                    "t1769": target(child(1769)),
                    f"t{EPIC}": epic_node(child(1600)),
                }
            }
        )

        found = client_over(fake).fetch_targets([EPIC, 1769])

        assert [each.number for each in found] == [EPIC, 1769]

    def test_a_closed_target_carries_its_state(self) -> None:
        fake = transport(children(child(1600), state="CLOSED"))

        assert client_over(fake).fetch_targets([EPIC])[0].state == "CLOSED"

    def test_a_number_that_resolves_to_nothing_is_an_error(self) -> None:
        fake = transport(missing(9999))

        with pytest.raises(IssueNotFoundError):
            _ = client_over(fake).fetch_targets([9999])

    def test_an_epic_with_more_children_than_one_page_is_refused(self) -> None:
        node = epic_node(child(1600))
        response = targets(
            {**node, "subIssues": {"totalCount": 120, "nodes": [child(1600)]}}
        )

        with pytest.raises(RuntimeError, match="120 sub-issues, fetched 1"):
            _ = client_over(transport(response)).fetch_targets([EPIC])


class TestClosingReferences:
    def test_a_pull_request_closing_another_repo_s_issue_closes_nothing(self) -> None:
        fake = transport(
            pull_requests(pull_request(101, body="Fixes upstream/other#9"))
        )

        fetched = client_over(fake).fetch_pull_requests("issue-9")

        assert [pull.closes for pull in fetched] == [()]
