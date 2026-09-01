"""Review a diff with one fresh headless claude session per lens."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Generator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Final, NoReturn, TypedDict, cast

import click

from orbit.core import open_url
from review.html import render as render_markdown

PROG_NAME: Final = "dev/review-diff"
LENSES: Final = ("correctness", "tests", "conventions")
DEFAULT_DIFF_SPEC: Final = "origin/main...HEAD"
ALLOWED_TOOLS: Final = (
    "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)"
)
DISALLOWED_TOOLS: Final = (
    "Bash(*pytest*),Bash(*dev/lint*),Bash(*ruff*),Bash(*black*),"
    "Bash(*basedpyright*),Edit,Write,NotebookEdit"
)
NO_ISSUE_CONTEXT: Final = (
    "You have not been told what the change was meant to do. Judge it on its own terms."
)

# A configured diff.external would launch a GUI difftool here and in
# every reviewer session; env config overrides it for both.
_GIT_CONFIG_ENV: Final = {
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "diff.external",
    "GIT_CONFIG_VALUE_0": "",
}

_PR_URL: Final = re.compile(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)")
_PR_SHORT: Final = re.compile(r"([^/#]+/[^/#]+)#(\d+)\Z")
_PR_NUMBER: Final = re.compile(r"\d+\Z")


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(frozen=True)
class Session:
    prompt: str
    cwd: Path
    model: str | None


RunCommand = Callable[[Sequence[str], Path], CommandResult]
RunSession = Callable[[Session], CommandResult]


@dataclass(frozen=True)
class Deps:
    run_command: RunCommand
    run_session: RunSession


@dataclass(frozen=True)
class PullRequest:
    number: str
    repo: str


@dataclass(frozen=True)
class PrReview:
    number: str
    repo: str
    title: str
    head_sha: str
    base_sha: str

    @property
    def diff_spec(self) -> str:
        return f"{self.base_sha}..{self.head_sha}"


class _PrMetadata(TypedDict):
    headRefOid: str
    baseRefOid: str
    baseRefName: str
    title: str


def _env() -> dict[str, str]:
    return {**os.environ, **_GIT_CONFIG_ENV}


def _run_command(argv: Sequence[str], cwd: Path) -> CommandResult:
    proc = subprocess.run(
        list(argv), capture_output=True, text=True, cwd=cwd, env=_env(), check=False
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def claude_argv(model: str | None) -> list[str]:
    argv = [
        "claude",
        "-p",
        "--output-format",
        "text",
        "--allowed-tools",
        ALLOWED_TOOLS,
        "--disallowed-tools",
        DISALLOWED_TOOLS,
    ]
    if model:
        argv += ["--model", model]
    return argv


def _run_session(session: Session) -> CommandResult:
    proc = subprocess.run(
        claude_argv(session.model),
        input=session.prompt,
        capture_output=True,
        text=True,
        cwd=session.cwd,
        env=_env(),
        check=False,
    )
    return CommandResult(proc.returncode, proc.stdout, proc.stderr)


def default_deps() -> Deps:
    if shutil.which("claude") is None:
        _die("claude not on PATH")
    return Deps(run_command=_run_command, run_session=_run_session)


def _die(message: str) -> NoReturn:
    click.echo(f"ERROR: {message}", err=True)
    raise SystemExit(1)


@dataclass(frozen=True)
class _Progress:
    quiet: bool

    def __call__(self, message: str) -> None:
        if not self.quiet:
            click.echo(f">>> {message}", err=True)


def template(*parts: str) -> str:
    return (
        resources.files("review")
        .joinpath("templates", *parts)
        .read_text(encoding="utf-8")
    )


def all_templates() -> dict[str, str]:
    root = resources.files("review").joinpath("templates")
    found: dict[str, str] = {}

    def walk(node: Traversable, prefix: str) -> None:
        for child in node.iterdir():
            name = f"{prefix}{child.name}"
            if child.is_dir():
                walk(child, f"{name}/")
            elif child.name.endswith(".md"):
                found[name] = child.read_text(encoding="utf-8")

    walk(root, "")
    return found


def lens_detail(lens: str) -> str:
    return template("lenses", f"{lens}.md").rstrip("\n")


def render_prompt(
    text: str,
    *,
    diff_spec: str,
    issue_context: str,
    lens: str = "",
    lens_reports: str = "",
) -> str:
    text = text.replace("{{DIFF_SPEC}}", diff_spec)
    if lens:
        text = text.replace("{{LENS}}", lens)
        text = text.replace("{{LENS_DETAIL}}", lens_detail(lens))
    else:
        text = text.replace("{{LENS_REPORTS}}", lens_reports)
    # Substituted last: an issue body or lens report may quote a
    # placeholder, and an earlier pass would expand it.
    return text.replace("{{ISSUE_CONTEXT}}", issue_context)


def format_lens_reports(reports: Mapping[str, str]) -> str:
    blocks: list[str] = []
    for lens in LENSES:
        body = reports[lens]
        if not body.endswith("\n"):
            body += "\n"
        blocks.append(f"--- BEGIN {lens} ---\n{body}--- END {lens} ---")
    return "\n\n".join(blocks)


def format_issue_context(issue: str, body: str) -> str:
    return f"""\
