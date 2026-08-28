"""The per-project `.orbit.toml`.

orbit ships no project-specific behavior: the repo names its own
milestones under `[milestone]`, and may register extra key→command
bindings under `[[commands]]` — press the key on a selected issue and
orbit runs the configured shell command. Repo-root discovery, TOML
parsing, validation and placeholder rendering all sit behind
`load_config`, `ProjectConfig` and `CustomCommand`, so callers get
either a usable config or a `ConfigError` naming every problem at once.
"""

from __future__ import annotations

import shlex
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum, auto
from pathlib import Path
from typing import cast

from ghgql.repo import repo_root

CONFIG_FILENAME = ".orbit.toml"

_ISSUE_PLACEHOLDER = "{issue}"
_BRANCH_PLACEHOLDER = "{branch}"


class CommandMode(StrEnum):
    SPAWN = auto()
    SUSPEND = auto()


class ConfigError(RuntimeError):
    """Everything wrong with a `.orbit.toml`, reported in one go.

    Collecting problems rather than raising on the first one means a
    broken config costs the user one fix pass, not one per mistake.
    """

    def __init__(self, path: Path, problems: Sequence[str]) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        detail = "\n".join(f"  - {problem}" for problem in problems)
        super().__init__(f"{path} is invalid:\n{detail}")


@dataclass(frozen=True)
class CustomCommand:
    """One `[[commands]]` entry: a key that runs `run` on the selected issue."""

    key: str
    label: str
    run: str
    mode: CommandMode = CommandMode.SPAWN

    @property
    def needs_branch(self) -> bool:
        """Whether `run` wants a branch name, which only the user can supply."""
        return _BRANCH_PLACEHOLDER in self.run

    def render(self, issue_number: int, branch: str | None = None) -> str:
        """`run` with its placeholders filled in, ready for a shell.

        Substituted values are shell-quoted: `run` is handed to a shell
        (the whole point is that a project can write pipes, quotes and
        `~`), so quoting is what keeps a branch name from being read as
        shell syntax.

        Raises ValueError if `run` needs a branch and none was given —
        rendering a literal "{branch}" into a command line would be a
        silent way to run the wrong thing.
        """
        if branch is None and self.needs_branch:
            raise ValueError(f"{self.run!r} needs a branch, but none was given")
        rendered = self.run.replace(_ISSUE_PLACEHOLDER, shlex.quote(str(issue_number)))
        if branch is not None:
            rendered = rendered.replace(_BRANCH_PLACEHOLDER, shlex.quote(branch))
        return rendered


@dataclass(frozen=True)
class Milestones:
    """The repo's `[milestone]` names: the sprint being worked, and the shelf."""

    current: str
    backlog: str


@dataclass(frozen=True)
class ProjectConfig:
    milestones: Milestones
    commands: tuple[CustomCommand, ...] = ()


def load_config(reserved_keys: frozenset[str]) -> ProjectConfig:
    """The project's `.orbit.toml`, parsed and validated.

    `reserved_keys` are the keys orbit already binds; a config may not
    claim them. Raises `ConfigError` if the file is absent or unusable
    — every command needs a milestone, so there is nothing to degrade
    to.
    """
    try:
        root = repo_root()
    except RuntimeError as exc:
        raise ConfigError(
            Path(CONFIG_FILENAME), ["not in a git repository, so there is none"]
        ) from exc
    path = root / CONFIG_FILENAME
    if not path.is_file():
        raise ConfigError(path, ["does not exist"])
    return _parse(path, path.read_text(), reserved_keys)


def _parse(path: Path, text: str, reserved_keys: frozenset[str]) -> ProjectConfig:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(path, [f"could not be parsed: {exc}"]) from exc

    problems: list[str] = []
    milestones = _parse_milestones(document.get("milestone"), problems)
    commands = _parse_commands(document.get("commands"), reserved_keys, problems)
    if problems or milestones is None:
        raise ConfigError(path, problems)
    return ProjectConfig(milestones=milestones, commands=commands)


