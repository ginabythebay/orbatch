from __future__ import annotations

import functools
import json
import sys
from collections.abc import Callable, Sequence
from typing import IO, TextIO, cast

import click
from click import UsageError
from click.decorators import FC

from orbit.completion import source_with_alias
from orbit.config import ConfigError, Milestones, ProjectConfig, load_config
from orbit.core import open_url
from orbit.filtering import partition_filtered, partition_standalone
from orbit.github.client import GitHubClient, github_client
from orbit.github.models import (
    AlreadyDoneError,
    CloseReason,
    Epic,
    Issue,
    SubIssueData,
    Surface,
)
from orbit.github.orchestrators import (
    close_issue,
    create_epic,
    create_leaf,
    edit_issue_body,
    move_issue,
    reorder_issue,
    schedule_issue,
)
from orbit.sort import sort_epics, sort_issues
from orbit.text_output import (
    print_epic_table,
    print_issue_detail,
    print_issue_table,
    print_parent_issue,
    print_standalone_section,
    print_sub_issue_tree,
)
from orbit.tree import FilteredRun, build_tree
from orbit.tui.app import OrbitApp, run_tui

PROG_NAME = "dev/orbit"
SCRIPT_NAME = "orbit"
COMPLETE_VAR = "_ORBIT_COMPLETE"
_CONFIG_KEY = "orbit.config"


def main(args: Sequence[str] | None = None) -> None:
    source = source_with_alias(cli, PROG_NAME, COMPLETE_VAR, SCRIPT_NAME)
    if source is not None:
        click.echo(source, nl=False)
        raise SystemExit(0)
    cli(args=args, prog_name=PROG_NAME, complete_var=COMPLETE_VAR)


def _issue_arg(f: FC) -> FC:
    return click.argument("issue_number", type=int)(f)


def _epic_arg(f: FC) -> FC:
    return click.argument("epic_number", type=int)(f)


def resolve_body(body: str | None, body_file: IO[str] | None) -> str | None:
    """Resolve an issue body from the --body / --body-file options.

    The two options are mutually exclusive. Returns None when neither is
    given so bodyless creation is preserved.
    """
    if body is not None and body_file is not None:
        raise UsageError("--body and --body-file are mutually exclusive")
    if body is not None:
        return body
    if body_file is not None:
        return body_file.read()
    return None


def _json_opt(f: FC) -> FC:
    return click.option(
        "--json", "use_json", is_flag=True, default=False, help="Output as JSON."
    )(f)


def _label_opt(f: FC) -> FC:
    """Attach a repeatable --label option, collected into a tuple."""
    return click.option(
        "--label", "labels", multiple=True, help="Label to add (repeatable)."
    )(f)


def _body_opts(f: FC) -> FC:
    """Attach mutually-exclusive --body / --body-file options.

    --body-file uses click.File so '-' reads stdin and missing files error
    out before the command body runs.
    """
    f = click.option(
        "--body-file",
        "body_file",
        type=click.File("r"),
        default=None,
        help="Read the issue body from a file ('-' for stdin).",
    )(f)
    return click.option("--body", "body", default=None, help="Issue body text.")(f)


def _echo_already_done(exc: AlreadyDoneError, use_json: bool) -> None:
    """Report an idempotent no-op as success.

    The human-readable message goes to stderr so that, in --json mode,
    stdout stays a pure JSON object carrying ``already_done: true`` for
    programmatic callers.
    """
    click.echo(str(exc), err=True)
    if use_json:
        click.echo(json.dumps({"already_done": True}, indent=2))


def _echo_reopened(reopened: tuple[int, ...]) -> None:
    if reopened:
        click.echo("Reopened " + ", ".join(f"#{number}" for number in reopened))


def _sort_opt(help_text: str = "Sort by: number, state, title.") -> Callable[[FC], FC]:
    def decorator(f: FC) -> FC:
        return click.option("--sort", "sort_key", default=None, help=help_text)(f)

    return decorator


def _reverse_opt(f: FC) -> FC:
    """Attach a --reverse option that accepts an optional sort key."""
    return click.option(
        "--reverse",
        "reverse_key",
        is_flag=False,
        flag_value="",
        default=None,
        help="Reverse sort order, optionally with a key.",
    )(f)


def _status_opt(f: FC) -> FC:
    """Filter by issue status."""
    return click.option(
        "--status",
        "filter_for_status",
        is_flag=False,
        default="ALL",
        help="Show issues with this status only. Default: ALL.",
    )(f)