The change is meant to satisfy GitHub issue #{issue}, whose body follows
between the markers. Treat it as a statement of intent, not as evidence
that the code is correct — a change that faithfully implements a wrong
plan is still a finding.

--- BEGIN ISSUE #{issue} ---
{body}
--- END ISSUE #{issue} ---"""


def parse_pr(target: str) -> PullRequest | None:
    url = _PR_URL.match(target)
    if url:
        return PullRequest(number=url.group(2), repo=url.group(1))
    short = _PR_SHORT.match(target)
    if short:
        return PullRequest(number=short.group(2), repo=short.group(1))
    if _PR_NUMBER.match(target):
        return PullRequest(number=target, repo="")
    return None


def _failure_block(heading: str, lead: str, stderr: str) -> str:
    tail = "\n".join(stderr.splitlines()[-20:])
    return f"{heading}\n\n{lead}\n```\n{tail}\n```\n"


def build_report(
    *,
    title: str,
    commit: str,
    issue: str,
    consolidated: str | None,
    reports: Mapping[str, str],
) -> str:
    header = [f"# {title}", "", f"Commit: `{commit}`"]
    if issue:
        header.append(f"Issue: #{issue}")
    sections = ["\n".join(header)]
    if consolidated is not None:
        sections.append(consolidated)
        sections.append("## Per-lens reports")
    sections.extend(reports[lens] for lens in LENSES)
    return "\n\n".join(section.strip("\n") for section in sections) + "\n"


def _relink(link: Path, target: Path) -> None:
    # Linking a path onto itself would replace the page just written
    # with a symlink to nothing. The cache path and the rendered path
    # can spell the same file differently, so compare them resolved.
    if link.resolve(strict=False) == target.resolve(strict=False):
        return
    link.unlink(missing_ok=True)
    link.symlink_to(target)


def render_html(
    markdown_path: Path, *, cache: Path, want_open: bool, info: _Progress
) -> None:
    source = markdown_path.parent.resolve() / markdown_path.name
    page = source.with_suffix(".html")
    try:
        page.write_text(
            render_markdown(source.read_text(encoding="utf-8")), encoding="utf-8"
        )
    except OSError:
        _die(f"could not render {page}")
    if want_open:
        open_url(page.resolve().as_uri())
    _relink(cache / "last-report.html", page)
    info(f"html: {page}")


@contextmanager
def pr_worktree(head_sha: str, deps: Deps) -> Generator[Path]:
    work_dir = Path(tempfile.mkdtemp())
    cwd = Path.cwd()
    try:
        added = deps.run_command(
            ["git", "worktree", "add", "-q", "--detach", str(work_dir), head_sha], cwd
        )
        if not added.ok:
            _die(f"could not create worktree at {head_sha}")
        yield work_dir
    finally:
        _ = deps.run_command(
            ["git", "worktree", "remove", "--force", str(work_dir)], cwd
        )
        shutil.rmtree(work_dir, ignore_errors=True)