def _parse_milestones(raw: object, problems: list[str]) -> Milestones | None:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        problems.append('"milestone" must be a [milestone] table')
        return None

    table = cast("dict[str, object]", raw)
    before = len(problems)
    fields: dict[str, str] = {}
    for name in ("current", "backlog"):
        value = table.get(name)
        if value is None:
            problems.append(f'milestone: missing required key "{name}"')
        elif not isinstance(value, str) or not value:
            problems.append(f'milestone: "{name}" must be a non-empty string')
        else:
            fields[name] = value

    unknown = sorted(set(table) - {"current", "backlog"})
    if unknown:
        problems.append(f"milestone: unknown key(s) {', '.join(unknown)}")

    if len(problems) != before:
        return None
    return Milestones(current=fields["current"], backlog=fields["backlog"])


def _parse_commands(
    raw_entries: object, reserved_keys: frozenset[str], problems: list[str]
) -> tuple[CustomCommand, ...]:
    if raw_entries is None:
        return ()
    if not isinstance(raw_entries, list):
        problems.append('"commands" must be a list of [[commands]] tables')
        return ()

    commands: list[CustomCommand] = []
    claimed: dict[str, int] = {}
    for index, raw_entry in enumerate(cast("list[object]", raw_entries)):
        where = f"commands[{index}]"
        if not isinstance(raw_entry, dict):
            problems.append(f"{where}: must be a [[commands]] table")
            continue
        command = _parse_entry(
            where,
            cast("dict[str, object]", raw_entry),
            reserved_keys,
            claimed,
            problems,
        )
        if command is not None:
            claimed[command.key] = index
            commands.append(command)
    return tuple(commands)


def _parse_entry(
    where: str,
    entry: dict[str, object],
    reserved_keys: frozenset[str],
    claimed: dict[str, int],
    problems: list[str],
) -> CustomCommand | None:
    """One `[[commands]]` table, or None with `problems` appended to.

    Every problem in the entry is recorded, not just the first, so one
    run of orbit reports everything wrong with the file.
    """
    before = len(problems)
    fields: dict[str, str] = {}
    for name in ("key", "label", "run"):
        value = entry.get(name)
        if value is None:
            problems.append(f'{where}: missing required field "{name}"')
        elif not isinstance(value, str) or not value:
            problems.append(f'{where}: "{name}" must be a non-empty string')
        else:
            fields[name] = value

    unknown = sorted(set(entry) - {"key", "label", "run", "mode"})
    if unknown:
        problems.append(f"{where}: unknown field(s) {', '.join(unknown)}")

    raw_mode = entry.get("mode", CommandMode.SPAWN.value)
    mode = CommandMode.SPAWN
    if not isinstance(raw_mode, str) or raw_mode not in set(CommandMode):
        expected = " or ".join(f'"{m.value}"' for m in CommandMode)
        problems.append(f"{where}: unknown mode {raw_mode!r} (expected {expected})")
    else:
        mode = CommandMode(raw_mode)

    key = fields.get("key")
    if key is not None:
        # Letters and digits are the keys whose Textual key name is the
        # character itself. Punctuation ("?" arrives as "question_mark")
        # would bind a name that never fires, so reject it rather than
        # ship a key that silently does nothing.
        if len(key) != 1 or not key.isascii() or not key.isalnum():
            problems.append(
                f'{where}: "key" must be a single letter or digit, got "{key}"'
            )
        elif key in reserved_keys:
            problems.append(f'{where}: key "{key}" is already bound by orbit')
        elif key in claimed:
            problems.append(
                f'{where}: key "{key}" is already used by commands[{claimed[key]}]'
            )

    if len(problems) != before:
        return None
    return CustomCommand(
        key=fields["key"], label=fields["label"], run=fields["run"], mode=mode
    )
