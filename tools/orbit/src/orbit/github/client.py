from __future__ import annotations

from datetime import date, datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ghgql.repo import Repo, repo
from ghgql.transport import GitHubGraphQL, GitHubTransport
from orbit.github.models import (
    ChildRef,
    CloseReason,
    CreatedIssue,
    Epic,
    Issue,
    IssueDetail,
    MilestoneIssue,
    MilestoneSummary,
    ParentRef,
    PeriodIssue,
    PeriodPR,
    SubIssueData,
)

_MILESTONE_ID_QUERY = """
query($milestone: String!, $owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    milestones(query: $milestone, first: 1) {
      nodes {
        id
        title
      }
    }
  }
}
"""


class _MilestoneIdNode(BaseModel):
    id: str
    title: str


class _MilestoneIdNodes(BaseModel):
    nodes: list[_MilestoneIdNode]


class _MilestoneIdRepo(BaseModel):
    milestones: _MilestoneIdNodes


class _MilestoneIdResponseData(BaseModel):
    repository: _MilestoneIdRepo


_LIST_MILESTONES_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    milestones(states: [OPEN], first: 100) {
      nodes {
        title
        state
        dueOn
      }
    }
  }
}
"""

_LIST_ALL_MILESTONES_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    milestones(states: [OPEN, CLOSED], first: 100) {
      nodes {
        title
        state
        dueOn
      }
    }
  }
}
"""


class _OpenMilestoneNode(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    title: str
    state: str
    due_on: datetime | None = Field(default=None, alias="dueOn")


class _OpenMilestoneNodes(BaseModel):
    nodes: list[_OpenMilestoneNode]


class _OpenMilestoneRepo(BaseModel):
    milestones: _OpenMilestoneNodes


class _OpenMilestoneResponseData(BaseModel):
    repository: _OpenMilestoneRepo


_LIST_ISSUES_QUERY = """
query($milestone: String!, $owner: String!, $name: String!, $labels: [String!], $after: String) {
  repository(owner: $owner, name: $name) {
    milestones(query: $milestone, first: 1) {
      nodes {
        title
        issues(first: 100, labels: $labels, after: $after) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            number
            state
            title
            parent {
              number
            }
            labels(first: 20) {
              nodes {
                name
              }
            }
          }
        }
      }
    }
  }
}
"""

_SUB_ISSUES_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      subIssues(first: 100) {
        totalCount
        nodes {
          number
          state
          title
          subIssues(first: 100) {
            totalCount
          }
        }
      }
    }
  }
}
"""

_FIRST_CHILD_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      subIssues(first: 1) {
        nodes {
          id
          number
          title
        }
      }
    }
  }
}
"""

_PARENT_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      parent {
        number
        state
        title
      }
    }
  }
}
"""

_LIST_EPICS_QUERY = """
query($milestone: String!, $owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    milestones(query: $milestone, first: 1) {
      nodes {
        title
        issues(first: 100, labels: ["epic"], after: $after) {
          pageInfo {
            hasNextPage
            endCursor
          }
          nodes {
            number
            state
            title
            subIssues(first: 100) {
              totalCount
              nodes {
                state
              }
            }
          }
        }
      }
    }
  }
}
"""

_SEARCH_ISSUES_QUERY = """
query($q: String!) {
  search(query: $q, type: ISSUE, first: 100) {
    nodes {
      ... on Issue {
        number
        state
        title
      }
    }
  }
}
"""

_ISSUE_DETAIL_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      id
      number
      state
      title
      body
      labels(first: 100) { nodes { name } }
      milestone { id title }
      parent { id number title }
    }
  }
}
"""


class _SubIssueState(BaseModel):
    state: str


