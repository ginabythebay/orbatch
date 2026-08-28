"""What happened over a period — epics, PRs, commits, deploys, docs — for snippets.

Usage:
    dev/snippets.py
    dev/snippets.py yesterday
    dev/snippets.py 'last week'
    dev/snippets.py 'last 7 days'
    dev/snippets.py February
    dev/snippets.py 'April 1-15'
"""

from __future__ import annotations

import calendar
import json
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import IO, TextIO, cast
from urllib.request import Request, urlopen

import click
from rich.console import Console
from rich.table import Table

from ghgql.repo import repo_root
from orbit.github.client import github_client
from orbit.github.models import MilestoneSummary, PeriodIssue, PeriodPR

PROG_NAME = "dev/snippets.py"

_MONTHS = {
    name.lower(): number for number, name in enumerate(calendar.month_name) if number
} | {name.lower(): number for number, name in enumerate(calendar.month_abbr) if number}

_MONTH_RANGE = re.compile(r"^([A-Za-z]+) +(\d+)(?:-(\d+))?$")

_DAY_WINDOW = re.compile(r"^(?:(?:last|past) +)?(\d+) *(?:d|days?)$")

_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%d %B %Y", "%B %d %Y")


class PeriodError(Exception):
    pass


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass(frozen=True)
class EpicRow:
    number: int
    title: str
    created: date
    closed: date | None
    subs_created: int
    subs_closed: int


@dataclass(frozen=True)
class StandaloneIssue:
    number: int
    date: date
    state: str
    title: str


@dataclass(frozen=True)
class Rollup:
    rows: tuple[EpicRow, ...]
    standalone: tuple[StandaloneIssue, ...]


@dataclass(frozen=True)
class Commit:
    sha: str
    subject: str


@dataclass(frozen=True)
class Report:
    period: Period
    milestones: tuple[MilestoneSummary, ...]
    rollup: Rollup
    prs: tuple[PeriodPR, ...]
    commits: tuple[Commit, ...]
    deploys: int | None
    docs: tuple[str, ...]


@dataclass(frozen=True)
class Annotation:
    at: datetime


type RunGit = Callable[[list[str]], str]
type FetchAnnotations = Callable[[str, str], list[Annotation]]

_VERSION_SUBJECT = re.compile(r"^\d+\.\d+\.\d+$")
_SQUASH_SUFFIX = re.compile(r"\(#(\d+)\)$")
_MERGE_SUBJECT = "Merge pull request "

_GRAFANA_ANNOTATIONS = "https://obedienttrixie.grafana.net/api/annotations"
DEPLOY_TOKEN_FILE = Path.home() / ".grafana_deploy_token"


def _git_bounds(period: Period) -> list[str]:
    return [
        f"--after={period.start}T00:00:00",
        f"--before={period.end + timedelta(days=1)}T00:00:00",
    ]


def git_in(root: Path) -> RunGit:
    def run(args: list[str]) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    return run


def fetch_commits(period: Period, git: RunGit) -> tuple[Commit, ...]:
    output = git(
        ["log", "--first-parent", "main", "--format=%H%x09%s", *_git_bounds(period)]
    )
    commits: list[Commit] = []
    for line in output.splitlines():
        sha, _, subject = line.partition("\t")
        if sha and subject:
            commits.append(Commit(sha=sha, subject=subject))
    return tuple(commits)


def direct_commits(
    commits: Sequence[Commit], prs: Sequence[PeriodPR]
) -> tuple[Commit, ...]:
    numbers = {pr.number for pr in prs}
    merge_shas = {pr.merge_commit_oid for pr in prs if pr.merge_commit_oid is not None}
    kept: list[Commit] = []
    for commit in commits:
        if commit.subject.startswith(_MERGE_SUBJECT):
            continue
        if _VERSION_SUBJECT.match(commit.subject):
            continue
        if commit.sha in merge_shas:
            continue
        squashed = _SQUASH_SUFFIX.search(commit.subject)
        if squashed is not None and int(squashed.group(1)) in numbers:
            continue
        kept.append(commit)
    return tuple(kept)


def doc_paths(log_output: str) -> tuple[str, ...]:
    return tuple(
        sorted({line for line in log_output.splitlines() if line.startswith("docs/")})
    )


def fetch_doc_paths(period: Period, git: RunGit) -> tuple[str, ...]:
    return doc_paths(
        git(["log", "main", "--name-only", "--format=", *_git_bounds(period)])
    )


