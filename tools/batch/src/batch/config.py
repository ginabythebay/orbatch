"""The per-project `batch.toml`.

batch ships no project-specific behavior: the repo names its own seed
image under `[vm]`, its own GitHub identity and the keychain item
holding its guest token under `[repo]`, and, under `[commands]`, both
the scripts batch drives and the wrapper it is invoked through.
Repo-root discovery is the caller's (batch runs from worktrees, so the
root is `main_repo()`, never the cwd); parsing and validation sit
behind `load_config` and `BatchConfig`, so callers get either a usable
config or a `ConfigError` naming every problem at once.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

CONFIG_FILENAME = "batch.toml"
_SLUG = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")
_COMMAND_KEYS = ("cli", "setup", "session", "agent", "plan_batch")
_REPO_STRING_KEYS = ("author_name", "author_email", "github_token_item")


class ConfigError(RuntimeError):
    """Everything wrong with a `batch.toml`, reported in one go.

    Collecting problems rather than raising on the first one means a
    broken config costs the user one fix pass, not one per mistake.
    """

    def __init__(self, path: Path, problems: Sequence[str]) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        detail = "\n".join(f"  - {problem}" for problem in problems)
        super().__init__(f"{path} is invalid:\n{detail}")


@dataclass(frozen=True)
class Commands:
    """The repo-root-relative script paths `batch` names.

    `cli` is the wrapper the repo invokes `batch` through, so it is the
    name that appears in usage lines and in every remedy batch prints;
    the rest are programs batch drives. Only the paths are
    configurable: their argv shapes are the contract every target
    repo's scripts must honour, and live in `vm.py`.
    """

    cli: str
    setup: str
    session: str
    agent: str
    plan_batch: str


@dataclass(frozen=True)
class _Repo:
    """`[repo]`, whose keys reach `BatchConfig` flat."""

    slug: str
    author_name: str
    author_email: str
    github_token_item: str


@dataclass(frozen=True)
class BatchConfig:
    seed_image: Path
    slug: str
    author_name: str
    author_email: str
    github_token_item: str
    commands: Commands

    def issue_url(self, number: int) -> str:
        """Feeds `batch.vm.session_uuid`, which `commands.session` must match."""
        return f"https://github.com/{self.slug}/issues/{number}"


def load_config(repo: Path) -> BatchConfig:
    """The project's `batch.toml`, parsed and validated."""
    path = repo / CONFIG_FILENAME
    if not path.is_file():
        raise ConfigError(path, ["does not exist"])
    return _parse(path, path.read_text())


def _parse(path: Path, text: str) -> BatchConfig:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(path, [f"could not be parsed: {exc}"]) from exc

    problems: list[str] = []
    seed_image = _parse_vm(document.get("vm"), problems)
    repo = _parse_repo(document.get("repo"), problems)
    commands = _parse_commands(document.get("commands"), problems)
    if problems or seed_image is None or repo is None or commands is None:
        raise ConfigError(path, problems)
    return BatchConfig(
        seed_image=seed_image.expanduser(),
        slug=repo.slug,
        author_name=repo.author_name,
        author_email=repo.author_email,
        github_token_item=repo.github_token_item,
        commands=commands,
    )


def _parse_vm(raw: object, problems: list[str]) -> Path | None:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        problems.append('"vm" must be a [vm] table')
        return None

    table = cast("dict[str, object]", raw)
    seed_image: Path | None = None
    value = table.get("seed_image")
    if value is None:
        problems.append('vm: missing required key "seed_image"')
    elif not isinstance(value, str) or not value:
        problems.append('vm: "seed_image" must be a non-empty string')
    else:
        seed_image = Path(value)

    unknown = sorted(set(table) - {"seed_image"})
    if unknown:
        problems.append(f"vm: unknown key(s) {', '.join(unknown)}")

    return seed_image


def _parse_repo(raw: object, problems: list[str]) -> _Repo | None:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        problems.append('"repo" must be a [repo] table')
        return None

    table = cast("dict[str, object]", raw)
    slug: str | None = None
    value = table.get("slug")
    if value is None:
        problems.append('repo: missing required key "slug"')
    elif not isinstance(value, str) or not value:
        problems.append('repo: "slug" must be a non-empty string')
    elif not _SLUG.fullmatch(value):
        problems.append('repo: "slug" must be "owner/name"')
    else:
        slug = value

    values: dict[str, str] = {}
    for key in _REPO_STRING_KEYS:
        value = table.get(key)
        if value is None:
            problems.append(f'repo: missing required key "{key}"')
        elif not isinstance(value, str) or not value:
            problems.append(f'repo: "{key}" must be a non-empty string')
        else:
            values[key] = value

    unknown = sorted(set(table) - {"slug", *_REPO_STRING_KEYS})
    if unknown:
        problems.append(f"repo: unknown key(s) {', '.join(unknown)}")

    if slug is None or len(values) < len(_REPO_STRING_KEYS):
        return None
    return _Repo(slug=slug, **values)


def _parse_commands(raw: object, problems: list[str]) -> Commands | None:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        problems.append('"commands" must be a [commands] table')
        return None

    table = cast("dict[str, object]", raw)
    values: dict[str, str] = {}
    for key in _COMMAND_KEYS:
        value = table.get(key)
        if value is None:
            problems.append(f'commands: missing required key "{key}"')
        elif not isinstance(value, str) or not value:
            problems.append(f'commands: "{key}" must be a non-empty string')
        else:
            values[key] = value

    unknown = sorted(set(table) - set(_COMMAND_KEYS))
    if unknown:
        problems.append(f"commands: unknown key(s) {', '.join(unknown)}")

    if len(values) < len(_COMMAND_KEYS):
        return None
    return Commands(**values)
