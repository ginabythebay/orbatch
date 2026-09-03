from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, RootModel

from batch.body import closing_references
from batch.models import BatchLabel, ChildIssue, CiStatus, PullRequest, Target
from ghgql.errors import IssueNotFoundError
from ghgql.repo import Repo
from ghgql.transport import GitHubGraphQL, RateLimit

_ISSUE_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
      number
      state
      title
      body
      labels(first: 100) { nodes { name } }
      closedByPullRequestsReferences(first: 100) { nodes { merged } }
    }
  }
}
"""

_LABEL_IDS_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    queued: label(name: "queued") { id }
    planned: label(name: "planned") { id }
    implementing: label(name: "implementing") { id }
    readyForReview: label(name: "ready-for-review") { id }
    stuck: label(name: "stuck") { id }
  }
}
"""

_ADD_LABEL_MUTATION = """
mutation($labelableId: ID!, $labelId: ID!) {
  addLabelsToLabelable(input: {labelableId: $labelableId, labelIds: [$labelId]}) {
    clientMutationId
  }
}
"""

_REMOVE_LABEL_MUTATION = """
mutation($labelableId: ID!, $labelId: ID!) {
  removeLabelsFromLabelable(
    input: {labelableId: $labelableId, labelIds: [$labelId]}
  ) {
    clientMutationId
  }
}
"""

_SET_BODY_MUTATION = """
mutation($issueId: ID!, $body: String!) {
  updateIssue(input: {id: $issueId, body: $body}) {
    clientMutationId
  }
}
"""

_TARGET_FRAGMENT = """
fragment core on Issue {
  id
  number
  state
  title
  body
  labels(first: 100) { nodes { name } }
  closedByPullRequestsReferences(first: 100) { nodes { merged } }
}
"""

TARGETS_PER_QUERY = 20


def _alias(number: int) -> str:
    return f"t{number}"


_TARGET_SELECTION = "...core subIssues(first: 100) { totalCount nodes { ...core } }"


def _targets_query(numbers: Sequence[int]) -> str:
    selections = "\n".join(
        f"    {_alias(number)}: issue(number: {number}) {{ {_TARGET_SELECTION} }}"
        for number in numbers
    )
    return f"""{_TARGET_FRAGMENT}
query($owner: String!, $name: String!) {{
  rateLimit {{ cost remaining limit resetAt }}
  repository(owner: $owner, name: $name) {{
{selections}
  }}
}}
"""


_PULL_REQUESTS_QUERY = """
query($owner: String!, $name: String!, $headRefName: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(headRefName: $headRefName, first: 20) {
      nodes {
        number
        state
        baseRefName
        createdAt
        body
        commits(last: 1) {
          nodes { commit { statusCheckRollup { state } } }
        }
      }
    }
  }
}
"""

_ROLLUP_STATES = {
    "SUCCESS": CiStatus.GREEN,
    "FAILURE": CiStatus.FAILED,
    "ERROR": CiStatus.FAILED,
    "PENDING": CiStatus.PENDING,
    "EXPECTED": CiStatus.PENDING,
}


class _LabelNode(BaseModel):
    name: str


class _LabelConnection(BaseModel):
    nodes: list[_LabelNode]


class _ClosingPullRequestNode(BaseModel):
    merged: bool


class _ClosingPullRequestConnection(BaseModel):
    nodes: list[_ClosingPullRequestNode]


