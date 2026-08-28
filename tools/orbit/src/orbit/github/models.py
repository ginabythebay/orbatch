from __future__ import annotations

from datetime import date
from enum import StrEnum, auto
from typing import Literal

from pydantic import BaseModel


class AlreadyDoneError(Exception):
    pass


class CloseReason(StrEnum):
    COMPLETED = auto()
    DUPLICATE = auto()
    NOT_PLANNED = auto()


class Surface(StrEnum):
    TUI = "orbit TUI"
    CLI = "orbit CLI"


class Issue(BaseModel, frozen=True):
    number: int
    state: str
    title: str


class MilestoneIssue(Issue, frozen=True):
    parent_number: int | None
    is_epic: bool


class ParentRef(BaseModel, frozen=True):
    number: int
    title: str
    created_at: date


class PeriodIssue(BaseModel, frozen=True):
    number: int
    title: str
    state: str
    created_at: date
    closed_at: date | None
    is_epic: bool
    parent: ParentRef | None
    milestone: str | None


class PeriodPR(BaseModel, frozen=True):
    number: int
    title: str
    merged_at: date
    merge_commit_oid: str | None


class IssueDetail(BaseModel, frozen=True):
    node_id: str
    number: int
    state: str
    title: str
    body: str
    labels: tuple[str, ...]
    milestone_id: str | None
    milestone_title: str | None
    parent_number: int | None
    parent_node_id: str | None
    parent_title: str | None


class ChildRef(BaseModel, frozen=True):
    node_id: str
    number: int
    title: str


class MoveResult(BaseModel, frozen=True):
    issue_number: int
    issue_title: str
    epic_number: int
    epic_title: str
    old_epic_number: int | None
    old_epic_title: str | None
    milestone: str | None
    converted_dest_to_epic: bool
    reopened: tuple[int, ...] = ()
    already_done: bool = False


class ReorderResult(BaseModel, frozen=True):
    issue_number: int
    issue_title: str
    epic_number: int
    epic_title: str
    position: Literal["first", "after", "before"]
    reference_number: int | None
    reference_title: str | None
    already_done: bool = False


class ScheduleResult(BaseModel, frozen=True):
    issue_number: int
    issue_title: str
    milestone: str
    old_epic_number: int | None
    old_epic_title: str | None
    already_done: bool = False


class SubIssueData(BaseModel, frozen=True):
    number: int
    state: str
    title: str
    children: tuple[SubIssueData, ...]


class CreatedIssue(BaseModel, frozen=True):
    node_id: str
    number: int
    title: str


class CreateEpicResult(BaseModel, frozen=True):
    number: int
    title: str
    milestone: str
    sub_issues_attached: tuple[int, ...]


class CreateResult(BaseModel, frozen=True):
    number: int
    title: str
    epic_number: int | None
    epic_title: str | None
    milestone: str | None
    converted_dest_to_epic: bool
    reopened: tuple[int, ...] = ()


class EditBodyResult(BaseModel, frozen=True):
    number: int
    title: str


class CloseResult(BaseModel, frozen=True):
    number: int
    reason: CloseReason
    already_done: bool = False


class Epic(BaseModel, frozen=True):
    number: int
    state: str
    title: str
    open_count: int
    total_count: int


class MilestoneSummary(BaseModel, frozen=True):
    title: str
    state: str
    due_on: date | None = None