class _SubIssueNodes(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    total_count: int = Field(alias="totalCount")
    nodes: list[_SubIssueState]


class _EpicIssue(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    number: int
    state: str
    title: str
    sub_issues: _SubIssueNodes = Field(alias="subIssues")


class _PageInfo(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    has_next_page: bool = Field(alias="hasNextPage")
    end_cursor: str | None = Field(default=None, alias="endCursor")


class _IssueContainer[T](BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    page_info: _PageInfo = Field(alias="pageInfo")
    nodes: list[T]


class _Milestone[T](BaseModel):
    title: str
    issues: _IssueContainer[T]


class _MilestoneNodes[T](BaseModel):
    nodes: list[_Milestone[T]]


class _Repository[T](BaseModel):
    milestones: _MilestoneNodes[T]


class _ResponseData[T](BaseModel):
    repository: _Repository[T]


class _SubCount(BaseModel):
    total_count: int = Field(alias="totalCount")


class _SubIssueChild(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    number: int
    state: str
    title: str
    sub_issues: _SubCount = Field(alias="subIssues")


class _SubConnection(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    total_count: int = Field(alias="totalCount")
    nodes: list[_SubIssueChild]


class _IssueWithSubs(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    sub_issues: _SubConnection = Field(alias="subIssues")


class _SubIssueRepo(BaseModel):
    issue: _IssueWithSubs | None = None


class _SubIssueResponseData(BaseModel):
    repository: _SubIssueRepo


class _FirstChildNode(BaseModel):
    id: str
    number: int
    title: str


class _FirstChildConnection(BaseModel):
    nodes: list[_FirstChildNode]


class _IssueWithFirstChild(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    sub_issues: _FirstChildConnection = Field(alias="subIssues")


class _FirstChildRepo(BaseModel):
    issue: _IssueWithFirstChild | None = None


class _FirstChildResponseData(BaseModel):
    repository: _FirstChildRepo


class _IssueWithParent(BaseModel):
    parent: Issue | None = None


class _ParentRepo(BaseModel):
    issue: _IssueWithParent | None = None


class _ParentResponseData(BaseModel):
    repository: _ParentRepo


class _LabelNode(BaseModel):
    name: str


class _LabelConnection(BaseModel):
    nodes: list[_LabelNode]


class _MilestoneDetail(BaseModel):
    id: str
    title: str


class _ParentDetail(BaseModel):
    id: str
    number: int
    title: str


class _IssueDetailRaw(BaseModel):
    id: str
    number: int
    state: str
    title: str
    body: str
    labels: _LabelConnection
    milestone: _MilestoneDetail | None = None
    parent: _ParentDetail | None = None


class _IssueDetailRepo(BaseModel):
    issue: _IssueDetailRaw | None = None


class _IssueDetailResponseData(BaseModel):
    repository: _IssueDetailRepo


def _same_milestone(actual: str, requested: str) -> bool:
    """Whether two milestone names refer to the same milestone.

    GitHub milestone names are matched case-insensitively everywhere in
    orbit, so an operator can pass 'backlog' for the 'Backlog' milestone.
    """
    return actual.casefold() == requested.casefold()


_PERIOD_SEARCH_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      ... on Issue {
        number
        title
        state
        createdAt
        closedAt
        labels(first: 100) { nodes { name } }
        parent { number title createdAt }
        milestone { title }
      }
    }
  }
}
"""

_PERIOD_PR_SEARCH_QUERY = """
query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 100, after: $after) {
    pageInfo {
      hasNextPage
      endCursor
    }
    nodes {
      ... on PullRequest {
        number
        title
        mergedAt
        mergeCommit { oid }
      }
    }
  }
}
"""


class _LabelName(BaseModel):
    name: str


class _LabelNodes(BaseModel):
    nodes: list[_LabelName]


class _ParentNumberNode(BaseModel):
    number: int


class _MilestoneIssueNode(BaseModel):
    number: int
    state: str
    title: str
    parent: _ParentNumberNode | None = None
    labels: _LabelNodes | None = None


class _PeriodParentNode(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    number: int
    title: str
    created_at: datetime = Field(alias="createdAt")


class _PeriodMilestoneNode(BaseModel):
    title: str


class _PeriodSearchNode(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    number: int | None = None
    title: str | None = None
    state: str | None = None
    created_at: datetime | None = Field(default=None, alias="createdAt")
    closed_at: datetime | None = Field(default=None, alias="closedAt")
    labels: _LabelNodes | None = None
    parent: _PeriodParentNode | None = None
    milestone: _PeriodMilestoneNode | None = None


class _MergeCommitNode(BaseModel):
    oid: str


class _PeriodPRNode(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    number: int | None = None
    title: str | None = None
    merged_at: datetime | None = Field(default=None, alias="mergedAt")
    merge_commit: _MergeCommitNode | None = Field(default=None, alias="mergeCommit")


class _PeriodSearchConnection(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    page_info: _PageInfo = Field(alias="pageInfo")
    nodes: list[_PeriodSearchNode]


class _PeriodSearchResponseData(BaseModel):
    search: _PeriodSearchConnection


class _PeriodPRConnection(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    page_info: _PageInfo = Field(alias="pageInfo")
    nodes: list[_PeriodPRNode]


class _PeriodPRResponseData(BaseModel):
    search: _PeriodPRConnection


class _SearchNode(BaseModel):
    number: int | None = None
    state: str | None = None
    title: str | None = None


class _SearchConnection(BaseModel):
    nodes: list[_SearchNode]


class _SearchResponseData(BaseModel):
    search: _SearchConnection


_ADD_SUB_ISSUE_MUTATION = """
mutation($parentId: ID!, $childId: ID!) {
  addSubIssue(input: {issueId: $parentId, subIssueId: $childId}) {
    clientMutationId
  }
}
"""

_REMOVE_SUB_ISSUE_MUTATION = """
mutation($parentId: ID!, $childId: ID!) {
  removeSubIssue(input: {issueId: $parentId, subIssueId: $childId}) {
    clientMutationId
  }
}
"""

_REPRIORITIZE_SUB_ISSUE_MUTATION = """
mutation($parentId: ID!, $childId: ID!, $afterId: ID, $beforeId: ID) {
  reprioritizeSubIssue(input: {issueId: $parentId, subIssueId: $childId, afterId: $afterId, beforeId: $beforeId}) {
    clientMutationId
  }
}
"""

_SET_MILESTONE_MUTATION = """
mutation($issueId: ID!, $milestoneId: ID!) {
  updateIssue(input: {id: $issueId, milestoneId: $milestoneId}) {
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

_ADD_COMMENT_MUTATION = """
mutation($subjectId: ID!, $body: String!) {
  addComment(input: {subjectId: $subjectId, body: $body}) {
    clientMutationId
  }
}
"""

_CLOSE_ISSUE_MUTATION = """
mutation($issueId: ID!, $reason: IssueClosedStateReason!) {
  closeIssue(input: {issueId: $issueId, stateReason: $reason}) {
    clientMutationId
  }
}
"""

_REOPEN_ISSUE_MUTATION = """
mutation($issueId: ID!) {
  reopenIssue(input: {issueId: $issueId}) {
    clientMutationId
  }
}
"""


_REPO_ID_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    id
  }
}
"""

_LABEL_ID_QUERY = """
query($owner: String!, $name: String!, $labelName: String!) {
  repository(owner: $owner, name: $name) {
    label(name: $labelName) {
      id
    }
  }
}
"""

_CREATE_ISSUE_MUTATION = """
mutation($repositoryId: ID!, $title: String!, $milestoneId: ID!, $body: String) {
  createIssue(input: {repositoryId: $repositoryId, title: $title, milestoneId: $milestoneId, body: $body}) {
    issue {
      id
      number
      title
    }
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


class _RepoIdRepo(BaseModel):
    id: str


class _RepoIdResponseData(BaseModel):
    repository: _RepoIdRepo


class _LabelIdNode(BaseModel):
    id: str


class _LabelIdRepo(BaseModel):
    label: _LabelIdNode | None = None


class _LabelIdResponseData(BaseModel):
    repository: _LabelIdRepo


class _CreatedIssueNode(BaseModel):
    id: str
    number: int
    title: str


class _CreateIssueMutationPayload(BaseModel):
    issue: _CreatedIssueNode


class _CreateIssueData(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    create_issue: _CreateIssueMutationPayload = Field(alias="createIssue")


def _milestone_issue(node: _MilestoneIssueNode) -> MilestoneIssue:
    labels = node.labels.nodes if node.labels is not None else []
    return MilestoneIssue(
        number=node.number,
        state=node.state,
        title=node.title,
        parent_number=node.parent.number if node.parent is not None else None,
        is_epic=any(label.name == "epic" for label in labels),
    )


def _period_issue(node: _PeriodSearchNode) -> PeriodIssue:
    if node.number is None or node.title is None or node.state is None:
        raise RuntimeError(f"Search returned an incomplete issue node: {node!r}")
    if node.created_at is None:
        raise RuntimeError(f"Issue #{node.number} came back without a creation date")
    labels = node.labels.nodes if node.labels is not None else []
    parent = (
        ParentRef(
            number=node.parent.number,
            title=node.parent.title,
            created_at=node.parent.created_at.date(),
        )
        if node.parent is not None
        else None
    )
    return PeriodIssue(
        number=node.number,
        title=node.title,
        state=node.state,
        created_at=node.created_at.date(),
        closed_at=node.closed_at.date() if node.closed_at is not None else None,
        is_epic=any(label.name == "epic" for label in labels),
        parent=parent,
        milestone=node.milestone.title if node.milestone is not None else None,
    )


def _period_pr(node: _PeriodPRNode) -> PeriodPR:
    if node.number is None or node.title is None:
        raise RuntimeError(f"Search returned an incomplete pull request node: {node!r}")
    if node.merged_at is None:
        raise RuntimeError(f"PR #{node.number} came back without a merge date")
    return PeriodPR(
        number=node.number,
        title=node.title,
        merged_at=node.merged_at.date(),
        merge_commit_oid=node.merge_commit.oid if node.merge_commit else None,
    )


class GitHubClient:
    def __init__(self, graphql: GitHubGraphQL, repo: Repo) -> None:
        self._graphql: GitHubGraphQL = graphql
        self.repo: Repo = repo

    def _fetch_milestone_issues[T](
        self,
        query: str,
        response_type: type[_ResponseData[T]],
        milestone: str,
        *,
        label: str | None = None,
    ) -> list[T]:
        owner, name = self.repo
        kwargs: dict[str, str | int | None] = {
            "milestone": milestone,
            "owner": owner,
            "name": name,
        }
        if label is not None:
            kwargs["labels"] = label
        nodes: list[T] = []
        after: str | None = None
        while True:
            raw = self._graphql.run(query, after=after, **kwargs)
            response = response_type.model_validate(raw)
            milestones = response.repository.milestones.nodes
            if not milestones:
                return nodes
            if not _same_milestone(milestones[0].title, milestone):
                raise RuntimeError(
                    f"Expected milestone {milestone!r}, got {milestones[0].title!r}"
                )
            connection = milestones[0].issues
            nodes.extend(connection.nodes)
            cursor = connection.page_info.end_cursor
            if not connection.page_info.has_next_page or cursor is None:
                return nodes
            after = cursor

    def list_issues_by_milestone(
        self,
        milestone: str,
        *,
        label: str | None = None,
    ) -> list[MilestoneIssue]:
        nodes = self._fetch_milestone_issues(
            _LIST_ISSUES_QUERY,
            _ResponseData[_MilestoneIssueNode],
            milestone,
            label=label,
        )
        return [_milestone_issue(node) for node in nodes]

    def search_issue_titles(self, query: str) -> list[Issue]:
        """Search issue titles in the current repo, returning matching issues.

        Scoped to the repo with ``in:title type:issue`` so non-issue and
        body-only matches are excluded. Nodes that are not issues (the search
        payload can include other types) are skipped.
        """
        owner, name = self.repo
        q = f"repo:{owner}/{name} in:title type:issue {query}"
        raw = self._graphql.run(_SEARCH_ISSUES_QUERY, q=q)
        response = _SearchResponseData.model_validate(raw)
        issues: list[Issue] = []
        for node in response.search.nodes:
            if node.number is None or node.state is None or node.title is None:
                continue
            issues.append(Issue(number=node.number, state=node.state, title=node.title))
        return issues

    def search_period_issues(self, start: date, end: date) -> list[PeriodIssue]:
        owner, name = self.repo
        scope = f"repo:{owner}/{name} type:issue"
        found: dict[int, PeriodIssue] = {}
        for qualifier in (f"created:{start}..{end}", f"closed:{start}..{end}"):
            for issue in self._search_period_pages(f"{scope} {qualifier}"):
                found.setdefault(issue.number, issue)
        return sorted(found.values(), key=lambda issue: issue.number)

    def _search_period_pages(self, q: str) -> list[PeriodIssue]:
        issues: list[PeriodIssue] = []
        after: str | None = None
        while True:
            raw = self._graphql.run(_PERIOD_SEARCH_QUERY, q=q, after=after)
            connection = _PeriodSearchResponseData.model_validate(raw).search
            issues.extend(
                _period_issue(node)
                for node in connection.nodes
                if node.number is not None
            )
            cursor = connection.page_info.end_cursor
            if not connection.page_info.has_next_page or cursor is None:
                return issues
            after = cursor

    def search_period_prs(self, start: date, end: date) -> list[PeriodPR]:
        owner, name = self.repo
        q = f"repo:{owner}/{name} type:pr is:merged merged:{start}..{end}"
        prs: list[PeriodPR] = []
        after: str | None = None
        while True:
            raw = self._graphql.run(_PERIOD_PR_SEARCH_QUERY, q=q, after=after)
            connection = _PeriodPRResponseData.model_validate(raw).search
            prs.extend(
                _period_pr(node) for node in connection.nodes if node.number is not None
            )
            cursor = connection.page_info.end_cursor
            if not connection.page_info.has_next_page or cursor is None:
                return prs
            after = cursor

    def fetch_sub_issue_tree(self, issue_number: int) -> list[SubIssueData]:
        owner, name = self.repo
        return self._fetch_children(owner, name, issue_number, visited=set())

    def _fetch_children(
        self,
        owner: str,
        name: str,
        issue_number: int,
        *,
        visited: set[int],
    ) -> list[SubIssueData]:
        if issue_number in visited:
            raise RuntimeError(
                f"Cycle detected: issue #{issue_number} is its own ancestor"
            )
        visited = visited | {issue_number}
        raw = self._graphql.run(
            _SUB_ISSUES_QUERY,
            owner=owner,
            name=name,
            number=issue_number,
        )
        response = _SubIssueResponseData.model_validate(raw)
        if response.repository.issue is None:
            return []
        connection = response.repository.issue.sub_issues
        if connection.total_count != len(connection.nodes):
            msg = (
                f"Issue #{issue_number} has {connection.total_count} sub-issues"
                f" but only {len(connection.nodes)} were fetched"
            )
            raise RuntimeError(msg)
        result: list[SubIssueData] = []
        for node in connection.nodes:
            if node.sub_issues.total_count > 0:
                children = tuple(
                    self._fetch_children(owner, name, node.number, visited=visited),
                )
            else:
                children = ()
            result.append(
                SubIssueData(
                    number=node.number,
                    state=node.state,
                    title=node.title,
                    children=children,
                )
            )
        return result

    def fetch_parent_issue(self, issue_number: int) -> Issue | None:
        owner, name = self.repo
        raw = self._graphql.run(
            _PARENT_QUERY,
            owner=owner,
            name=name,
            number=issue_number,
        )
        response = _ParentResponseData.model_validate(raw)
        issue_data = response.repository.issue
        if issue_data is None:
            return None
        return issue_data.parent

    def list_epics_by_milestone(self, milestone: str) -> list[Epic]:
        raw = self._fetch_milestone_issues(
            _LIST_EPICS_QUERY,
            _ResponseData[_EpicIssue],
            milestone,
        )
        epics: list[Epic] = []
        for issue in raw:
            sub = issue.sub_issues
            if sub.total_count != len(sub.nodes):
                msg = (
                    f"Epic #{issue.number} has {sub.total_count} sub-issues"
                    f" but only {len(sub.nodes)} were fetched"
                )
                raise RuntimeError(msg)
            open_count = sum(1 for s in sub.nodes if s.state == "OPEN")
            epics.append(
                Epic(
                    number=issue.number,
                    state=issue.state,
                    title=issue.title,
                    open_count=open_count,
                    total_count=sub.total_count,
                )
            )
        return epics

    def list_milestones(
        self, *, include_closed: bool = False
    ) -> list[MilestoneSummary]:
        """Milestones in the repo, open ones only unless include_closed."""
        owner, name = self.repo
        query = _LIST_ALL_MILESTONES_QUERY if include_closed else _LIST_MILESTONES_QUERY
        raw = self._graphql.run(query, owner=owner, name=name)
        response = _OpenMilestoneResponseData.model_validate(raw)
        return [
            MilestoneSummary(
                title=node.title,
                state=node.state,
                due_on=node.due_on.date() if node.due_on is not None else None,
            )
            for node in response.repository.milestones.nodes
        ]

    def fetch_milestone_id(self, milestone: str) -> str:
        owner, name = self.repo
        raw = self._graphql.run(
            _MILESTONE_ID_QUERY,
            owner=owner,
            name=name,
            milestone=milestone,
        )
        response = _MilestoneIdResponseData.model_validate(raw)
        nodes = response.repository.milestones.nodes
        if not nodes:
            raise RuntimeError(f"Milestone {milestone!r} not found")
        if not _same_milestone(nodes[0].title, milestone):
            raise RuntimeError(
                f"Expected milestone {milestone!r}, got {nodes[0].title!r}"
            )
        return nodes[0].id

    def fetch_issue_detail(self, number: int) -> IssueDetail:
        owner, name = self.repo
        raw = self._graphql.run(
            _ISSUE_DETAIL_QUERY,
            owner=owner,
            name=name,
            number=number,
        )
        response = _IssueDetailResponseData.model_validate(raw)
        issue = response.repository.issue
        if issue is None:
            raise RuntimeError(f"Issue #{number} not found")
        return IssueDetail(
            node_id=issue.id,
            number=issue.number,
            state=issue.state,
            title=issue.title,
            body=issue.body,
            labels=tuple(label.name for label in issue.labels.nodes),
            milestone_id=issue.milestone.id if issue.milestone else None,
            milestone_title=issue.milestone.title if issue.milestone else None,
            parent_number=issue.parent.number if issue.parent else None,
            parent_node_id=issue.parent.id if issue.parent else None,
            parent_title=issue.parent.title if issue.parent else None,
        )

    def fetch_first_child(self, parent_number: int) -> ChildRef | None:
        owner, name = self.repo
        raw = self._graphql.run(
            _FIRST_CHILD_QUERY,
            owner=owner,
            name=name,
            number=parent_number,
        )
        response = _FirstChildResponseData.model_validate(raw)
        issue = response.repository.issue
        if issue is None:
            raise RuntimeError(f"Issue #{parent_number} not found")
        nodes = issue.sub_issues.nodes
        if not nodes:
            return None
        return ChildRef(
            node_id=nodes[0].id, number=nodes[0].number, title=nodes[0].title
        )

    def add_sub_issue(self, parent_node_id: str, child_node_id: str) -> None:
        self._graphql.run(
            _ADD_SUB_ISSUE_MUTATION,
            parentId=parent_node_id,
            childId=child_node_id,
        )

    def add_comment(self, subject_node_id: str, body: str) -> None:
        self._graphql.run(
            _ADD_COMMENT_MUTATION,
            subjectId=subject_node_id,
            body=body,
        )

    def close_issue_by_id(self, node_id: str, reason: CloseReason) -> None:
        self._graphql.run(
            _CLOSE_ISSUE_MUTATION,
            issueId=node_id,
            reason=reason.upper(),
        )

    def reopen_issue_by_id(self, node_id: str) -> None:
        self._graphql.run(_REOPEN_ISSUE_MUTATION, issueId=node_id)

    def remove_sub_issue(self, parent_node_id: str, child_node_id: str) -> None:
        self._graphql.run(
            _REMOVE_SUB_ISSUE_MUTATION,
            parentId=parent_node_id,
            childId=child_node_id,
        )

    def reprioritize_sub_issue(
        self,
        parent_node_id: str,
        child_node_id: str,
        after_id: str | None = None,
        before_id: str | None = None,
    ) -> None:
        """Exactly one of after_id and before_id is required; GitHub rejects
        a call that carries neither."""
        if after_id is not None and before_id is not None:
            raise ValueError("after_id and before_id are mutually exclusive")
        if after_id is None and before_id is None:
            raise ValueError("one of after_id or before_id is required")
        variables: dict[str, str] = {
            "parentId": parent_node_id,
            "childId": child_node_id,
        }
        if after_id is not None:
            variables["afterId"] = after_id
        if before_id is not None:
            variables["beforeId"] = before_id
        self._graphql.run(_REPRIORITIZE_SUB_ISSUE_MUTATION, **variables)

    def set_issue_milestone(self, issue_node_id: str, milestone_id: str) -> None:
        self._graphql.run(
            _SET_MILESTONE_MUTATION,
            issueId=issue_node_id,
            milestoneId=milestone_id,
        )

    def set_issue_body(self, issue_node_id: str, body: str) -> None:
        self._graphql.run(
            _SET_BODY_MUTATION,
            issueId=issue_node_id,
            body=body,
        )

    def fetch_repository_id(self) -> str:
        owner, name = self.repo
        raw = self._graphql.run(_REPO_ID_QUERY, owner=owner, name=name)
        response = _RepoIdResponseData.model_validate(raw)
        return response.repository.id

    def fetch_label_id(self, label_name: str) -> str:
        owner, name = self.repo
        raw = self._graphql.run(
            _LABEL_ID_QUERY, owner=owner, name=name, labelName=label_name
        )
        response = _LabelIdResponseData.model_validate(raw)
        if response.repository.label is None:
            raise RuntimeError(f"Label {label_name!r} not found")
        return response.repository.label.id

    def create_issue(
        self,
        title: str,
        repository_id: str,
        milestone_id: str,
        body: str | None = None,
    ) -> CreatedIssue:
        variables: dict[str, str | int] = {
            "repositoryId": repository_id,
            "title": title,
            "milestoneId": milestone_id,
        }
        if body is not None:
            variables["body"] = body
        raw = self._graphql.run(_CREATE_ISSUE_MUTATION, **variables)
        response = _CreateIssueData.model_validate(raw)
        node = response.create_issue.issue
        return CreatedIssue(node_id=node.id, number=node.number, title=node.title)

    def add_label(self, issue_node_id: str, label_node_id: str) -> None:
        self._graphql.run(
            _ADD_LABEL_MUTATION,
            labelableId=issue_node_id,
            labelId=label_node_id,
        )


def github_client() -> GitHubClient:
    return GitHubClient(GitHubGraphQL(GitHubTransport()), repo())