type StatusItem = SubIssueData | Issue | Epic


def make_status_filter(status: str) -> Callable[[StatusItem], bool]:
    """Return true if the item matches the given status filter."""
    if status not in ("ALL", "OPEN", "CLOSED"):
        raise UsageError(
            f"Invalid status filter: {status!r}. Valid values: ALL, OPEN, CLOSED"
        )

    def filter_fn(item: StatusItem) -> bool:
        if status == "ALL":
            return True
        return item.state == status

    return filter_fn


def is_status_filter_active(status: str) -> bool:
    """Return true if the status filter is active."""
    return status != "ALL"


def _resolve_client(ctx: click.Context) -> GitHubClient:
    client = cast("GitHubClient | None", ctx.obj)
    if client is None:
        try:
            client = github_client()
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc
        ctx.obj = client
    return client


def _resolve_config(ctx: click.Context) -> ProjectConfig:
    """The project's `.orbit.toml`, loaded once per invocation.

    Every command validates it, even the ones that read no milestone,
    so a broken config fails the same way everywhere instead of only
    where a milestone happens to be needed.
    """
    config = cast("ProjectConfig | None", ctx.meta.get(_CONFIG_KEY))
    if config is None:
        try:
            config = load_config(OrbitApp.reserved_keys())
        except ConfigError as exc:
            raise click.ClickException(str(exc)) from exc
        ctx.meta[_CONFIG_KEY] = config
    return config


def _pass_client(f: Callable[..., None]) -> Callable[..., None]:
    """Hand the command its client, built on first use.

    Resolved when the command body runs, not in the group callback, so
    `orbit <command> --help` needs neither a checkout nor a token.
    """

    @click.pass_context
    @functools.wraps(f)
    def wrapper(ctx: click.Context, *args: object, **kwargs: object) -> None:
        _ = _resolve_config(ctx)
        f(_resolve_client(ctx), *args, **kwargs)

    return wrapper


def _pass_milestones(f: Callable[..., None]) -> Callable[..., None]:
    """Stack above `_pass_client` to hand the command the milestones too."""

    @click.pass_context
    @functools.wraps(f)
    def wrapper(ctx: click.Context, *args: object, **kwargs: object) -> None:
        f(_resolve_config(ctx).milestones, *args, **kwargs)

    return wrapper


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """Dev tool for issue and epic management.

    Without a subcommand, launches the interactive TUI.
    """
    if ctx.invoked_subcommand is None:
        run_tui(_resolve_client(ctx), _resolve_config(ctx))


def _resolve_sort(
    sort_key: str | None,
    reverse_key: str | None,
) -> tuple[str | None, bool]:
    reverse = reverse_key is not None
    if reverse_key:
        sort_key = reverse_key
    return sort_key, reverse


def _emit_rows[T: Issue | Epic](
    rows: list[T | FilteredRun],
    filter_for_status: str,
    use_json: bool,
    print_table: Callable[[Sequence[T | FilteredRun], TextIO], None],
) -> None:
    if use_json:
        kept = [row for row in rows if not isinstance(row, FilteredRun)]
        click.echo(json.dumps([item.model_dump() for item in kept], indent=2))
    else:
        print_table(rows, sys.stdout)
        print_status_filter(filter_for_status)


def _sorted_issues[T: Issue](
    fetch: Callable[[], list[T]],
    sort_key: str | None,
    reverse_key: str | None,
) -> list[T]:
    sort_key, reverse = _resolve_sort(sort_key, reverse_key)
    try:
        return sort_issues(fetch(), sort_key, reverse)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


def _emit_issues[T: Issue](
    fetch: Callable[[], list[T]],
    filter_for_status: str,
    sort_key: str | None,
    reverse_key: str | None,
    use_json: bool,
) -> None:
    issues = _sorted_issues(fetch, sort_key, reverse_key)
    rows = partition_filtered(issues, make_status_filter(filter_for_status))
    _emit_rows(rows, filter_for_status, use_json, print_issue_table)


