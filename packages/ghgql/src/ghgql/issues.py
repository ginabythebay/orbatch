from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, RootModel

from ghgql.repo import Repo
from ghgql.transport import GitHubGraphQL


def _label_ids_query(names: Sequence[str]) -> str:
    declarations = " ".join(f"${_alias(index)}: String!" for index in range(len(names)))
    selections = "\n".join(
        f"    {_alias(index)}: label(name: ${_alias(index)}) {{ id }}"
        for index in range(len(names))
    )
    return f"""query($owner: String!, $name: String!, {declarations}) {{
  repository(owner: $owner, name: $name) {{
{selections}
  }}
}}
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


def _alias(index: int) -> str:
    return f"l{index}"


class LabelNode(BaseModel):
    name: str


class LabelConnection(BaseModel):
    nodes: list[LabelNode]


class IssueCore(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(populate_by_name=True)
    id: str
    number: int
    state: str
    title: str
    labels: LabelConnection


class _LabelIdNode(BaseModel):
    id: str


class _LabelIdRepo(RootModel[dict[str, _LabelIdNode | None]]):
    pass


class _LabelIdResponseData(BaseModel):
    repository: _LabelIdRepo


class IssueMutations:
    def __init__(self, graphql: GitHubGraphQL, repo: Repo) -> None:
        self._graphql: GitHubGraphQL = graphql
        self._repo: Repo = repo
        self._cache: dict[str, str] = {}

    def label_ids(self, names: Sequence[str]) -> dict[str, str]:
        if not names:
            return {}
        owner, name = self._repo
        variables: dict[str, str | int | None] = {"owner": owner, "name": name}
        variables.update({_alias(index): label for index, label in enumerate(names)})
        raw = self._graphql.run(_label_ids_query(names), **variables)
        found = _LabelIdResponseData.model_validate(raw).repository.root
        nodes = {label: found.get(_alias(index)) for index, label in enumerate(names)}
        missing = [label for label, node in nodes.items() if node is None]
        if missing:
            raise RuntimeError(f"Labels not found in repo: {', '.join(missing)}")
        return {label: node.id for label, node in nodes.items() if node is not None}

    def label_id(self, name: str, group: Sequence[str] = ()) -> str:
        """`group` is fetched with `name`, so a known set of labels costs one query."""
        if name not in self._cache:
            wanted = [
                label
                for label in dict.fromkeys((*group, name))
                if label not in self._cache
            ]
            self._cache.update(self.label_ids(wanted))
        return self._cache[name]

    def add_label(self, node_id: str, label_id: str) -> None:
        self._graphql.run(_ADD_LABEL_MUTATION, labelableId=node_id, labelId=label_id)

    def remove_label(self, node_id: str, label_id: str) -> None:
        self._graphql.run(_REMOVE_LABEL_MUTATION, labelableId=node_id, labelId=label_id)

    def set_issue_body(self, node_id: str, body: str) -> None:
        self._graphql.run(_SET_BODY_MUTATION, issueId=node_id, body=body)