def _resolve_pr(pr: PullRequest, deps: Deps, info: _Progress) -> PrReview:
    cwd = Path.cwd()
    argv = [
        "gh",
        "pr",
        "view",
        pr.number,
        "--json",
        "headRefOid,baseRefOid,baseRefName,title",
    ]
    if pr.repo:
        argv += ["--repo", pr.repo]
    viewed = deps.run_command(argv, cwd)
    if not viewed.ok:
        _die(f"could not read PR #{pr.number}")
    meta = cast(_PrMetadata, json.loads(viewed.stdout))

    repo = pr.repo
    if not repo:
        named = deps.run_command(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            cwd,
        )
        if not named.ok:
            _die(f"could not determine the repository for PR #{pr.number}")
        repo = named.stdout.strip()

    info(f"fetching PR #{pr.number} ({repo}) — {meta['title']}")
    remote = f"https://github.com/{repo}"
    base_ref = meta["baseRefName"]
    fetches = (
        (
            f"refs/pull/{pr.number}/head",
            f"could not fetch PR #{pr.number} from {remote}",
        ),
        (
            f"refs/heads/{base_ref}",
            f"could not fetch base branch {base_ref} from {remote}",
        ),
    )
    for ref, message in fetches:
        if not deps.run_command(["git", "fetch", "-q", remote, ref], cwd).ok:
            _die(message)

    # baseRefOid is pinned per-PR, so a merged PR still diffs against
    # where it branched rather than against a base tip that now
    # contains it.
    base_oid = meta["baseRefOid"]
    head_sha = meta["headRefOid"]
    merge_base = deps.run_command(["git", "merge-base", head_sha, base_oid], cwd)
    base_sha = merge_base.stdout.strip() if merge_base.ok else base_oid

    return PrReview(
        number=pr.number,
        repo=repo,
        title=meta["title"],
        head_sha=head_sha,
        base_sha=base_sha,
    )


def _read_issue_context(issue: str, deps: Deps) -> str:
    if not issue:
        return NO_ISSUE_CONTEXT
    viewed = deps.run_command(
        ["gh", "issue", "view", issue, "--json", "body", "--jq", ".body"], Path.cwd()
    )
    if not viewed.ok:
        _die(f"could not read issue #{issue}")
    return format_issue_context(issue, viewed.stdout.rstrip("\n"))


@dataclass(frozen=True)
class _Reviewer:
    deps: Deps
    run_dir: Path
    work_dir: Path
    model: str | None
    diff_spec: str
    issue_context: str

    def _session(self, kind: str, prompt: str, heading: str, lead: str) -> str:
        (self.run_dir / f"{kind}.prompt").write_text(prompt, encoding="utf-8")
        result = self.deps.run_session(Session(prompt, self.work_dir, self.model))
        text = (
            result.stdout if result.ok else _failure_block(heading, lead, result.stderr)
        )
        (self.run_dir / f"{kind}.md").write_text(text, encoding="utf-8")
        (self.run_dir / f"{kind}.err").write_text(result.stderr, encoding="utf-8")
        return text

    def lens(self, name: str) -> str:
        prompt = render_prompt(
            template("review.md"),
            diff_spec=self.diff_spec,
            issue_context=self.issue_context,
            lens=name,
        )
        return self._session(name, prompt, f"## {name}", "Reviewer failed. stderr:")

    def consolidate(self, reports: Mapping[str, str]) -> str:
        prompt = render_prompt(
            template("review-consolidate.md"),
            diff_spec=self.diff_spec,
            issue_context=self.issue_context,
            lens_reports=format_lens_reports(reports),
        )
        return self._session(
            "consolidated",
            prompt,
            "## Consolidation failed",
            "The per-lens reports below are unmerged. stderr:",
        )


_EPILOG = """\
A pull request is reviewed in a detached worktree at its head commit, so the
files around the diff are the PR's, not your working tree's. Your checkout is
left untouched.

REVIEW_MODEL overrides the model used for every session.
"""


