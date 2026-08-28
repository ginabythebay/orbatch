from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from orbit.config import Milestones
from orbit.github.client import GitHubClient
from orbit.github.models import (
    AlreadyDoneError,
    CloseReason,
    CloseResult,
    CreateEpicResult,
    CreateResult,
    EditBodyResult,
    IssueDetail,
    MoveResult,
    ReorderResult,
    ScheduleResult,
    Surface,
)


def apply_labels(
    client: GitHubClient, node_id: str, label_names: Iterable[str]
) -> None:
    """Resolve each label name to its id and add it to the issue, in order."""
    for name in label_names:
        client.add_label(node_id, client.fetch_label_id(name))


def create_epic(
    client: GitHubClient,
    milestone: str,
    title: str,
    sub_issue_numbers: list[int],
    body: str | None = None,
    labels: Iterable[str] = (),
) -> CreateEpicResult:
    repository_id = client.fetch_repository_id()
    milestone_id = client.fetch_milestone_id(milestone)
    created = client.create_issue(title, repository_id, milestone_id, body)
    apply_labels(client, created.node_id, ("epic", *labels))

    attached: list[int] = []
    for num in sub_issue_numbers:
        detail = client.fetch_issue_detail(num)
        try:
            client.add_sub_issue(created.node_id, detail.node_id)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Epic #{created.number} created but failed to attach"
                + f" #{num}: {exc}"
            ) from exc
        try:
            client.set_issue_milestone(detail.node_id, milestone_id)
        except RuntimeError as exc:
            raise RuntimeError(
                f"#{num} attached to epic #{created.number}"
                + f" but milestone update failed: {exc}"
            ) from exc
        attached.append(num)

    return CreateEpicResult(
        number=created.number,
        title=created.title,
        milestone=milestone,
        sub_issues_attached=tuple(attached),
    )


def create_leaf(
    client: GitHubClient,
    milestones: Milestones,
    destination: str,
    title: str,
    body: str | None = None,
    labels: Iterable[str] = (),
) -> CreateResult:
    parentless = {"shelf": milestones.backlog, "standalone": milestones.current}
    if destination in parentless:
        milestone = parentless[destination]
        repository_id = client.fetch_repository_id()
        milestone_id = client.fetch_milestone_id(milestone)
        created = client.create_issue(title, repository_id, milestone_id, body)
        apply_labels(client, created.node_id, labels)
        return CreateResult(
            number=created.number,
            title=created.title,
            epic_number=None,
            epic_title=None,
            milestone=milestone,
            converted_dest_to_epic=False,
        )

    try:
        epic_number = int(destination)
    except ValueError:
        raise RuntimeError(
            "DESTINATION must be an epic issue number, 'standalone', or 'shelf',"
            + f" got {destination!r}"
        ) from None
    epic = client.fetch_issue_detail(epic_number)

    converted_dest_to_epic = False

    if epic.milestone_id is None:
        raise RuntimeError(f"Epic #{epic_number} has no milestone")

    reopened = reopen_closed_ancestry(client, epic)
    if "epic" not in epic.labels:
        label_id = client.fetch_label_id("epic")
        client.add_label(epic.node_id, label_id)
        converted_dest_to_epic = True

    repository_id = client.fetch_repository_id()
    created = client.create_issue(title, repository_id, epic.milestone_id, body)
    client.add_sub_issue(epic.node_id, created.node_id)
    apply_labels(client, created.node_id, labels)

    return CreateResult(
        number=created.number,
        title=created.title,
        epic_number=epic.number,
        epic_title=epic.title,
        milestone=epic.milestone_title,
        converted_dest_to_epic=converted_dest_to_epic,
        reopened=reopened,
    )


