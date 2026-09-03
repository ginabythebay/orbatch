"""Tests for the per-project `batch.toml`.

Loading goes through the real file, with only the repo root redirected
at a tmp_path, so these exercise the path a user's config actually
takes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from batch.config import (
    CONFIG_FILENAME,
    BatchConfig,
    Commands,
    ConfigError,
    load_config,
)

_VM = '[vm]\nseed_image = "/images/seed.raw"\n'
_REPO_DEFAULTS = {
    "slug": '"owner/name"',
    "author_name": '"Ada Lovelace"',
    "author_email": '"ada@example.com"',
    "github_token_item": '"acme-guest-token"',
}


def _repo(**overrides: str | None) -> str:
    """A `[repo]` table; an override is raw TOML, or None to omit the key."""
    values = _REPO_DEFAULTS | overrides
    body = "".join(f"{key} = {v}\n" for key, v in values.items() if v is not None)
    return f"[repo]\n{body}"


_REPO = _repo()
_COMMANDS = (
    "[commands]\n"
    'cli = "dev/batch"\n'
    'setup = "dev/setup"\n'
    'session = "dev/session"\n'
    'agent = "dev/agent"\n'
    'plan_batch = "dev/plan-batch"\n'
)


def _write(root: Path, text: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _ = (root / CONFIG_FILENAME).write_text(text)
    return root


class TestLoad:
    def test_the_seed_image_arrives_expanded(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path, f'[vm]\nseed_image = "~/images/seed.raw"\n{_REPO}{_COMMANDS}'
        )

        config = load_config(root)

        assert config.seed_image == Path.home() / "images/seed.raw"

    def test_the_root_is_the_argument_not_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # batch runs from worktrees, so a cwd-relative read would find
        # the wrong repo's config, or none at all.
        root = _write(tmp_path / "repo", _VM + _REPO + _COMMANDS)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        assert load_config(root).seed_image == Path("/images/seed.raw")

    def test_the_five_commands_arrive_as_written(self, tmp_path: Path) -> None:
        root = _write(tmp_path, _VM + _REPO + _COMMANDS)

        assert load_config(root).commands == Commands(
            cli="dev/batch",
            setup="dev/setup",
            session="dev/session",
            agent="dev/agent",
            plan_batch="dev/plan-batch",
        )

    def test_the_repo_table_carries_the_slug_the_git_author_and_the_token_item(
        self, tmp_path: Path
    ) -> None:
        root = _write(tmp_path, _VM + _REPO + _COMMANDS)

        config = load_config(root)

        assert (
            config.slug,
            config.author_name,
            config.author_email,
            config.github_token_item,
        ) == (
            "owner/name",
            "Ada Lovelace",
            "ada@example.com",
            "acme-guest-token",
        )


class TestValidation:
    def test_an_absent_config_names_the_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError) as caught:
            _ = load_config(tmp_path)

        assert caught.value.problems == ("does not exist",)
        assert CONFIG_FILENAME in str(caught.value)

    def test_an_absent_vm_table_names_the_missing_key(self, tmp_path: Path) -> None:
        root = _write(tmp_path, _REPO + _COMMANDS)

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == ('vm: missing required key "seed_image"',)

    @pytest.mark.parametrize(
        "value",
        ["7", '""'],
        ids=["integer", "empty"],
    )
    def test_an_unusable_seed_image_names_the_key(
        self, tmp_path: Path, value: str
    ) -> None:
        root = _write(tmp_path, f"[vm]\nseed_image = {value}\n{_REPO}{_COMMANDS}")

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == ('vm: "seed_image" must be a non-empty string',)

    def test_malformed_toml_says_so(self, tmp_path: Path) -> None:
        root = _write(tmp_path, "[vm\nseed_image =\n")

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems[0].startswith("could not be parsed")

    def test_every_problem_in_the_vm_table_arrives_together(
        self, tmp_path: Path
    ) -> None:
        root = _write(tmp_path, f"[vm]\nram = 8\ncpus = 2\n{_REPO}{_COMMANDS}")

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == (
            'vm: missing required key "seed_image"',
            "vm: unknown key(s) cpus, ram",
        )

    def test_a_vm_that_is_not_a_table_says_so(self, tmp_path: Path) -> None:
        root = _write(tmp_path, f'vm = "seed.raw"\n{_REPO}{_COMMANDS}')

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == ('"vm" must be a [vm] table',)

    def test_an_absent_repo_table_names_the_missing_key(self, tmp_path: Path) -> None:
        root = _write(tmp_path, _VM + _COMMANDS)

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == (
            'repo: missing required key "slug"',
            'repo: missing required key "author_name"',
            'repo: missing required key "author_email"',
            'repo: missing required key "github_token_item"',
        )

    @pytest.mark.parametrize(
        "value",
        ["7", '""'],
        ids=["integer", "empty"],
    )
    def test_an_unusable_slug_names_the_key(self, tmp_path: Path, value: str) -> None:
        root = _write(tmp_path, _VM + _repo(slug=value) + _COMMANDS)

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == ('repo: "slug" must be a non-empty string',)

    @pytest.mark.parametrize(
        "value",
        [
            "name",
            "/name",
            "owner/",
            "a/b/c",
            "own er/name",
            "acme/widgets;poweroff",
            "acme/$(id)",
        ],
        ids=[
            "no-slash",
            "empty-owner",
            "empty-name",
            "three-segments",
            "space",
            "semicolon",
            "substitution",
        ],
    )
    def test_a_malformed_slug_names_the_shape_it_wants(
        self, tmp_path: Path, value: str
    ) -> None:
        root = _write(tmp_path, _VM + _repo(slug=f'"{value}"') + _COMMANDS)

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == ('repo: "slug" must be "owner/name"',)

    @pytest.mark.parametrize(
        "key", ["author_name", "author_email", "github_token_item"]
    )
    @pytest.mark.parametrize(
        ("value", "problem"),
        [
            (None, 'repo: missing required key "{key}"'),
            ("7", 'repo: "{key}" must be a non-empty string'),
            ('""', 'repo: "{key}" must be a non-empty string'),
        ],
        ids=["missing", "integer", "empty"],
    )
    def test_each_unusable_author_key_is_reported_on_its_own(
        self, tmp_path: Path, key: str, value: str | None, problem: str
    ) -> None:
        root = _write(tmp_path, _VM + _repo(**{key: value}) + _COMMANDS)

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == (problem.format(key=key),)

    def test_a_repo_that_is_not_a_table_says_so(self, tmp_path: Path) -> None:
        root = _write(tmp_path, f'repo = "owner/name"\n{_VM}{_COMMANDS}')

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == ('"repo" must be a [repo] table',)

    def test_an_unknown_repo_key_is_refused(self, tmp_path: Path) -> None:
        root = _write(tmp_path, _VM + _repo() + 'host = "gh"\n' + _COMMANDS)

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == ("repo: unknown key(s) host",)

    def test_an_absent_commands_table_names_every_missing_key(
        self, tmp_path: Path
    ) -> None:
        root = _write(tmp_path, _VM + _REPO)

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == (
            'commands: missing required key "cli"',
            'commands: missing required key "setup"',
            'commands: missing required key "session"',
            'commands: missing required key "agent"',
            'commands: missing required key "plan_batch"',
        )

    @pytest.mark.parametrize("key", ["cli", "setup", "session", "agent", "plan_batch"])
    @pytest.mark.parametrize(
        ("value", "problem"),
        [
            (None, 'commands: missing required key "{key}"'),
            ("7", 'commands: "{key}" must be a non-empty string'),
            ('""', 'commands: "{key}" must be a non-empty string'),
        ],
        ids=["missing", "integer", "empty"],
    )
    def test_each_unusable_command_is_reported_on_its_own(
        self, tmp_path: Path, key: str, value: str | None, problem: str
    ) -> None:
        lines = [
            f"{name} = {value}" if name == key else f'{name} = "dev/{name}"'
            for name in ("cli", "setup", "session", "agent", "plan_batch")
            if not (name == key and value is None)
        ]
        root = _write(tmp_path, _VM + _REPO + "[commands]\n" + "\n".join(lines) + "\n")

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == (problem.format(key=key),)

    def test_a_commands_entry_that_is_not_a_table_says_so(self, tmp_path: Path) -> None:
        root = _write(tmp_path, f'commands = "dev/ralph"\n{_VM}{_REPO}')

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == ('"commands" must be a [commands] table',)

    def test_a_missing_cli_joins_the_other_problems(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path,
            _VM
            + _REPO
            + "[commands]\n"
            + 'setup = "dev/setup"\n'
            + 'session = ""\n'
            + 'agent = "dev/agent"\n'
            + 'plan_batch = "dev/plan-batch"\n',
        )

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == (
            'commands: missing required key "cli"',
            'commands: "session" must be a non-empty string',
        )

    def test_an_unknown_commands_key_is_refused(self, tmp_path: Path) -> None:
        root = _write(tmp_path, _VM + _REPO + _COMMANDS + 'review = "dev/review"\n')

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == ("commands: unknown key(s) review",)

    def test_problems_from_all_three_tables_arrive_in_one_report(
        self, tmp_path: Path
    ) -> None:
        root = _write(
            tmp_path,
            "[vm]\nseed_image = 7\n"
            + _repo(slug='"nope"')
            + '[commands]\ncli = "dev/batch"\nsetup = "dev/setup"\nsession = ""\n',
        )

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == (
            'vm: "seed_image" must be a non-empty string',
            'repo: "slug" must be "owner/name"',
            'commands: "session" must be a non-empty string',
            'commands: missing required key "agent"',
            'commands: missing required key "plan_batch"',
        )

    def test_a_bad_slug_a_bad_author_and_a_bad_seed_image_arrive_together(
        self, tmp_path: Path
    ) -> None:
        root = _write(
            tmp_path,
            "[vm]\nseed_image = 7\n"
            + _repo(slug='"nope"', author_name='""', author_email=None)
            + _COMMANDS,
        )

        with pytest.raises(ConfigError) as caught:
            _ = load_config(root)

        assert caught.value.problems == (
            'vm: "seed_image" must be a non-empty string',
            'repo: "slug" must be "owner/name"',
            'repo: "author_name" must be a non-empty string',
            'repo: missing required key "author_email"',
        )


class TestAFullyPopulatedConfig:
    def test_every_key_round_trips(self, tmp_path: Path) -> None:
        root = _write(
            tmp_path,
            f'[vm]\nseed_image = "~/images/seed.raw"\n{_REPO}{_COMMANDS}',
        )

        assert load_config(root) == BatchConfig(
            seed_image=Path("~/images/seed.raw").expanduser(),
            slug="owner/name",
            author_name="Ada Lovelace",
            author_email="ada@example.com",
            github_token_item="acme-guest-token",
            commands=Commands(
                cli="dev/batch",
                setup="dev/setup",
                session="dev/session",
                agent="dev/agent",
                plan_batch="dev/plan-batch",
            ),
        )
