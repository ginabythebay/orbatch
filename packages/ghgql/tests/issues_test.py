from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from ghgql.fake import FakeTransport
from ghgql.issues import IssueCore, IssueMutations
from ghgql.repo import Repo
from ghgql.transport import GitHubGraphQL

REPO = Repo("example-org", "example-repo")


def mutations(transport: FakeTransport) -> IssueMutations:
    return IssueMutations(GitHubGraphQL(transport), REPO)


def label_response(*names: str) -> dict[str, object]:
    return {
        "repository": {
            f"l{index}": {"id": f"LA_{name}"} for index, name in enumerate(names)
        }
    }


class TestLabelIds:
    def test_one_query_names_every_label(self) -> None:
        transport = FakeTransport([label_response("queued", "stuck")])

        found = mutations(transport).label_ids(("queued", "stuck"))

        assert found == {"queued": "LA_queued", "stuck": "LA_stuck"}
        assert len(transport.calls) == 1
        query_text = transport.calls[0].query_text
        assert "$l0: String!" in query_text
        assert "$l1: String!" in query_text
        assert "l0: label(name: $l0)" in query_text
        assert "l1: label(name: $l1)" in query_text
        assert transport.calls[0].variables == {
            "owner": "example-org",
            "name": "example-repo",
            "l0": "queued",
            "l1": "stuck",
        }

    def test_no_names_costs_no_query(self) -> None:
        transport = FakeTransport([])

        assert mutations(transport).label_ids(()) == {}
        assert transport.calls == []

    def test_every_missing_label_is_named_in_one_error(self) -> None:
        transport = FakeTransport(
            [{"repository": {"l0": None, "l1": {"id": "LA_queued"}, "l2": None}}]
        )

        with pytest.raises(
            RuntimeError, match="Labels not found in repo: planned, stuck"
        ):
            mutations(transport).label_ids(("planned", "queued", "stuck"))


class TestLabelId:
    def test_a_repeated_lookup_costs_one_query(self) -> None:
        transport = FakeTransport([label_response("epic")])
        issues = mutations(transport)

        assert issues.label_id("epic") == "LA_epic"
        assert issues.label_id("epic") == "LA_epic"
        assert len(transport.calls) == 1

    def test_a_new_name_costs_another_query(self) -> None:
        transport = FakeTransport([label_response("epic"), label_response("stuck")])
        issues = mutations(transport)

        assert issues.label_id("epic") == "LA_epic"
        assert issues.label_id("stuck") == "LA_stuck"
        assert len(transport.calls) == 2

    def test_a_group_is_fetched_alongside_the_name(self) -> None:
        transport = FakeTransport([label_response("queued", "stuck")])
        issues = mutations(transport)

        assert issues.label_id("queued", ("queued", "stuck")) == "LA_queued"
        assert issues.label_id("stuck", ("queued", "stuck")) == "LA_stuck"
        assert len(transport.calls) == 1

    def test_a_failed_lookup_caches_nothing(self) -> None:
        transport = FakeTransport(
            [{"repository": {"l0": None}}, label_response("stuck")]
        )
        issues = mutations(transport)

        with pytest.raises(RuntimeError, match="Labels not found in repo: stuck"):
            issues.label_id("stuck")

        assert issues.label_id("stuck") == "LA_stuck"


class TestLabelMutations:
    def test_add_label_names_the_labelable_and_the_label(self) -> None:
        transport = FakeTransport([{}])

        mutations(transport).add_label("I_1", "LA_queued")

        call = transport.calls[0]
        assert "addLabelsToLabelable" in call.query_text
        assert call.variables == {"labelableId": "I_1", "labelId": "LA_queued"}

    def test_remove_label_names_the_labelable_and_the_label(self) -> None:
        transport = FakeTransport([{}])

        mutations(transport).remove_label("I_1", "LA_queued")

        call = transport.calls[0]
        assert "removeLabelsFromLabelable" in call.query_text
        assert call.variables == {"labelableId": "I_1", "labelId": "LA_queued"}


class TestSetIssueBody:
    def test_the_issue_id_and_body_reach_update_issue(self) -> None:
        transport = FakeTransport([{}])

        mutations(transport).set_issue_body("I_1", "## Test Plan\n")

        call = transport.calls[0]
        assert "updateIssue" in call.query_text
        assert call.variables == {"issueId": "I_1", "body": "## Test Plan\n"}


class _MergedNode(BaseModel):
    merged: bool


class _MergedConnection(BaseModel):
    nodes: list[_MergedNode]


class _WiderNode(IssueCore):
    body: str
    closed_by: _MergedConnection = Field(alias="closedByPullRequestsReferences")


class TestIssueCore:
    def test_a_subclass_widens_the_node_and_keeps_the_core(self) -> None:
        node = _WiderNode.model_validate(
            {
                "id": "I_1",
                "number": 7,
                "state": "OPEN",
                "title": "Fix the widget",
                "labels": {"nodes": [{"name": "queued"}]},
                "body": "## Test Plan\n",
                "closedByPullRequestsReferences": {"nodes": [{"merged": True}]},
            }
        )

        assert (node.id, node.number, node.state, node.title) == (
            "I_1",
            7,
            "OPEN",
            "Fix the widget",
        )
        assert [label.name for label in node.labels.nodes] == ["queued"]
        assert node.body == "## Test Plan\n"
        assert [pr.merged for pr in node.closed_by.nodes] == [True]