def reopen_closed_ancestry(client: GitHubClient, epic: IssueDetail) -> tuple[int, ...]:
    """Reopen every closed issue from `epic` to the root of its parent chain.

    An epic with an open sub-issue is not done, so nesting is never allowed to
    leave a closed issue anywhere above the new child. Returns the numbers
    reopened, deepest first.
    """
    reopened: list[int] = []
    seen: set[int] = set()
    current: IssueDetail | None = epic
    while current is not None and current.number not in seen:
        seen.add(current.number)
        if current.state != "OPEN":
            client.reopen_issue_by_id(current.node_id)
            reopened.append(current.number)
        current = (
            None
            if current.parent_number is None
            else client.fetch_issue_detail(current.parent_number)
        )
    return tuple(reopened)


def edit_issue_body(
    client: GitHubClient, issue_number: int, body: str
) -> EditBodyResult:
    issue = client.fetch_issue_detail(issue_number)
    client.set_issue_body(issue.node_id, body)
    return EditBodyResult(number=issue.number, title=issue.title)


def close_issue(
    client: GitHubClient, issue_number: int, reason: CloseReason, surface: Surface
) -> CloseResult:
    issue = client.fetch_issue_detail(issue_number)
    if issue.state == "CLOSED":
        raise AlreadyDoneError(f"Issue #{issue_number} is already closed")
    client.add_comment(issue.node_id, _close_note(reason, surface))
    client.close_issue_by_id(issue.node_id, reason)
    return CloseResult(number=issue.number, reason=reason)


def _close_note(reason: CloseReason, surface: Surface) -> str:
    return f"Closed by the {surface} ({reason.replace('_', ' ')})."


def move_issue(client: GitHubClient, issue_number: int, epic_number: int) -> MoveResult:
    if issue_number == epic_number:
        raise RuntimeError(f"Issue #{issue_number} cannot be moved under itself")
    issue = client.fetch_issue_detail(issue_number)
    epic = client.fetch_issue_detail(epic_number)

    converted_dest_to_epic = False

    reopened = reopen_closed_ancestry(client, epic)
    if issue.parent_node_id == epic.node_id:
        if not reopened:
            raise AlreadyDoneError(
                f"Issue #{issue_number} is already under epic #{epic_number}"
            )
        return MoveResult(
            issue_number=issue.number,
            issue_title=issue.title,
            epic_number=epic.number,
            epic_title=epic.title,
            old_epic_number=None,
            old_epic_title=None,
            milestone=epic.milestone_title,
            converted_dest_to_epic=False,
            reopened=reopened,
            already_done=True,
        )
    if "epic" not in epic.labels:
        label_id = client.fetch_label_id("epic")
        client.add_label(epic.node_id, label_id)
        converted_dest_to_epic = True

    old_epic_number: int | None = None
    old_epic_title: str | None = None
    if issue.parent_node_id is not None:
        old_epic_number = issue.parent_number
        old_epic_title = issue.parent_title
        client.remove_sub_issue(issue.parent_node_id, issue.node_id)

    try:
        client.add_sub_issue(epic.node_id, issue.node_id)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Failed to attach #{issue_number} to #{epic_number}"
            + f" (issue may be detached from #{old_epic_number}): {exc}"
        ) from exc

    if epic.milestone_id is not None:
        try:
            client.set_issue_milestone(issue.node_id, epic.milestone_id)
        except RuntimeError as exc:
            raise RuntimeError(
                f"#{issue_number} moved to #{epic_number}"
                + f" but milestone update failed: {exc}"
            ) from exc

    return MoveResult(
        issue_number=issue.number,
        issue_title=issue.title,
        epic_number=epic.number,
        epic_title=epic.title,
        old_epic_number=old_epic_number,
        old_epic_title=old_epic_title,
        milestone=epic.milestone_title,
        converted_dest_to_epic=converted_dest_to_epic,
        reopened=reopened,
    )