@click.command(context_settings={"help_option_names": ["-h", "--help"]}, epilog=_EPILOG)
@click.argument("target", required=False)
@click.option(
    "-q",
    "quiet",
    is_flag=True,
    help="quiet: progress off, report still printed to stdout",
)
@click.option(
    "-s", "serial", is_flag=True, help="run lenses one at a time instead of in parallel"
)
@click.option(
    "-i",
    "issue",
    metavar="ISSUE",
    default="",
    help="give reviewers the body of GitHub issue ISSUE as intent",
)
@click.option(
    "--no-consolidate",
    "no_consolidate",
    is_flag=True,
    help="skip the merge session; print the per-lens reports as-is",
)
@click.option(
    "--markdown",
    "reprint",
    is_flag=True,
    help="reprint the last report instead of reviewing again",
)
@click.option(
    "--html", "want_html", is_flag=True, help="also render report.html beside report.md"
)
@click.option(
    "--open",
    "want_open",
    is_flag=True,
    help="imply --html and open the page in a browser",
)
@click.pass_context
def cli(
    ctx: click.Context,
    target: str | None,
    quiet: bool,
    serial: bool,
    issue: str,
    no_consolidate: bool,
    reprint: bool,
    want_html: bool,
    want_open: bool,
) -> None:
    """Spawn one fresh headless claude session per review lens.

    The lenses (correctness, tests, conventions) share no context, and a final
    session merges their reports into one deduplicated list of findings. The
    raw per-lens reports follow it in the report.

    TARGET is a git diff spec (default: origin/main...HEAD), or a pull request
    as 123, owner/repo#123, or https://github.com/owner/repo/pull/123.
    """
    want_html = want_html or want_open
    cache = Path(
        os.environ.get("REVIEW_CACHE_DIR") or Path.home() / ".cache" / "claude-review"
    )
    info = _Progress(quiet)

    if reprint:
        last = cache / "last-report.md"
        if not last.is_file():
            _die(f"no previous report at {last}")
        click.echo(last.read_text(encoding="utf-8"), nl=False)
        if want_html:
            # One level is enough: the symlink is written with an
            # absolute target.
            source = last.readlink() if last.is_symlink() else last
            render_html(source, cache=cache, want_open=want_open, info=info)
        return

    injected = cast(object, ctx.obj)
    deps = injected if isinstance(injected, Deps) else default_deps()
    diff_spec = target or DEFAULT_DIFF_SPEC
    pr = parse_pr(diff_spec) if target else None

    with ExitStack() as stack:
        work_dir = Path.cwd()
        pr_review: PrReview | None = None
        if pr is not None:
            pr_review = _resolve_pr(pr, deps, info)
            diff_spec = pr_review.diff_spec
            work_dir = stack.enter_context(pr_worktree(pr_review.head_sha, deps))

        if deps.run_command(["git", "diff", "--quiet", diff_spec], work_dir).ok:
            _die(f"empty diff for {diff_spec}")

        issue_context = _read_issue_context(issue, deps)
        run_dir = cache / datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
        run_dir.mkdir(parents=True, exist_ok=True)

        reviewer = _Reviewer(
            deps=deps,
            run_dir=run_dir,
            work_dir=work_dir,
            model=os.environ.get("REVIEW_MODEL"),
            diff_spec=diff_spec,
            issue_context=issue_context,
        )

        info(f"reviewing {diff_spec} with {len(LENSES)} fresh sessions -> {run_dir}")
        reports: dict[str, str] = {}
        if serial:
            for name in LENSES:
                info(f"lens: {name}")
                reports[name] = reviewer.lens(name)
        else:
            with ThreadPoolExecutor(max_workers=len(LENSES)) as pool:
                for name, text in zip(
                    LENSES, pool.map(reviewer.lens, LENSES), strict=True
                ):
                    reports[name] = text

        consolidated: str | None = None
        if not no_consolidate:
            info(f"consolidating {len(LENSES)} reports into one list")
            consolidated = reviewer.consolidate(reports)

        title = (
            f"Review of {pr_review.repo}#{pr_review.number} — {pr_review.title}"
            if pr_review is not None
            else f"Review of `{diff_spec}`"
        )
        commit = deps.run_command(
            ["git", "rev-parse", "--short", "HEAD"], work_dir
        ).stdout.strip()
        report = build_report(
            title=title,
            commit=commit,
            issue=issue,
            consolidated=consolidated,
            reports=reports,
        )

    report_path = run_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")
    _relink(cache / "last-report.md", report_path)
    info(f"report: {report_path}")
    click.echo(report, nl=False)
    if want_html:
        render_html(report_path, cache=cache, want_open=want_open, info=info)


def main(args: Sequence[str] | None = None) -> None:
    cli(args=args, prog_name=PROG_NAME)