class _ChildNode(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    id: str
    number: int
    state: str
    title: str
    body: str
    labels: _LabelConnection
    closed_by: _ClosingPullRequestConnection = Field(
        alias="closedByPullRequestsReferences"
    )


class _SubIssueConnection(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    total_count: int = Field(alias="totalCount")
    nodes: list[_ChildNode]


class _TargetNode(_ChildNode):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    sub_issues: _SubIssueConnection = Field(alias="subIssues")


class _IssueRepo(BaseModel):
    issue: _ChildNode | None = None


class _IssueResponseData(BaseModel):
    repository: _IssueRepo


class _TargetsRepo(RootModel[dict[str, _TargetNode | None]]):
    pass


class _TargetsResponseData(BaseModel):
    repository: _TargetsRepo


class _LabelIdNode(BaseModel):
    id: str


class _LabelIdRepo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    queued: _LabelIdNode | None = None
    planned: _LabelIdNode | None = None
    implementing: _LabelIdNode | None = None
    ready_for_review: _LabelIdNode | None = Field(default=None, alias="readyForReview")
    stuck: _LabelIdNode | None = None


class _LabelIdResponseData(BaseModel):
    repository: _LabelIdRepo


class _RollupNode(BaseModel):
    state: str


class _CommitNode(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    status_check_rollup: _RollupNode | None = Field(
        default=None, alias="statusCheckRollup"
    )


class _CommitEdgeNode(BaseModel):
    commit: _CommitNode


class _CommitConnection(BaseModel):
    nodes: list[_CommitEdgeNode]


class _PullRequestNode(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    number: int
    state: str
    base_ref_name: str = Field(alias="baseRefName")
    created_at: str = Field(alias="createdAt")
    body: str
    commits: _CommitConnection


class _PullRequestConnection(BaseModel):
    nodes: list[_PullRequestNode]


class _PullRequestRepo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    pull_requests: _PullRequestConnection = Field(alias="pullRequests")


class _PullRequestResponseData(BaseModel):
    repository: _PullRequestRepo


def _rollup(node: _PullRequestNode) -> CiStatus:
    """PENDING when no rollup exists: 'not started' and 'never will' look alike."""
    commits = node.commits.nodes
    rollup = commits[0].commit.status_check_rollup if commits else None
    if rollup is None:
        return CiStatus.PENDING
    return _ROLLUP_STATES.get(rollup.state, CiStatus.PENDING)


def _to_pull_request(node: _PullRequestNode, slug: str) -> PullRequest:
    return PullRequest(
        number=node.number,
        state=node.state,
        base=node.base_ref_name,
        created_at=node.created_at,
        closes=closing_references(node.body, slug),
        ci=_rollup(node),
    )


def _to_target(number: int, node: _TargetNode | None) -> Target:
    if node is None:
        raise IssueNotFoundError(number)
    connection = node.sub_issues
    total, fetched = connection.total_count, len(connection.nodes)
    if total != fetched:
        raise RuntimeError(f"#{number} has {total} sub-issues, fetched {fetched}")
    return Target(
        number=number,
        title=node.title,
        state=node.state,
        members=tuple(_to_child(child) for child in connection.nodes)
        if total
        else (_to_child(node),),
        epic=bool(total),
    )


def _to_child(node: _ChildNode) -> ChildIssue:
    return ChildIssue(
        node_id=node.id,
        number=node.number,
        state=node.state,
        title=node.title,
        body=node.body,
        labels=tuple(label.name for label in node.labels.nodes),
        closed_by_merge=any(pr.merged for pr in node.closed_by.nodes),
    )


class BatchGitHub:
    def __init__(self, graphql: GitHubGraphQL, repo: Repo) -> None:
        self._graphql: GitHubGraphQL = graphql
        self.repo: Repo = repo
        self._label_ids: dict[BatchLabel, str] = {}

    @property
    def rate_limit(self) -> RateLimit | None:
        """What the last query that asked was told; only `fetch_targets` asks."""
        return self._graphql.rate_limit

    def fetch_issue(self, number: int) -> ChildIssue:
        owner, name = self.repo
        raw = self._graphql.run(_ISSUE_QUERY, owner=owner, name=name, number=number)
        response = _IssueResponseData.model_validate(raw)
        if response.repository.issue is None:
            raise IssueNotFoundError(number)
        return _to_child(response.repository.issue)

    def label_id(self, label: BatchLabel) -> str:
        """The label's node id, fetching all five on first use."""
        if not self._label_ids:
            owner, name = self.repo
            raw = self._graphql.run(_LABEL_IDS_QUERY, owner=owner, name=name)
            found = _LabelIdResponseData.model_validate(raw).repository
            nodes = {
                BatchLabel.QUEUED: found.queued,
                BatchLabel.PLANNED: found.planned,
                BatchLabel.IMPLEMENTING: found.implementing,
                BatchLabel.READY_FOR_REVIEW: found.ready_for_review,
                BatchLabel.STUCK: found.stuck,
            }
            missing = [name for name, node in nodes.items() if node is None]
            if missing:
                raise RuntimeError(f"Labels not found in repo: {', '.join(missing)}")
            self._label_ids = {
                key: node.id for key, node in nodes.items() if node is not None
            }
        return self._label_ids[label]

    def add_label(self, node_id: str, label: BatchLabel) -> None:
        self._graphql.run(
            _ADD_LABEL_MUTATION,
            labelableId=node_id,
            labelId=self.label_id(label),
        )

    def remove_label(self, node_id: str, label: BatchLabel) -> None:
        self._graphql.run(
            _REMOVE_LABEL_MUTATION,
            labelableId=node_id,
            labelId=self.label_id(label),
        )

    def set_issue_body(self, node_id: str, body: str) -> None:
        self._graphql.run(_SET_BODY_MUTATION, issueId=node_id, body=body)

    def fetch_pull_requests(self, head_ref_name: str) -> list[PullRequest]:
        owner, name = self.repo
        raw = self._graphql.run(
            _PULL_REQUESTS_QUERY,
            owner=owner,
            name=name,
            headRefName=head_ref_name,
        )
        response = _PullRequestResponseData.model_validate(raw)
        return [
            _to_pull_request(node, f"{owner}/{name}")
            for node in response.repository.pull_requests.nodes
        ]

    def fetch_targets(self, numbers: Sequence[int]) -> list[Target]:
        """Each named number as a batch target, in the order given.

        A number with sub-issues is an epic and contributes them; one with none
        is standalone and contributes itself.
        """
        owner, name = self.repo
        unique = list(dict.fromkeys(numbers))
        targets: list[Target] = []
        for start in range(0, len(unique), TARGETS_PER_QUERY):
            chunk = unique[start : start + TARGETS_PER_QUERY]
            raw = self._graphql.run(_targets_query(chunk), owner=owner, name=name)
            found = _TargetsResponseData.model_validate(raw).repository.root
            targets += [
                _to_target(number, found.get(_alias(number))) for number in chunk
            ]
        return targets