def reorder_issue(
    client: GitHubClient,
    issue_number: int,
    after_number: int | None = None,
    before_number: int | None = None,
) -> ReorderResult:
    """Passing neither after_number nor before_number places it first,
    returning already_done=True without a mutation when it already is.
    Raises RuntimeError when the parent epic reports no children."""
    reference_number = after_number if after_number is not None else before_number
    if reference_number == issue_number:
        raise RuntimeError(
            f"Issue #{issue_number} cannot be positioned relative to itself"
        )

    issue = client.fetch_issue_detail(issue_number)
    if (
        issue.parent_node_id is None
        or issue.parent_number is None
        or issue.parent_title is None
    ):
        raise RuntimeError(f"Issue #{issue_number} is not in an epic")

    reference_node_id: str | None = None
    reference_title: str | None = None
    if reference_number is not None:
        reference = client.fetch_issue_detail(reference_number)
        if reference.parent_node_id is None:
            raise RuntimeError(f"Issue #{reference_number} is not in an epic")
        if reference.parent_node_id != issue.parent_node_id:
            raise RuntimeError(
                f"Issue #{issue_number} is under epic #{issue.parent_number}"
                + f" but #{reference_number} is under epic"
                + f" #{reference.parent_number}"
            )
        reference_node_id = reference.node_id
        reference_title = reference.title

    already_done = False
    if reference_node_id is None:
        first_child = client.fetch_first_child(issue.parent_number)
        if first_child is None:
            raise RuntimeError(
                f"Epic #{issue.parent_number} reports no children"
                + f" but #{issue_number} claims it as its parent"
            )
        position: Literal["first", "after", "before"] = "first"
        already_done = first_child.number == issue.number
        if not already_done:
            client.reprioritize_sub_issue(
                issue.parent_node_id, issue.node_id, before_id=first_child.node_id
            )
    elif after_number is not None:
        client.reprioritize_sub_issue(
            issue.parent_node_id, issue.node_id, after_id=reference_node_id
        )
        position = "after"
    else:
        client.reprioritize_sub_issue(
            issue.parent_node_id, issue.node_id, before_id=reference_node_id
        )
        position = "before"

    return ReorderResult(
        issue_number=issue.number,
        issue_title=issue.title,
        epic_number=issue.parent_number,
        epic_title=issue.parent_title,
        position=position,
        reference_number=reference_number,
        reference_title=reference_title,
        already_done=already_done,
    )


def schedule_issue(
    client: GitHubClient, issue_number: int, milestone: str
) -> ScheduleResult:
    issue = client.fetch_issue_detail(issue_number)

    # Detach the epic parent iff the epic lives in a different milestone
    # than the target: a leaf shares its epic's milestone, so once they
    # diverge the parent link no longer holds. Scheduling to the epic's
    # own milestone (or an epic with no parent) keeps the link.
    old_epic_number: int | None = None
    old_epic_title: str | None = None
    detach_node_id: str | None = None
    parent_number = issue.parent_number
    parent_node_id = issue.parent_node_id
    if parent_number is not None and parent_node_id is not None:
        parent_detail = client.fetch_issue_detail(parent_number)
        if parent_detail.milestone_title != milestone:
            detach_node_id = parent_node_id
            old_epic_number = parent_number
            old_epic_title = issue.parent_title

    if issue.milestone_title == milestone and detach_node_id is None:
        raise AlreadyDoneError(
            f"Issue #{issue_number} is already in milestone {milestone!r}"
        )

    # Resolve the target only once a change is certain; this also validates
    # the milestone name (raising on an unknown one) for real mutations.
    milestone_id = client.fetch_milestone_id(milestone)

    if detach_node_id is not None:
        client.remove_sub_issue(detach_node_id, issue.node_id)

    try:
        client.set_issue_milestone(issue.node_id, milestone_id)
    except RuntimeError as exc:
        detached = (
            f" (issue may be detached from #{old_epic_number})"
            if old_epic_number is not None
            else ""
        )
        raise RuntimeError(
            f"Milestone update failed for #{issue_number}{detached}: {exc}"
        ) from exc

    return ScheduleResult(
        issue_number=issue.number,
        issue_title=issue.title,
        milestone=milestone,
        old_epic_number=old_epic_number,
        old_epic_title=old_epic_title,
    )