@cli.command()
@_status_opt
@_sort_opt()
@_reverse_opt
@_json_opt
@_pass_milestones
@_pass_client
def sprint(
    client: GitHubClient,
    milestones: Milestones,
    filter_for_status: str,
    sort_key: str | None,
    reverse_key: str | None,
    use_json: bool,
) -> None:
    """List issues in the current sprint milestone."""
    issues = _sorted_issues(
        lambda: client.list_issues_by_milestone(milestones.current),
        sort_key,
        reverse_key,
    )
    status_filter = make_status_filter(filter_for_status)
    if use_json:
        _emit_rows(
            partition_filtered(issues, status_filter),
            filter_for_status,
            use_json,
            print_issue_table,
        )
        return
    structured, standalone = partition_standalone(issues)
    structured_rows = partition_filtered(structured, status_filter)
    standalone_rows = partition_filtered(standalone, status_filter)
    has_standalone = any(not isinstance(row, FilteredRun) for row in standalone_rows)
    if structured_rows or not has_standalone:
        print_issue_table(structured_rows, sys.stdout)
    if has_standalone:
        print_standalone_section(standalone_rows, sys.stdout)
    print_status_filter(filter_for_status)


@cli.command()
@_status_opt
@_sort_opt()
@_reverse_opt
@_json_opt
@_pass_milestones
@_pass_client
def backlog(
    client: GitHubClient,
    milestones: Milestones,
    filter_for_status: str,
    use_json: bool,
    sort_key: str | None,
    reverse_key: str | None,
) -> None:
    """List issues in the backlog milestone."""
    _emit_issues(
        lambda: client.list_issues_by_milestone(milestones.backlog),
        filter_for_status,
        sort_key,
        reverse_key,
        use_json,
    )


@cli.command()
@_status_opt
@_sort_opt()
@_reverse_opt
@_json_opt
@_pass_milestones
@_pass_client
def soon(
    client: GitHubClient,
    milestones: Milestones,
    filter_for_status: str,
    use_json: bool,
    sort_key: str | None,
    reverse_key: str | None,
) -> None:
    """List backlog issues labeled 'soon' for next sprint."""
    _emit_issues(
        lambda: client.list_issues_by_milestone(milestones.backlog, label="soon"),
        filter_for_status,
        sort_key,
        reverse_key,
        use_json,
    )


