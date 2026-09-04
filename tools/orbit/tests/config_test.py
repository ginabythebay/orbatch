"""Tests for the per-project `.orbit.toml`.

Loading goes through the real file and the real `git rev-parse`, with
only the repo root redirected at a tmp_path, so these exercise the
path a user's config actually takes.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from unittest.mock import patch

import pytest

from orbit.config import (
    CONFIG_FILENAME,
    CommandMode,
    ConfigError,
    CustomCommand,
    Milestones,
    ProjectConfig,
    load_config,
)
from orbit.tui.app import OrbitApp

# A stand-in for OrbitApp's real roster; these tests care that
# collisions are rejected, not which keys orbit happens to bind.
_RESERVED = frozenset({"e", "c", "b", "q", "escape"})

_MILESTONES = Milestones(current="sprint 42", backlog="Icebox")
_MILESTONE_TOML = """
[milestone]
current = "sprint 42"
backlog = "Icebox"
"""


def _load(
    tmp_path: Path,
    text: str | None = None,
    reserved: frozenset[str] = _RESERVED,
) -> ProjectConfig:
    """Load a config from `tmp_path`, writing `text` there first if given."""
    if text is not None:
        _ = (tmp_path / CONFIG_FILENAME).write_text(text)
    with patch("orbit.config.repo_root", return_value=tmp_path):
        return load_config(reserved)


def _load_commands(
    tmp_path: Path,
    text: str,
    reserved: frozenset[str] = _RESERVED,
) -> tuple[CustomCommand, ...]:
    """The commands from a config that also carries a valid [milestone]."""
    return _load(tmp_path, _MILESTONE_TOML + text, reserved).commands


class TestLoad:
    def test_milestones_and_commands_come_from_one_load(self, tmp_path: Path) -> None:
        config = _load(
            tmp_path,
            _MILESTONE_TOML
            + """
            [[commands]]
            key = "w"
            label = "Worktree"
            run = "vwt {branch} {issue}"

            [[commands]]
            key = "v"
            label = "Edit"
            run = "vim {issue}"
            """,
        )
        assert config == ProjectConfig(
            milestones=_MILESTONES,
            commands=(
                CustomCommand(key="w", label="Worktree", run="vwt {branch} {issue}"),
                CustomCommand(key="v", label="Edit", run="vim {issue}"),
            ),
        )

    def test_valid_config_parses_with_spawn_as_the_default_mode(
        self, tmp_path: Path
    ) -> None:
        commands = _load_commands(
            tmp_path,
            """
            [[commands]]
            key = "w"
            label = "Worktree"
            run = "vwt {branch} {issue}"

            [[commands]]
            key = "v"
            label = "Edit"
            run = "vim {issue}"
            mode = "suspend"
            """,
        )
        assert commands == (
            CustomCommand(
                key="w",
                label="Worktree",
                run="vwt {branch} {issue}",
                mode=CommandMode.SPAWN,
            ),
            CustomCommand(
                key="v", label="Edit", run="vim {issue}", mode=CommandMode.SUSPEND
            ),
        )

    def test_milestones_alone_yield_no_commands(self, tmp_path: Path) -> None:
        # Binding custom keys is optional; naming the milestones is not.
        assert _load(tmp_path, _MILESTONE_TOML) == ProjectConfig(milestones=_MILESTONES)

    def test_missing_config_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as excinfo:
            _ = _load(tmp_path)
        assert CONFIG_FILENAME in str(excinfo.value)

    def test_outside_a_git_repo_is_rejected(self) -> None:
        # No root means no file to read the milestones from, and every
        # milestone-dependent command needs them.
        with (
            patch(
                "orbit.config.repo_root",
                side_effect=RuntimeError("not a git repository"),
            ),
            pytest.raises(ConfigError, match="not in a git repository"),
        ):
            _ = load_config(_RESERVED)

    def test_missing_milestone_section_names_both_keys(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as excinfo:
            _ = _load(tmp_path, "# nothing here yet\n")
        assert excinfo.value.problems == (
            'milestone: missing required key "current"',
            'milestone: missing required key "backlog"',
        )

    @pytest.mark.parametrize(
        ("section", "expected"),
        [
            pytest.param(
                'backlog = "Icebox"',
                'milestone: missing required key "current"',
                id="missing-current",
            ),
            pytest.param(
                'current = "sprint 42"',
                'milestone: missing required key "backlog"',
                id="missing-backlog",
            ),
            pytest.param(
                'current = 3\nbacklog = "Icebox"',
                'milestone: "current" must be a non-empty string',
                id="non-string",
            ),
            pytest.param(
                'current = "sprint 42"\nbacklog = ""',
                'milestone: "backlog" must be a non-empty string',
                id="empty-string",
            ),
            pytest.param(
                'current = "sprint 42"\nbacklog = "Icebox"\nnext = "sprint 43"',
                "milestone: unknown key(s) next",
                id="unknown-key",
            ),
        ],
    )
    def test_invalid_milestone_section_is_rejected(
        self, tmp_path: Path, section: str, expected: str
    ) -> None:
        with pytest.raises(ConfigError) as excinfo:
            _ = _load(tmp_path, f"[milestone]\n{section}\n")
        assert expected in str(excinfo.value)

    def test_milestone_written_as_a_bare_value_is_rejected(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(ConfigError, match=r"must be a \[milestone\] table"):
            _ = _load(tmp_path, 'milestone = "sprint 42"\n')

    def test_milestone_and_command_problems_arrive_together(
        self, tmp_path: Path
    ) -> None:
        # One loader, one read: a broken file costs the user one fix
        # pass across both sections, not one per section.
        with pytest.raises(ConfigError) as excinfo:
            _ = _load(
                tmp_path,
                """
                [milestone]
                current = "sprint 42"

                [[commands]]
                key = "e"
                label = "Collides"
                run = "x"
                """,
            )
        assert excinfo.value.problems == (
            'milestone: missing required key "backlog"',
            'commands[0]: key "e" is already bound by orbit',
        )

    def test_malformed_toml_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as excinfo:
            _ = _load_commands(tmp_path, "[[commands]]\nkey = 'w'\nlabel = broken\n")
        assert "could not be parsed" in str(excinfo.value)

    def test_key_colliding_with_a_builtin_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as excinfo:
            _ = _load_commands(
                tmp_path,
                """
                [[commands]]
                key = "e"
                label = "Worktree"
                run = "vwt {issue}"
                """,
            )
        assert 'key "e" is already bound by orbit' in str(excinfo.value)

    def test_duplicate_custom_key_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as excinfo:
            _ = _load_commands(
                tmp_path,
                """
                [[commands]]
                key = "w"
                label = "Worktree"
                run = "vwt {issue}"

                [[commands]]
                key = "w"
                label = "Other"
                run = "other {issue}"
                """,
            )
        assert 'key "w" is already used by commands[0]' in str(excinfo.value)

    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            pytest.param(
                'key = "w"\nlabel = "W"\nrun = "x"\nmode = "detach"',
                "unknown mode 'detach'",
                id="unknown-mode",
            ),
            pytest.param(
                'label = "W"\nrun = "x"',
                'missing required field "key"',
                id="missing-key",
            ),
            pytest.param(
                'key = "w"\nrun = "x"',
                'missing required field "label"',
                id="missing-label",
            ),
            pytest.param(
                'key = "w"\nlabel = "W"',
                'missing required field "run"',
                id="missing-run",
            ),
            pytest.param(
                'key = "worktree"\nlabel = "W"\nrun = "x"',
                '"key" must be a single letter or digit',
                id="multi-char-key",
            ),
            pytest.param(
                'key = "?"\nlabel = "W"\nrun = "x"',
                '"key" must be a single letter or digit',
                id="punctuation-key",
            ),
            pytest.param(
                'key = "w"\nlabel = "W"\nrun = "x"\nprompt = "branch"',
                "unknown field(s) prompt",
                id="unknown-field",
            ),
        ],
    )
    def test_invalid_entry_is_rejected(
        self, tmp_path: Path, entry: str, expected: str
    ) -> None:
        with pytest.raises(ConfigError) as excinfo:
            _ = _load_commands(tmp_path, f"[[commands]]\n{entry}\n")
        assert expected in str(excinfo.value)

    def test_every_problem_is_reported_at_once(self, tmp_path: Path) -> None:
        # One run of orbit should tell the user everything to fix, so a
        # broken config costs one edit pass rather than one per mistake.
        with pytest.raises(ConfigError) as excinfo:
            _ = _load_commands(
                tmp_path,
                """
                [[commands]]
                key = "e"
                label = "Collides"
                run = "x"

                [[commands]]
                key = "worktree"
                run = "y"
                mode = "detach"
                """,
            )
        assert excinfo.value.problems == (
            'commands[0]: key "e" is already bound by orbit',
            'commands[1]: missing required field "label"',
            'commands[1]: unknown mode \'detach\' (expected "spawn" or "suspend")',
            'commands[1]: "key" must be a single letter or digit, got "worktree"',
        )


class TestRender:
    def test_substitutes_issue_and_branch(self) -> None:
        command = CustomCommand(key="w", label="Worktree", run="vwt {branch} {issue}")
        assert command.render(20, "cache-lookups") == "vwt cache-lookups 20"

    def test_issue_alone_renders_without_a_branch(self) -> None:
        command = CustomCommand(key="w", label="Worktree", run="vwt {issue}")
        assert command.render(20) == "vwt 20"

    def test_substituted_values_cannot_inject_shell_syntax(self) -> None:
        # `run` is handed to a shell, so a branch name is the one piece
        # of attacker-ish input on the command line: it must survive as
        # a single literal argument, not as syntax.
        command = CustomCommand(key="w", label="Worktree", run="vwt {branch}")
        rendered = command.render(20, "x'; rm -rf /; #")
        assert shlex.split(rendered) == ["vwt", "x'; rm -rf /; #"]

    def test_branch_with_spaces_stays_one_argument(self) -> None:
        command = CustomCommand(key="w", label="Worktree", run="vwt {branch}")
        assert shlex.split(command.render(20, "two words")) == ["vwt", "two words"]

    def test_rendering_without_a_needed_branch_is_refused(self) -> None:
        # Substituting nothing would put a literal "{branch}" on the
        # command line and run the wrong thing without saying so.
        command = CustomCommand(key="w", label="Worktree", run="vwt {branch}")
        with pytest.raises(ValueError, match="needs a branch"):
            _ = command.render(20)

    def test_needs_branch_tracks_the_placeholder(self) -> None:
        assert CustomCommand(key="w", label="W", run="vwt {branch}").needs_branch
        assert not CustomCommand(key="w", label="W", run="vwt {issue}").needs_branch


class TestThisRepositorysConfig:
    def test_this_repository_ships_a_loadable_orbit_toml(self) -> None:
        # Every orbit command now dies without it, and every other
        # test in the suite runs against a patched repo root, so this
        # is the only thing standing between a typo in the real file
        # and a green suite.
        config = load_config(OrbitApp.reserved_keys())
        assert config.milestones.current
        assert config.milestones.backlog

    def test_discovery_works_from_a_subdirectory(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # orbit is run from wherever the user happens to be, so discovery
        # rests on `git rev-parse --show-toplevel` rather than the cwd.
        monkeypatch.chdir(Path(__file__).parent)
        config = load_config(OrbitApp.reserved_keys())
        assert config.milestones.current
        assert config.milestones.backlog
