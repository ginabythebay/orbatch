from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import ClassVar

import pytest
from pydantic import ValidationError

from ghgql.fake import FakeTransport
from ghgql.repo import Repo
from ghgql.transport import GitHubGraphQL
from orbit.github.client import GitHubClient
from orbit.github.models import MilestoneSummary

_MILESTONE = "developer velocity"
_REPO = Repo("example-org", "example-repo")


def _client(transport: FakeTransport) -> GitHubClient:
    return GitHubClient(GitHubGraphQL(transport), _REPO)


_GRAPHQL_RESPONSE = {
    "repository": {
        "milestones": {
            "nodes": [
                {
                    "title": _MILESTONE,
                    "issues": {
                        "nodes": [
                            {
                                "number": 42,
                                "state": "OPEN",
                                "title": "Fix the widget",
                            },
                            {
                                "number": 100,
                                "state": "CLOSED",
                                "title": "Add tests",
                            },
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            ]
        }
    }
}


_PARENTED_GRAPHQL_RESPONSE: dict[str, object] = {
    "repository": {
        "milestones": {
            "nodes": [
                {
                    "title": _MILESTONE,
                    "issues": {
                        "nodes": [
                            {
                                "number": 905,
                                "state": "OPEN",
                                "title": "An epic",
                                "parent": None,
                                "labels": {"nodes": [{"name": "epic"}]},
                            },
                            {
                                "number": 906,
                                "state": "OPEN",
                                "title": "Its child",
                                "parent": {"number": 905},
                                "labels": {"nodes": []},
                            },
                            {
                                "number": 907,
                                "state": "OPEN",
                                "title": "A one-off",
                                "parent": None,
                                "labels": {"nodes": [{"name": "bug"}]},
                            },
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            ]
        }
    }
}


def _issues_response(
    nodes: list[dict[str, object]],
    *,
    milestone_title: str = _MILESTONE,
    has_next_page: bool = False,
    end_cursor: str | None = None,
) -> dict[str, object]:
    return {
        "repository": {
            "milestones": {
                "nodes": [
                    {
                        "title": milestone_title,
                        "issues": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": has_next_page,
                                "endCursor": end_cursor,
                            },
                        },
                    }
                ]
            }
        }
    }


def _issue_node(number: int) -> dict[str, object]:
    return {"number": number, "state": "OPEN", "title": f"Issue {number}"}


class TestListIssuesByMilestone:
    def test_parses_graphql_response(self) -> None:
        client = _client(FakeTransport([_GRAPHQL_RESPONSE]))
        issues = client.list_issues_by_milestone(_MILESTONE)
        assert len(issues) == 2
        assert issues[0].number == 42
        assert issues[0].state == "OPEN"
        assert issues[0].title == "Fix the widget"
        assert issues[1].number == 100
        assert issues[1].state == "CLOSED"
        assert issues[1].title == "Add tests"

    def test_returns_empty_when_milestone_not_found(self) -> None:
        response: dict[str, object] = {"repository": {"milestones": {"nodes": []}}}
        client = _client(FakeTransport([response]))
        assert client.list_issues_by_milestone(_MILESTONE) == []

    def test_raises_on_milestone_title_mismatch(self) -> None:
        no_issues: list[object] = []
        response: dict[str, object] = {
            "repository": {
                "milestones": {
                    "nodes": [
                        {
                            "title": "developer velocity Q2",
                            "issues": {
                                "nodes": no_issues,
                                "pageInfo": {
                                    "hasNextPage": False,
                                    "endCursor": None,
                                },
                            },
                        }
                    ]
                }
            }
        }
        client = _client(FakeTransport([response]))
        with pytest.raises(RuntimeError, match="Expected milestone"):
            client.list_issues_by_milestone(_MILESTONE)

    def test_matches_milestone_case_insensitively(self) -> None:
        client = _client(FakeTransport([_GRAPHQL_RESPONSE]))
        issues = client.list_issues_by_milestone(_MILESTONE.upper())
        assert len(issues) == 2

    def test_maps_parent_and_the_epic_label(self) -> None:
        transport = FakeTransport([_PARENTED_GRAPHQL_RESPONSE])
        issues = _client(transport).list_issues_by_milestone(_MILESTONE)
        query = transport.calls[0].query_text
        assert "parent" in query
        assert "labels(first: 20)" in query
        assert [(i.number, i.parent_number, i.is_epic) for i in issues] == [
            (905, None, True),
            (906, 905, False),
            (907, None, False),
        ]

    def test_backlog_listing_still_filters_by_label(self) -> None:
        transport = FakeTransport([_GRAPHQL_RESPONSE])
        issues = _client(transport).list_issues_by_milestone(_MILESTONE, label="soon")
        assert [issue.number for issue in issues] == [42, 100]
        assert transport.calls[0].variables["labels"] == "soon"

    def test_concatenates_two_pages_in_order(self) -> None:
        transport = FakeTransport(
            [
                _issues_response(
                    [_issue_node(1), _issue_node(2)],
                    has_next_page=True,
                    end_cursor="CURSOR1",
                ),
                _issues_response([_issue_node(3)]),
            ]
        )
        issues = _client(transport).list_issues_by_milestone(_MILESTONE)
        assert [issue.number for issue in issues] == [1, 2, 3]

    def test_pages_forward_with_the_previous_cursor(self) -> None:
        transport = FakeTransport(
            [
                _issues_response(
                    [_issue_node(1)], has_next_page=True, end_cursor="CURSOR1"
                ),
                _issues_response([_issue_node(2)]),
            ]
        )
        _client(transport).list_issues_by_milestone(_MILESTONE)
        assert transport.calls[0].variables["after"] is None
        assert transport.calls[1].variables["after"] == "CURSOR1"

    def test_single_page_issues_one_call(self) -> None:
        transport = FakeTransport([_issues_response([_issue_node(1)])])
        _client(transport).list_issues_by_milestone(_MILESTONE)
        assert len(transport.calls) == 1

    def test_stops_when_the_cursor_is_missing(self) -> None:
        transport = FakeTransport(
            [_issues_response([_issue_node(1)], has_next_page=True, end_cursor=None)]
        )
        issues = _client(transport).list_issues_by_milestone(_MILESTONE)
        assert [issue.number for issue in issues] == [1]
        assert len(transport.calls) == 1

    def test_query_asks_for_the_pagination_fields(self) -> None:
        transport = FakeTransport([_issues_response([])])
        _client(transport).list_issues_by_milestone(_MILESTONE)
        query = transport.calls[0].query_text
        assert "pageInfo" in query
        assert "issues(first: 100, labels: $labels, after: $after)" in query

    def test_keeps_earlier_pages_when_the_milestone_disappears(self) -> None:
        transport = FakeTransport(
            [
                _issues_response(
                    [_issue_node(1)], has_next_page=True, end_cursor="CURSOR1"
                ),
                {"repository": {"milestones": {"nodes": []}}},
            ]
        )
        issues = _client(transport).list_issues_by_milestone(_MILESTONE)
        assert [issue.number for issue in issues] == [1]

    def test_rejects_a_response_without_page_info(self) -> None:
        response: dict[str, object] = {
            "repository": {
                "milestones": {
                    "nodes": [{"title": _MILESTONE, "issues": {"nodes": []}}]
                }
            }
        }
        client = _client(FakeTransport([response]))
        with pytest.raises(ValidationError):
            client.list_issues_by_milestone(_MILESTONE)

    def test_raises_on_a_title_mismatch_on_a_later_page(self) -> None:
        transport = FakeTransport(
            [
                _issues_response(
                    [_issue_node(1)], has_next_page=True, end_cursor="CURSOR1"
                ),
                _issues_response([_issue_node(2)], milestone_title="another milestone"),
            ]
        )
        with pytest.raises(RuntimeError, match="Expected milestone"):
            _client(transport).list_issues_by_milestone(_MILESTONE)

    def test_sends_the_label_filter_on_every_page(self) -> None:
        transport = FakeTransport(
            [
                _issues_response(
                    [_issue_node(1)], has_next_page=True, end_cursor="CURSOR1"
                ),
                _issues_response([_issue_node(2)]),
            ]
        )
        _client(transport).list_issues_by_milestone(_MILESTONE, label="soon")
        assert transport.calls[1].variables["labels"] == "soon"


_EPICS_GRAPHQL_RESPONSE = {
    "repository": {
        "milestones": {
            "nodes": [
                {
                    "title": _MILESTONE,
                    "issues": {
                        "nodes": [
                            {
                                "number": 905,
                                "state": "OPEN",
                                "title": "orbit — dev tool",
                                "subIssues": {
                                    "totalCount": 5,
                                    "nodes": [
                                        {"state": "OPEN"},
                                        {"state": "OPEN"},
                                        {"state": "OPEN"},
                                        {"state": "CLOSED"},
                                        {"state": "CLOSED"},
                                    ],
                                },
                            },
                            {
                                "number": 852,
                                "state": "OPEN",
                                "title": "Test suite speed",
                                "subIssues": {
                                    "totalCount": 4,
                                    "nodes": [
                                        {"state": "OPEN"},
                                        {"state": "CLOSED"},
                                        {"state": "CLOSED"},
                                        {"state": "CLOSED"},
                                    ],
                                },
                            },
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    },
                }
            ]
        }
    }
}


class TestListEpicsByMilestone:
    def test_parses_graphql_response(self) -> None:
        client = _client(FakeTransport([_EPICS_GRAPHQL_RESPONSE]))
        epics = client.list_epics_by_milestone(_MILESTONE)
        assert len(epics) == 2
        assert epics[0].number == 905
        assert epics[0].state == "OPEN"
        assert epics[0].title == "orbit — dev tool"
        assert epics[0].open_count == 3
        assert epics[0].total_count == 5
        assert epics[1].number == 852
        assert epics[1].open_count == 1
        assert epics[1].total_count == 4

    def test_concatenates_two_pages_of_epics(self) -> None:
        transport = FakeTransport(
            [
                _epic_response(
                    [_epic_node(10, "OPEN", "First epic", ["OPEN", "CLOSED"])],
                    has_next_page=True,
                    end_cursor="CURSOR1",
                ),
                _epic_response([_epic_node(11, "OPEN", "Second epic", ["OPEN"])]),
            ]
        )
        epics = _client(transport).list_epics_by_milestone(_MILESTONE)
        assert [(e.number, e.open_count, e.total_count) for e in epics] == [
            (10, 1, 2),
            (11, 1, 1),
        ]
        assert transport.calls[1].variables["after"] == "CURSOR1"

    def test_query_asks_for_the_pagination_fields(self) -> None:
        transport = FakeTransport([_epic_response([])])
        _client(transport).list_epics_by_milestone(_MILESTONE)
        query = transport.calls[0].query_text
        assert "pageInfo" in query
        assert 'issues(first: 100, labels: ["epic"], after: $after)' in query

    def test_epic_with_no_sub_issues(self) -> None:
        response = _epic_response(
            [
                _epic_node(10, "OPEN", "Empty epic", []),
            ]
        )
        client = _client(FakeTransport([response]))
        epics = client.list_epics_by_milestone(_MILESTONE)
        assert epics[0].open_count == 0
        assert epics[0].total_count == 0

    def test_returns_empty_when_milestone_not_found(self) -> None:
        response: dict[str, object] = {"repository": {"milestones": {"nodes": []}}}
        client = _client(FakeTransport([response]))
        assert client.list_epics_by_milestone(_MILESTONE) == []

    def test_raises_on_milestone_title_mismatch(self) -> None:
        response = _epic_response([], milestone_title="wrong milestone")
        client = _client(FakeTransport([response]))
        with pytest.raises(RuntimeError, match="Expected milestone"):
            client.list_epics_by_milestone(_MILESTONE)

    def test_matches_milestone_case_insensitively(self) -> None:
        response = _epic_response(
            [_epic_node(2, "OPEN", "Fresh epic", ["OPEN"])],
            milestone_title=_MILESTONE,
        )
        client = _client(FakeTransport([response]))
        epics = client.list_epics_by_milestone(_MILESTONE.upper())
        assert len(epics) == 1

    def test_raises_when_sub_issues_exceed_page_size(self) -> None:
        nodes = [{"state": "OPEN"}] * 100
        response = _epic_response(
            [
                {
                    "number": 1,
                    "state": "OPEN",
                    "title": "Big epic",
                    "subIssues": {"totalCount": 150, "nodes": nodes},
                }
            ]
        )
        client = _client(FakeTransport([response]))
        with pytest.raises(RuntimeError, match=r"150 sub-issues.*100 were fetched"):
            client.list_epics_by_milestone(_MILESTONE)

    def test_all_closed_sub_issues(self) -> None:
        response = _epic_response(
            [
                _epic_node(1, "CLOSED", "Done epic", ["CLOSED", "CLOSED", "CLOSED"]),
            ]
        )
        client = _client(FakeTransport([response]))
        epics = client.list_epics_by_milestone(_MILESTONE)
        assert epics[0].open_count == 0
        assert epics[0].total_count == 3

    def test_all_open_sub_issues(self) -> None:
        response = _epic_response(
            [
                _epic_node(2, "OPEN", "Fresh epic", ["OPEN", "OPEN"]),
            ]
        )
        client = _client(FakeTransport([response]))
        epics = client.list_epics_by_milestone(_MILESTONE)
        assert epics[0].open_count == 2
        assert epics[0].total_count == 2

    def test_raises_when_total_count_less_than_nodes(self) -> None:
        response = _epic_response(
            [
                {
                    "number": 1,
                    "state": "OPEN",
                    "title": "Race epic",
                    "subIssues": {
                        "totalCount": 2,
                        "nodes": [
                            {"state": "OPEN"},
                            {"state": "OPEN"},
                            {"state": "CLOSED"},
                        ],
                    },
                }
            ]
        )
        client = _client(FakeTransport([response]))
        with pytest.raises(RuntimeError, match=r"2 sub-issues.*3 were fetched"):
            client.list_epics_by_milestone(_MILESTONE)


def _milestone_id_response(title: str) -> dict[str, object]:
    return {"repository": {"milestones": {"nodes": [{"id": "MI_1", "title": title}]}}}


class TestListMilestones:
    def test_parses_titles_and_state(self) -> None:
        response: dict[str, object] = {
            "repository": {
                "milestones": {
                    "nodes": [
                        {
                            "title": "degraded state",
                            "state": "OPEN",
                            "dueOn": "2026-04-30T07:00:00Z",
                        },
                        {
                            "title": "developer velocity",
                            "state": "OPEN",
                            "dueOn": None,
                        },
                    ]
                }
            }
        }
        client = _client(FakeTransport([response]))
        assert client.list_milestones() == [
            MilestoneSummary(
                title="degraded state", state="OPEN", due_on=date(2026, 4, 30)
            ),
            MilestoneSummary(title="developer velocity", state="OPEN", due_on=None),
        ]

    def test_include_closed_asks_for_closed_milestones_too(self) -> None:
        response: dict[str, object] = {
            "repository": {
                "milestones": {
                    "nodes": [
                        {
                            "title": "an old sprint",
                            "state": "CLOSED",
                            "dueOn": "2026-02-28T08:00:00Z",
                        }
                    ]
                }
            }
        }
        transport = FakeTransport([response])
        assert _client(transport).list_milestones(include_closed=True) == [
            MilestoneSummary(
                title="an old sprint", state="CLOSED", due_on=date(2026, 2, 28)
            )
        ]
        assert "CLOSED" in transport.calls[0].query_text

    def test_open_only_by_default(self) -> None:
        transport = FakeTransport([{"repository": {"milestones": {"nodes": []}}}])
        _client(transport).list_milestones()
        assert "CLOSED" not in transport.calls[0].query_text

    def test_returns_empty_when_no_milestones(self) -> None:
        response: dict[str, object] = {"repository": {"milestones": {"nodes": []}}}
        client = _client(FakeTransport([response]))
        assert client.list_milestones() == []


class TestFetchMilestoneId:
    def test_resolves_exact_title(self) -> None:
        client = _client(FakeTransport([_milestone_id_response(_MILESTONE)]))
        assert client.fetch_milestone_id(_MILESTONE) == "MI_1"

    def test_matches_case_insensitively(self) -> None:
        client = _client(FakeTransport([_milestone_id_response(_MILESTONE)]))
        assert client.fetch_milestone_id(_MILESTONE.upper()) == "MI_1"

    def test_raises_when_not_found(self) -> None:
        response: dict[str, object] = {"repository": {"milestones": {"nodes": []}}}
        client = _client(FakeTransport([response]))
        with pytest.raises(RuntimeError, match="not found"):
            client.fetch_milestone_id(_MILESTONE)

    def test_raises_on_substring_mismatch(self) -> None:
        response = _milestone_id_response("developer velocity Q2")
        client = _client(FakeTransport([response]))
        with pytest.raises(RuntimeError, match="Expected milestone"):
            client.fetch_milestone_id(_MILESTONE)


def _epic_node(
    number: int,
    state: str,
    title: str,
    sub_states: list[str],
) -> dict[str, object]:
    return {
        "number": number,
        "state": state,
        "title": title,
        "subIssues": {
            "totalCount": len(sub_states),
            "nodes": [{"state": s} for s in sub_states],
        },
    }


def _epic_response(
    nodes: list[dict[str, object]],
    milestone_title: str = _MILESTONE,
    *,
    has_next_page: bool = False,
    end_cursor: str | None = None,
) -> dict[str, object]:
    return {
        "repository": {
            "milestones": {
                "nodes": [
                    {
                        "title": milestone_title,
                        "issues": {
                            "nodes": nodes,
                            "pageInfo": {
                                "hasNextPage": has_next_page,
                                "endCursor": end_cursor,
                            },
                        },
                    }
                ]
            }
        }
    }


def _sub_issues_response(
    nodes: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "repository": {
            "issue": {
                "subIssues": {
                    "totalCount": len(nodes),
                    "nodes": nodes,
                }
            }
        }
    }


class TestFetchSubIssueTree:
    def test_parses_flat_sub_issues(self) -> None:
        response = _sub_issues_response(
            [
                {
                    "number": 10,
                    "state": "OPEN",
                    "title": "Sub A",
                    "subIssues": {"totalCount": 0},
                },
                {
                    "number": 20,
                    "state": "CLOSED",
                    "title": "Sub B",
                    "subIssues": {"totalCount": 0},
                },
            ]
        )
        client = _client(FakeTransport([response]))
        result = client.fetch_sub_issue_tree(1)
        assert len(result) == 2
        assert result[0].number == 10
        assert result[0].state == "OPEN"
        assert result[0].title == "Sub A"
        assert result[0].children == ()
        assert result[1].number == 20
        assert result[1].children == ()

    def test_returns_empty_when_issue_not_found(self) -> None:
        response: dict[str, object] = {"repository": {"issue": None}}
        client = _client(FakeTransport([response]))
        assert client.fetch_sub_issue_tree(9999) == []


class TestFetchParentIssue:
    def test_returns_parent(self) -> None:
        response: dict[str, object] = {
            "repository": {
                "issue": {
                    "parent": {
                        "number": 100,
                        "state": "OPEN",
                        "title": "Epic parent",
                    }
                }
            }
        }
        client = _client(FakeTransport([response]))
        result = client.fetch_parent_issue(42)
        assert result is not None
        assert result.number == 100
        assert result.state == "OPEN"
        assert result.title == "Epic parent"

    def test_returns_none_when_no_parent(self) -> None:
        response: dict[str, object] = {"repository": {"issue": {"parent": None}}}
        client = _client(FakeTransport([response]))
        assert client.fetch_parent_issue(42) is None


def _issue_detail_response(
    *,
    number: int = 42,
    state: str = "OPEN",
    title: str = "Fix widget",
    body: str = "",
    labels: list[str] | None = None,
    milestone: dict[str, str] | None = None,
    parent: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "repository": {
            "issue": {
                "id": f"I_{number}",
                "number": number,
                "state": state,
                "title": title,
                "body": body,
                "labels": {
                    "nodes": [{"name": n} for n in (labels or [])],
                },
                "milestone": milestone,
                "parent": parent,
            }
        }
    }


class TestFetchIssueDetail:
    def test_parses_full_response(self) -> None:
        response = _issue_detail_response(
            number=42,
            state="OPEN",
            title="Fix widget",
            body="## Problem\n\nDetails here.",
            labels=["epic", "bug"],
            milestone={"id": "MI_1", "title": "developer velocity"},
            parent={"id": "I_99", "number": 99, "title": "Parent epic"},
        )
        client = _client(FakeTransport([response]))
        result = client.fetch_issue_detail(42)
        assert result.node_id == "I_42"
        assert result.number == 42
        assert result.title == "Fix widget"
        assert result.body == "## Problem\n\nDetails here."
        assert result.labels == ("epic", "bug")
        assert result.milestone_id == "MI_1"
        assert result.milestone_title == "developer velocity"
        assert result.parent_number == 99
        assert result.parent_node_id == "I_99"
        assert result.parent_title == "Parent epic"

    def test_parses_response_without_milestone_or_parent(self) -> None:
        response = _issue_detail_response(number=10, labels=[])
        client = _client(FakeTransport([response]))
        result = client.fetch_issue_detail(10)
        assert result.milestone_id is None
        assert result.milestone_title is None
        assert result.parent_number is None
        assert result.parent_node_id is None
        assert result.parent_title is None

    def test_raises_when_issue_not_found(self) -> None:
        response: dict[str, object] = {"repository": {"issue": None}}
        client = _client(FakeTransport([response]))
        with pytest.raises(RuntimeError, match="not found"):
            client.fetch_issue_detail(9999)


class TestCreateIssue:
    _RESPONSE: ClassVar[dict[str, object]] = {
        "createIssue": {"issue": {"id": "I_1", "number": 7, "title": "T"}}
    }

    def test_body_included_in_variables_when_given(self) -> None:
        transport = FakeTransport([self._RESPONSE])
        client = _client(transport)
        client.create_issue("T", "R_1", "MI_1", "the body")
        kwargs = transport.calls[-1].variables
        assert kwargs["body"] == "the body"

    def test_body_absent_from_variables_when_none(self) -> None:
        transport = FakeTransport([self._RESPONSE])
        client = _client(transport)
        client.create_issue("T", "R_1", "MI_1")
        kwargs = transport.calls[-1].variables
        assert "body" not in kwargs


class TestReopenIssueById:
    def test_passes_node_id_to_mutation(self) -> None:
        transport = FakeTransport([{}])
        client = _client(transport)
        client.reopen_issue_by_id("I_42")
        call = transport.calls[-1]
        assert call.variables == {"issueId": "I_42"}
        assert "reopenIssue" in call.query_text


class TestSetIssueBody:
    def test_passes_node_id_and_body_to_mutation(self) -> None:
        transport = FakeTransport([{}])
        client = _client(transport)
        client.set_issue_body("I_42", "the new body")
        kwargs = transport.calls[-1].variables
        assert kwargs == {"issueId": "I_42", "body": "the new body"}


class TestAddComment:
    def test_passes_subject_id_and_body_to_mutation(self) -> None:
        transport = FakeTransport([{}])
        client = _client(transport)
        client.add_comment("I_42", "Closed by the orbit CLI (completed).")
        kwargs = transport.calls[-1].variables
        assert kwargs == {
            "subjectId": "I_42",
            "body": "Closed by the orbit CLI (completed).",
        }


class TestReprioritizeSubIssue:
    def test_after_id_sent_alone(self) -> None:
        transport = FakeTransport([{}])
        client = _client(transport)
        client.reprioritize_sub_issue("I_epic", "I_child", after_id="I_sib")
        kwargs = transport.calls[-1].variables
        assert kwargs == {
            "parentId": "I_epic",
            "childId": "I_child",
            "afterId": "I_sib",
        }

    def test_before_id_sent_alone(self) -> None:
        transport = FakeTransport([{}])
        client = _client(transport)
        client.reprioritize_sub_issue("I_epic", "I_child", before_id="I_sib")
        kwargs = transport.calls[-1].variables
        assert kwargs == {
            "parentId": "I_epic",
            "childId": "I_child",
            "beforeId": "I_sib",
        }

    def test_rejects_neither_position(self) -> None:
        transport = FakeTransport([{}])
        client = _client(transport)
        with pytest.raises(ValueError, match="after_id or before_id"):
            client.reprioritize_sub_issue("I_epic", "I_child")
        assert transport.calls == []

    def test_rejects_both_positions(self) -> None:
        transport = FakeTransport([{}])
        client = _client(transport)
        with pytest.raises(ValueError, match="after_id and before_id"):
            client.reprioritize_sub_issue(
                "I_epic", "I_child", after_id="I_a", before_id="I_b"
            )
        assert transport.calls == []


class TestSearchIssueTitles:
    _RESPONSE: ClassVar[dict[str, object]] = {
        "search": {
            "nodes": [
                {"number": 42, "state": "OPEN", "title": "guild.yaml loader"},
                {"number": 100, "state": "CLOSED", "title": "guild.yaml schema"},
            ]
        }
    }

    def test_builds_scoped_title_query(self) -> None:
        transport = FakeTransport([self._RESPONSE])
        client = _client(transport)
        client.search_issue_titles("guild.yaml")
        kwargs = transport.calls[-1].variables
        assert kwargs["q"] == (
            "repo:example-org/example-repo in:title type:issue guild.yaml"
        )

    def test_parses_matches(self) -> None:
        client = _client(FakeTransport([self._RESPONSE]))
        issues = client.search_issue_titles("guild.yaml")
        assert [i.number for i in issues] == [42, 100]
        assert issues[0].title == "guild.yaml loader"
        assert issues[1].state == "CLOSED"

    def test_skips_non_issue_nodes(self) -> None:
        response = {
            "search": {
                "nodes": [
                    {},
                    {"number": 7, "state": "OPEN", "title": "real issue"},
                ]
            }
        }
        client = _client(FakeTransport([response]))
        issues = client.search_issue_titles("x")
        assert [i.number for i in issues] == [7]


def _period_node(
    number: int,
    *,
    title: str = "a title",
    state: str = "OPEN",
    created: str = "2026-04-02T09:00:00Z",
    closed: str | None = None,
    labels: Sequence[str] = (),
    parent: dict[str, object] | None = None,
    milestone: str | None = None,
) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "state": state,
        "createdAt": created,
        "closedAt": closed,
        "labels": {"nodes": [{"name": name} for name in labels]},
        "parent": parent,
        "milestone": None if milestone is None else {"title": milestone},
    }


def _period_page(
    nodes: Sequence[dict[str, object]],
    *,
    cursor: str | None = None,
) -> dict[str, object]:
    return {
        "search": {
            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
            "nodes": list(nodes),
        }
    }


class TestSearchPeriodIssues:
    _START: ClassVar[date] = date(2026, 4, 1)
    _END: ClassVar[date] = date(2026, 4, 30)

    def test_maps_every_field(self) -> None:
        created_page = _period_page(
            [
                _period_node(
                    11,
                    title="child of an epic",
                    state="CLOSED",
                    created="2026-04-02T09:00:00Z",
                    closed="2026-04-09T17:30:00Z",
                    parent={
                        "number": 5,
                        "title": "the epic",
                        "createdAt": "2026-01-15T08:00:00Z",
                    },
                    milestone="config v2",
                ),
                _period_node(
                    12,
                    title="a new epic",
                    labels=["epic", "config"],
                ),
            ]
        )
        client = _client(FakeTransport([created_page, _period_page([])]))
        issues = client.search_period_issues(self._START, self._END)
        child, epic = issues
        assert child.number == 11
        assert child.title == "child of an epic"
        assert child.state == "CLOSED"
        assert child.created_at == date(2026, 4, 2)
        assert child.closed_at == date(2026, 4, 9)
        assert child.is_epic is False
        assert child.parent is not None
        assert child.parent.number == 5
        assert child.parent.title == "the epic"
        assert child.parent.created_at == date(2026, 1, 15)
        assert child.milestone == "config v2"
        assert epic.milestone is None
        assert epic.closed_at is None
        assert epic.is_epic is True
        assert epic.parent is None

    def test_searches_created_and_closed_and_unions_by_number(self) -> None:
        both = _period_node(11, closed="2026-04-09T17:30:00Z", state="CLOSED")
        transport = FakeTransport(
            [
                _period_page([both]),
                _period_page([both, _period_node(12)]),
            ]
        )
        issues = _client(transport).search_period_issues(self._START, self._END)
        queries = [call.variables["q"] for call in transport.calls]
        assert queries == [
            "repo:example-org/example-repo type:issue created:2026-04-01..2026-04-30",
            "repo:example-org/example-repo type:issue closed:2026-04-01..2026-04-30",
        ]
        assert [issue.number for issue in issues] == [11, 12]

    def test_skips_non_issue_nodes(self) -> None:
        transport = FakeTransport(
            [_period_page([{}, _period_node(11)]), _period_page([])]
        )
        issues = _client(transport).search_period_issues(self._START, self._END)
        assert [issue.number for issue in issues] == [11]

    def test_follows_the_cursor(self) -> None:
        transport = FakeTransport(
            [
                _period_page([_period_node(11)], cursor="Y3Vy"),
                _period_page([_period_node(12)]),
                _period_page([]),
            ]
        )
        issues = _client(transport).search_period_issues(self._START, self._END)
        assert [issue.number for issue in issues] == [11, 12]
        assert [call.variables["after"] for call in transport.calls] == [
            None,
            "Y3Vy",
            None,
        ]


def _pr_node(
    number: int,
    *,
    title: str = "a pull request",
    merged: str = "2026-04-03T11:00:00Z",
    merge_commit: str | None = "abc123",
) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "mergedAt": merged,
        "mergeCommit": None if merge_commit is None else {"oid": merge_commit},
    }


def _pr_page(
    nodes: Sequence[dict[str, object]],
    *,
    cursor: str | None = None,
) -> dict[str, object]:
    return {
        "search": {
            "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
            "nodes": list(nodes),
        }
    }


class TestSearchPeriodPrs:
    _START: ClassVar[date] = date(2026, 4, 1)
    _END: ClassVar[date] = date(2026, 4, 30)

    def test_maps_every_field(self) -> None:
        transport = FakeTransport(
            [
                _pr_page(
                    [
                        _pr_node(
                            180,
                            title="feat: guild config validation",
                            merged="2026-04-03T11:00:00Z",
                            merge_commit="d34db33f",
                        ),
                        _pr_node(181, merge_commit=None),
                    ]
                )
            ]
        )
        prs = _client(transport).search_period_prs(self._START, self._END)
        assert [(p.number, p.title, p.merged_at, p.merge_commit_oid) for p in prs] == [
            (180, "feat: guild config validation", date(2026, 4, 3), "d34db33f"),
            (181, "a pull request", date(2026, 4, 3), None),
        ]
        assert transport.calls[0].variables["q"] == (
            "repo:example-org/example-repo type:pr is:merged"
            " merged:2026-04-01..2026-04-30"
        )

    def test_follows_the_cursor(self) -> None:
        transport = FakeTransport(
            [
                _pr_page([_pr_node(180)], cursor="Y3Vy"),
                _pr_page([_pr_node(181)]),
            ]
        )
        prs = _client(transport).search_period_prs(self._START, self._END)
        assert [p.number for p in prs] == [180, 181]
        assert [call.variables["after"] for call in transport.calls] == [None, "Y3Vy"]


class TestFetchFirstChild:
    _RESPONSE: ClassVar[dict[str, object]] = {
        "repository": {
            "issue": {
                "subIssues": {
                    "nodes": [{"id": "I_43", "number": 43, "title": "Ship widget"}]
                }
            }
        }
    }

    def test_queries_only_the_first_child(self) -> None:
        transport = FakeTransport([self._RESPONSE])
        client = _client(transport)
        child = client.fetch_first_child(800)
        call = transport.calls[-1]
        assert call.variables == {
            "owner": "example-org",
            "name": "example-repo",
            "number": 800,
        }
        assert "subIssues(first: 1)" in call.query_text
        assert child is not None
        assert (child.number, child.node_id, child.title) == (43, "I_43", "Ship widget")

    def test_raises_for_missing_issue(self) -> None:
        transport = FakeTransport([{"repository": {"issue": None}}])
        with pytest.raises(RuntimeError, match="not found"):
            _client(transport).fetch_first_child(800)

    def test_returns_none_for_childless_epic(self) -> None:
        transport = FakeTransport(
            [{"repository": {"issue": {"subIssues": {"nodes": []}}}}]
        )
        assert _client(transport).fetch_first_child(800) is None