@cli.command()
@_status_opt
@_sort_opt(help_text="Sort by: number, state, title, progress.")
@_reverse_opt
@_json_opt
@_pass_milestones
@_pass_client
def epics(
    client: GitHubClient,
    milestones: Milestones,
    filter_for_status: str,
    use_json: bool,
    sort_key: str | None,
    reverse_key: str | None,
) -> None:
    """List epics in the current sprint with sub-issue progress."""
    status_filter = make_status_filter(filter_for_status)
    sort_key, reverse = _resolve_sort(sort_key, reverse_key)
    try:
        epic_list = sort_epics(
            client.list_epics_by_milestone(milestones.current), sort_key, reverse
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    rows = partition_filtered(epic_list, status_filter)
    _emit_rows(rows, filter_for_status, use_json, print_epic_table)


@cli.command()
@_issue_arg
@_status_opt
@_json_opt
@_pass_client
def subs(
    client: GitHubClient, issue_number: int, filter_for_status: str, use_json: bool
) -> None:
    """Show recursive sub-issue tree of an issue.

    Epic nodes display open/total counts of their direct children.
    """
    status_filter = make_status_filter(filter_for_status)
    raw = client.fetch_sub_issue_tree(issue_number)
    nodes = build_tree(raw, status_filter)
    if use_json:
        click.echo(json.dumps([n.model_dump() for n in nodes], indent=2))
    else:
        print_sub_issue_tree(nodes, sys.stdout)
        print_status_filter(filter_for_status)


def print_status_filter(filter_for_status: str):
    if is_status_filter_active(filter_for_status):
        click.echo(f"Status filter '{filter_for_status}' applied")


@cli.command()
@_issue_arg
@_json_opt
@_pass_client
def show(client: GitHubClient, issue_number: int, use_json: bool) -> None:
    """Show an issue's details: metadata header and body."""
    try:
        detail = client.fetch_issue_detail(issue_number)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if use_json:
        click.echo(json.dumps(detail.model_dump(), indent=2))
    else:
        print_issue_detail(detail, sys.stdout)


@cli.command()
@click.argument("query")
@_status_opt
@_sort_opt()
@_reverse_opt
@_json_opt
@_pass_client
def find(
    client: GitHubClient,
    query: str,
    filter_for_status: str,
    sort_key: str | None,
    reverse_key: str | None,
    use_json: bool,
) -> None:
    """Search issue titles in the repo and list matching issues."""

    def fetch() -> list[Issue]:
        try:
            return client.search_issue_titles(query)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc

    _emit_issues(fetch, filter_for_status, sort_key, reverse_key, use_json)


@cli.command()
@_issue_arg
@_json_opt
@_pass_client
def parent(client: GitHubClient, issue_number: int, use_json: bool) -> None:
    """Show the parent issue."""
    issue = client.fetch_parent_issue(issue_number)
    if use_json:
        click.echo(json.dumps(issue.model_dump() if issue else None, indent=2))
    else:
        print_parent_issue(issue, sys.stdout)


@cli.command("create-epic")
@click.argument("title")
@click.argument("sub_issue_numbers", nargs=-1, type=int)
@_body_opts
@_label_opt
@_json_opt
@_pass_milestones
@_pass_client
def create_epic_cmd(
    client: GitHubClient,
    milestones: Milestones,
    title: str,
    sub_issue_numbers: tuple[int, ...],
    body: str | None,
    body_file: IO[str] | None,
    labels: tuple[str, ...],
    use_json: bool,
) -> None:
    """Create an epic in the current milestone with the epic label."""
    body_text = resolve_body(body, body_file)
    try:
        result = create_epic(
            client,
            milestones.current,
            title,
            list(sub_issue_numbers),
            body_text,
            labels,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if use_json:
        click.echo(json.dumps(result.model_dump(), indent=2))
    else:
        click.echo(
            f"Created epic #{result.number} ({result.title})"
            + f" in milestone {result.milestone!r}"
        )
        for num in result.sub_issues_attached:
            click.echo(f"Attached #{num} as sub-issue")


@cli.command()
@click.argument("epic")
@click.argument("title")
@_body_opts
@_label_opt
@_json_opt
@_pass_milestones
@_pass_client
def create(
    client: GitHubClient,
    milestones: Milestones,
    epic: str,
    title: str,
    body: str | None,
    body_file: IO[str] | None,
    labels: tuple[str, ...],
    use_json: bool,
) -> None:
    """Create a leaf issue under an epic, standalone, or in the backlog.

    EPIC is an epic issue number, 'standalone' for the current milestone
    with no parent, or 'shelf' for the backlog.
    """
    body_text = resolve_body(body, body_file)
    try:
        result = create_leaf(client, milestones, epic, title, body_text, labels)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if use_json:
        click.echo(json.dumps(result.model_dump(), indent=2))
    else:
        _echo_reopened(result.reopened)
        if result.epic_number is not None:
            click.echo(
                f"Created #{result.number} ({result.title})"
                + f" under epic #{result.epic_number} ({result.epic_title})"
            )
            if result.converted_dest_to_epic:
                click.echo(f"Converted #{result.epic_number} to epic")
        else:
            click.echo(
                f"Created #{result.number} ({result.title})"
                + f" in milestone {result.milestone!r}"
            )


@cli.command()
@_issue_arg
@_epic_arg
@_json_opt
@_pass_client
def move(
    client: GitHubClient, issue_number: int, epic_number: int, use_json: bool
) -> None:
    """Move an issue under an epic, setting its milestone to match."""
    try:
        result = move_issue(client, issue_number, epic_number)
    except AlreadyDoneError as exc:
        _echo_already_done(exc, use_json)
        return
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if use_json:
        click.echo(json.dumps(result.model_dump(), indent=2))
    else:
        _echo_reopened(result.reopened)
        if result.already_done:
            click.echo(
                f"Issue #{result.issue_number} is already under"
                + f" epic #{result.epic_number}",
                err=True,
            )
            return
        if result.old_epic_number is not None:
            click.echo(
                f"Detached #{result.issue_number} from epic"
                + f" #{result.old_epic_number} ({result.old_epic_title})"
            )
        click.echo(
            f"Moved #{result.issue_number} ({result.issue_title})"
            + f" → epic #{result.epic_number} ({result.epic_title})"
        )
        if result.milestone is not None:
            click.echo(f"Milestone set to {result.milestone!r}")
        if result.converted_dest_to_epic:
            click.echo(f"Converted #{result.epic_number} to epic")


@cli.command()
@_issue_arg
@click.option("--after", "after_number", type=int, help="Place after this issue.")
@click.option("--before", "before_number", type=int, help="Place before this issue.")
@click.option(
    "--first", is_flag=True, default=False, help="Place first among its siblings."
)
@_json_opt
@_pass_client
def reorder(
    client: GitHubClient,
    issue_number: int,
    after_number: int | None,
    before_number: int | None,
    first: bool,
    use_json: bool,
) -> None:
    """Reposition an issue within its epic's sub-issue order."""
    positions = [after_number is not None, before_number is not None, first]
    if sum(positions) != 1:
        raise UsageError("Pass exactly one of --after, --before, or --first")
    try:
        result = reorder_issue(
            client, issue_number, after_number=after_number, before_number=before_number
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if use_json:
        click.echo(json.dumps(result.model_dump(), indent=2))
    elif result.already_done:
        click.echo(
            f"#{result.issue_number} ({result.issue_title}) is already first"
            + f" in epic #{result.epic_number} ({result.epic_title})"
        )
    else:
        placement = (
            "first"
            if result.position == "first"
            else f"{result.position} #{result.reference_number}"
            + f" ({result.reference_title})"
        )
        click.echo(
            f"Moved #{result.issue_number} ({result.issue_title}) {placement}"
            + f" in epic #{result.epic_number} ({result.epic_title})"
        )


@cli.command()
@_issue_arg
@click.option(
    "-m",
    "--milestone",
    default=None,
    help="Target milestone (defaults to the current milestone).",
)
@_json_opt
@_pass_milestones
@_pass_client
def schedule(
    client: GitHubClient,
    milestones: Milestones,
    issue_number: int,
    milestone: str | None,
    use_json: bool,
) -> None:
    """Move an issue or epic to a milestone (defaults to the current one).

    Detaches the issue from its epic when that epic lives in a different
    milestone. Use '-m Backlog' to shelve an issue.
    """
    try:
        result = schedule_issue(client, issue_number, milestone or milestones.current)
    except AlreadyDoneError as exc:
        _echo_already_done(exc, use_json)
        return
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if use_json:
        click.echo(json.dumps(result.model_dump(), indent=2))
    else:
        if result.old_epic_number is not None:
            click.echo(
                f"Detached #{result.issue_number} from epic"
                + f" #{result.old_epic_number} ({result.old_epic_title})"
            )
        click.echo(
            f"Scheduled #{result.issue_number} ({result.issue_title})"
            + f" → milestone {result.milestone!r}"
        )


@cli.command()
@_issue_arg
@click.option(
    "--reason",
    default=str(CloseReason.COMPLETED),
    help="Reason for closing the issue.",
    type=str,
)
@_json_opt
@_pass_client
def close(client: GitHubClient, issue_number: int, reason: str, use_json: bool) -> None:
    """Close an issue, with an optional reason."""
    try:
        close_reason: CloseReason = CloseReason(reason.lower())
    except ValueError:
        valid = ", ".join(r.value for r in CloseReason)
        raise click.ClickException(
            f"Invalid reason: {reason!r}. Valid values: {valid}"
        ) from None
    try:
        result = close_issue(client, issue_number, close_reason, Surface.CLI)
    except AlreadyDoneError as exc:
        _echo_already_done(exc, use_json)
        return
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    if use_json:
        click.echo(json.dumps(result.model_dump(), indent=2))
    else:
        click.echo(f"Closed #{result.number} ({result.reason})")


@cli.command("set-body")
@_issue_arg
@_body_opts
@_json_opt
@_pass_client
def set_body_cmd(
    client: GitHubClient,
    issue_number: int,
    body: str | None,
    body_file: IO[str] | None,
    use_json: bool,
) -> None:
    """Set an existing issue's body from --body or --body-file."""
    body_text = resolve_body(body, body_file)
    if body_text is None:
        raise UsageError("pass --body or --body-file to set the issue body")
    try:
        result = edit_issue_body(client, issue_number, body_text)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if use_json:
        click.echo(json.dumps(result.model_dump(), indent=2))
    else:
        click.echo(f"Updated body of #{result.number} ({result.title})")


@cli.command("edit")
@_issue_arg
@_json_opt
@_pass_client
def edit_cmd(client: GitHubClient, issue_number: int, use_json: bool) -> None:
    """Open a GitHub issue in the default browser."""
    owner, name = client.repo
    url = f"https://github.com/{owner}/{name}/issues/{issue_number}"
    open_url(url)
    if use_json:
        click.echo(json.dumps({"number": issue_number, "url": url}, indent=2))
    else:
        click.echo(url)


if __name__ == "__main__":
    main()