def milestones_in(
    issues: Sequence[PeriodIssue], known: Sequence[MilestoneSummary]
) -> tuple[MilestoneSummary, ...]:
    by_title = {milestone.title: milestone for milestone in known}
    titles = {issue.milestone for issue in issues if issue.milestone is not None}
    return tuple(
        by_title.get(title, MilestoneSummary(title=title, state="unknown"))
        for title in sorted(titles)
    )


def read_deploy_token(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text().strip() or None


def fetch_annotations(token: str, url: str) -> list[Annotation]:
    request = Request(
        url,
        headers={"Authorization": f"Bearer {token}"},
    )
    opened = cast("AbstractContextManager[IO[bytes]]", urlopen(request, timeout=30))
    with opened as response:
        return annotations_from(cast("list[dict[str, object]]", json.load(response)))


def annotations_from(raw: Sequence[Mapping[str, object]]) -> list[Annotation]:
    return [
        Annotation(at=datetime.fromtimestamp(float(millis) / 1000, tz=UTC))
        for entry in raw
        if isinstance(millis := entry.get("time"), int | float)
    ]


def count_deploys(
    period: Period, token: str | None, fetch: FetchAnnotations
) -> int | None:
    if token is None:
        return None
    start_ms = int(
        datetime.combine(period.start, time.min, tzinfo=UTC).timestamp() * 1000
    )
    end_ms = int(
        datetime.combine(
            period.end + timedelta(days=1), time.min, tzinfo=UTC
        ).timestamp()
        * 1000
    )
    url = f"{_GRAFANA_ANNOTATIONS}?tags=deploy&from={start_ms}&to={end_ms}&limit=500"
    try:
        annotations = fetch(token, url)
    except Exception:  # noqa: BLE001
        return None
    return sum(
        1 for note in annotations if period.contains(note.at.astimezone(UTC).date())
    )


def _month_end(first: date) -> date:
    return first.replace(day=calendar.monthrange(first.year, first.month)[1])


def _month_period(month: int, today: date) -> Period:
    first = date(today.year, month, 1)
    if first > today:
        first = first.replace(year=first.year - 1)
    return Period(first, _month_end(first))


def _lenient_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    raise PeriodError(f"cannot parse period '{text}'")


def parse_period(text: str, today: date) -> Period:
    stripped = text.strip()
    normalized = " ".join(stripped.lower().split())
    if normalized == "today":
        return Period(today, today)
    if normalized == "yesterday":
        yesterday = today - timedelta(days=1)
        return Period(yesterday, yesterday)
    if normalized == "this week":
        return Period(today - timedelta(days=today.weekday()), today)
    if normalized in ("last week", "past week"):
        start = today - timedelta(days=today.weekday() + 7)
        return Period(start, start + timedelta(days=6))
    window = _DAY_WINDOW.match(normalized)
    if window is not None:
        days = int(window.group(1))
        if days < 1:
            raise PeriodError(f"cannot parse period '{stripped}'")
        return Period(today - timedelta(days=days - 1), today)
    if normalized == "this month":
        return Period(today.replace(day=1), today)
    if normalized in ("last month", "past month"):
        end = today.replace(day=1) - timedelta(days=1)
        return Period(end.replace(day=1), end)
    if normalized in _MONTHS:
        return _month_period(_MONTHS[normalized], today)
    match = _MONTH_RANGE.match(normalized)
    if match is not None:
        month = _MONTHS.get(match.group(1))
        if month is None:
            raise PeriodError(f"cannot parse period '{stripped}'")
        try:
            start = date(today.year, month, int(match.group(2)))
            last = match.group(3)
            end = date(today.year, month, int(last)) if last else start
        except ValueError as exc:
            raise PeriodError(f"cannot parse period '{stripped}'") from exc
        return Period(start, end)
    day = _lenient_date(stripped)
    return Period(day, day)


def build_rollup(issues: Sequence[PeriodIssue], period: Period) -> Rollup:
    epics: dict[int, PeriodIssue] = {}
    parents: dict[int, tuple[str, date]] = {}
    created_counts: dict[int, int] = {}
    closed_counts: dict[int, int] = {}
    standalone: list[StandaloneIssue] = []
    for issue in issues:
        if issue.is_epic:
            epics[issue.number] = issue
        parent = issue.parent
        if parent is None:
            if not issue.is_epic:
                standalone.append(_standalone(issue, period))
            continue
        parents.setdefault(parent.number, (parent.title, parent.created_at))
        if period.contains(issue.created_at):
            created_counts[parent.number] = created_counts.get(parent.number, 0) + 1
        if issue.closed_at is not None and period.contains(issue.closed_at):
            closed_counts[parent.number] = closed_counts.get(parent.number, 0) + 1
    rows: list[EpicRow] = []
    for number in sorted(set(epics) | set(parents)):
        epic = epics.get(number)
        if epic is not None:
            title, created, closed = epic.title, epic.created_at, epic.closed_at
        else:
            title, created = parents[number]
            closed = None
        subs_created = created_counts.get(number, 0)
        subs_closed = closed_counts.get(number, 0)
        own_activity = period.contains(created) or (
            closed is not None and period.contains(closed)
        )
        if not (subs_created or subs_closed or own_activity):
            continue
        rows.append(
            EpicRow(
                number=number,
                title=title,
                created=created,
                closed=closed,
                subs_created=subs_created,
                subs_closed=subs_closed,
            )
        )
    return Rollup(tuple(rows), tuple(standalone))


def _standalone(issue: PeriodIssue, period: Period) -> StandaloneIssue:
    when = (
        issue.closed_at
        if issue.closed_at is not None and period.contains(issue.closed_at)
        else issue.created_at
    )
    return StandaloneIssue(
        number=issue.number,
        date=when,
        state=issue.state,
        title=issue.title,
    )


def print_report(report: Report, out: TextIO) -> None:
    console = Console(file=out, highlight=False)
    console.print(f"Period: {report.period.start} to {report.period.end}")
    _section(console, "MILESTONES", [_milestone_line(m) for m in report.milestones])
    console.print()
    console.print("EPIC ACTIVITY:")
    if report.rollup.rows:
        console.print(_epic_table(report.rollup.rows))
    else:
        console.print("  (none)")
    _section(
        console,
        "STANDALONE WORK",
        [
            f"#{s.number} {s.date} [{s.state}] {s.title}"
            for s in report.rollup.standalone
        ],
    )
    _section(
        console,
        "MERGED PRs",
        [f"#{pr.number} {pr.merged_at} {pr.title}" for pr in report.prs],
    )
    _section(
        console,
        "DIRECT COMMITS",
        [f"{c.sha[:7]} {c.subject}" for c in report.commits],
    )
    _section(
        console,
        "DEPLOYS",
        [] if report.deploys is None else [str(report.deploys)],
        empty="(skipped: no Grafana token)",
    )
    _section(console, "CHANGED DOCS", list(report.docs))


def _section(
    console: Console, label: str, lines: Sequence[str], empty: str = "(none)"
) -> None:
    console.print()
    console.print(f"{label}:")
    for line in lines or [empty]:
        console.print(f"  {line}")


def _milestone_line(milestone: MilestoneSummary) -> str:
    due = f" due {milestone.due_on}" if milestone.due_on is not None else ""
    return f"{milestone.title} [{milestone.state}]{due}"


def _epic_table(rows: Sequence[EpicRow]) -> Table:
    table = Table(box=None, pad_edge=False)
    table.add_column("EPIC", style="cyan", no_wrap=True)
    table.add_column("CREATED", no_wrap=True)
    table.add_column("CLOSED", no_wrap=True)
    table.add_column("SUBS +", justify="right", no_wrap=True)
    table.add_column("SUBS -", justify="right", no_wrap=True)
    table.add_column("TITLE")
    for row in rows:
        table.add_row(
            f"#{row.number}",
            str(row.created),
            str(row.closed) if row.closed is not None else "",
            str(row.subs_created),
            str(row.subs_closed),
            row.title,
        )
    return table


def _today() -> date:
    return datetime.now(UTC).astimezone().date()


@click.command()
@click.argument("period_words", metavar="[PERIOD]", nargs=-1)
def cli(period_words: tuple[str, ...]) -> None:
    """Roll up issue activity for PERIOD (default: today)."""
    text = " ".join(period_words) or "today"
    try:
        period = parse_period(text, _today())
    except PeriodError as exc:
        raise click.ClickException(str(exc)) from exc
    print_report(build_report(period), sys.stdout)


def build_report(period: Period) -> Report:
    git = git_in(repo_root())
    client = github_client()
    issues = client.search_period_issues(period.start, period.end)
    prs = client.search_period_prs(period.start, period.end)
    return Report(
        period=period,
        milestones=milestones_in(issues, client.list_milestones(include_closed=True)),
        rollup=build_rollup(issues, period),
        prs=tuple(prs),
        commits=direct_commits(fetch_commits(period, git), prs),
        deploys=count_deploys(
            period, read_deploy_token(DEPLOY_TOKEN_FILE), fetch_annotations
        ),
        docs=fetch_doc_paths(period, git),
    )


def main(args: Sequence[str] | None = None) -> None:
    cli(args=args, prog_name=PROG_NAME)


if __name__ == "__main__":
    main()
