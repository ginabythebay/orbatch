from __future__ import annotations

import errno
import json
import shlex
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
import requests

from batch.config import BatchConfig, ConfigError
from batch.models import (
    AccountCheckError,
    Alignment,
    AlreadyRunningError,
    EmptyTokenError,
    KeychainError,
    OccupancyError,
    Slot,
    UnmountedWorktreeError,
    VmSession,
    VmStatus,
    WrongAccountError,
)
from batch.testing.payloads import (
    TEST_AUTHOR_EMAIL,
    TEST_AUTHOR_NAME,
    TEST_COMMANDS,
    TEST_SLUG,
    TEST_TOKEN_ITEM,
    fake_account,
)
from batch.testing.payloads import TEST_SLUG as OTHER_SLUG
from batch.testing.payloads import batch_config as config
from batch.vm import (
    ACCOUNT_TIMEOUT,
    ACCOUNT_URL,
    CONFIG_MOUNT,
    PS,
    GuestAccount,
    VmRunner,
    account_request,
    agent_command,
    debug_agent_command,
    disks_in_use,
    fetch_login,
    keychain_argv,
    keychain_token,
    plan_batch_command,
    plan_slot_branch,
    secret_env,
    send_chain,
    session_for,
)


def _unreadable_table() -> frozenset[str]:
    raise OccupancyError("ps exited 1")


def _unreadable() -> BatchConfig:
    raise ConfigError(Path("batch.toml"), ("does not exist",))


class TestALazyConfig:
    def test_the_socket_paths_never_resolve_the_config(self, tmp_path: Path) -> None:
        runner = VmRunner(tmp_path, environ={}, config=_unreadable)

        assert runner.socket(1499) == tmp_path / "issue-1499.sock"
        assert runner.log(1499) == tmp_path / "issue-1499.log"
        assert runner.status(1499) is VmStatus.EXITED
        assert runner.attach_command(1499)[:2] == ("dtach", "-a")

    def test_building_a_vibe_invocation_propagates_the_failure(
        self, tmp_path: Path
    ) -> None:
        runner = VmRunner(tmp_path, environ={}, config=_unreadable)

        with pytest.raises(ConfigError):
            _ = runner.vibe_command(
                VmSession(
                    worktree="issue-1499",
                    disk=tmp_path / "issue-1499.raw",
                    config_dir=tmp_path / "config",
                    agent="tools/drive 1499",
                )
            )


class TestSendChain:
    def test_detached_chain_ends_in_an_unconditional_poweroff(self) -> None:
        sends = send_chain(
            config(), worktree="issue-1499", agent="tools/drive 1499", poweroff=True
        )

        assert sends[-1] == (
            "bash -c 'trap : INT; tools/prepare && tools/drive 1499; poweroff'"
        )

    def test_foreground_chain_neither_traps_nor_powers_off(self) -> None:
        sends = send_chain(
            config(), worktree="issue-1499", agent="tools/drive 1499", poweroff=False
        )

        assert sends[-1] == "tools/prepare && tools/drive 1499"
        assert not any("poweroff" in send for send in sends)

    def test_preamble_matches_the_solo_flow(self) -> None:
        sends = send_chain(
            config(OTHER_SLUG), worktree="issue-1499", agent="claude", poweroff=False
        )

        assert sends[:-1] == (
            "export PATH=$PATH:~/.claude/bin:~/.local/bin",
            ". /mnt/claude-config/env",
            "export GIT_DISCOVERY_ACROSS_FILESYSTEM=1",
            "export IS_SANDBOX=1",
            "export EDITOR=emacs",
            "export GH_HOST=github.com",
            f"export GH_REPO={OTHER_SLUG}",
            "export UV_PROJECT_ENVIRONMENT=/root/agent-venv",
            "export MISE_PYTHON_UV_VENV_AUTO=false",
            "export UV_PYTHON_PREFERENCE=only-managed",
            "cd issue-1499",
            "export PATH=$(dirname $(mise which claude)):$PATH",
            f"git config user.email {shlex.quote(TEST_AUTHOR_EMAIL)}",
            f"git config user.name {shlex.quote(TEST_AUTHOR_NAME)}",
        )

    def test_the_guest_cds_into_the_path_relative_to_the_mount_root(self) -> None:
        sends = send_chain(
            config(),
            worktree="widgets/worktrees/issue-1857",
            agent="claude",
            poweroff=False,
        )

        assert "cd widgets/worktrees/issue-1857" in sends

    def test_a_hostile_setup_command_cannot_run_before_the_agent(self) -> None:
        hostile = replace(TEST_COMMANDS, setup="tools/prepare; poweroff")

        sends = send_chain(
            config(commands=hostile),
            worktree="issue-1499",
            agent="tools/drive 1499",
            poweroff=True,
        )

        head, flag, script = shlex.split(sends[-1])
        assert (head, flag) == ("bash", "-c")
        assert shlex.split(script.removeprefix("trap : INT; "))[0] == hostile.setup

    @pytest.mark.parametrize(
        ("field", "setting", "value"),
        [
            ("author_name", "user.name", "Moist; poweroff"),
            ("author_email", "user.email", "ada@example.com; poweroff"),
        ],
    )
    def test_a_hostile_author_cannot_run_a_second_command(
        self, field: str, setting: str, value: str
    ) -> None:
        hostile = replace(config(), **{field: value})

        sends = send_chain(
            hostile, worktree="issue-1499", agent="claude", poweroff=False
        )

        line = next(send for send in sends if send.startswith(f"git config {setting}"))
        assert shlex.split(line) == ["git", "config", setting, value]

    def test_no_secret_ever_reaches_a_send(self) -> None:
        sends = send_chain(
            config(), worktree="issue-1499", agent="claude", poweroff=False
        )

        assert not any("TOKEN=" in send or "API_KEY=" in send for send in sends)


def fake_token(_item: str) -> str:
    return "gh-tok"


class TestSecretEnv:
    def test_the_resolved_token_and_the_environments_key_are_exported(self) -> None:
        rendered = secret_env("gh-tok", {"OPENROUTER_API_KEY": "or"})

        assert rendered == "export GITHUB_TOKEN=gh-tok\nexport OPENROUTER_API_KEY=or\n"

    def test_an_absent_environment_key_exports_as_empty(self) -> None:
        assert (
            secret_env("gh-tok", {})
            == "export GITHUB_TOKEN=gh-tok\nexport OPENROUTER_API_KEY=''\n"
        )

    def test_an_ambient_vibe_gh_token_cannot_reach_the_guest(self) -> None:
        rendered = secret_env("gh-tok", {"VIBE_GH_TOKEN": "stale-tok"})

        assert "stale-tok" not in rendered

    def test_a_hostile_token_cannot_run_a_command(self) -> None:
        rendered = secret_env("a'; rm -rf /; echo '", {})

        assert shlex.split(rendered.splitlines()[0])[0] == "export"
        assert "rm -rf" not in shlex.split(rendered.splitlines()[0])[1].split("=", 1)[0]


class TestKeychainToken:
    def test_the_argv_names_the_item_and_asks_for_the_password_only(self) -> None:
        assert keychain_argv("acme-guest-token") == (
            "security",
            "find-generic-password",
            "-s",
            "acme-guest-token",
            "-w",
        )

    def test_a_resolved_token_arrives_without_the_trailing_newline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _found(
            *_args: object, **_kwargs: object
        ) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="gh-tok\n")

        monkeypatch.setattr("batch.vm.subprocess.run", _found)

        assert keychain_token("acme-guest-token") == "gh-tok"

    def test_a_missing_item_names_itself_and_the_remedy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _absent(*_args: object, **_kwargs: object) -> object:
            raise subprocess.CalledProcessError(44, "security")

        monkeypatch.setattr("batch.vm.subprocess.run", _absent)

        with pytest.raises(KeychainError) as caught:
            _ = keychain_token("acme-guest-token")

        assert caught.value.item == "acme-guest-token"
        assert "add-generic-password -s acme-guest-token" in str(caught.value)


class TestGuestAccount:
    def test_an_empty_token_names_the_keychain_item_and_the_remedy(self) -> None:
        account = GuestAccount(login=lambda _token: "acme")

        with pytest.raises(EmptyTokenError) as caught:
            account.verify("", item=TEST_TOKEN_ITEM, slug=TEST_SLUG)

        assert caught.value.item == TEST_TOKEN_ITEM
        assert f"add-generic-password -U -s {TEST_TOKEN_ITEM}" in str(caught.value)

    def test_a_token_belonging_to_another_account_names_both(self) -> None:
        account = GuestAccount(login=lambda _token: "mallory")

        with pytest.raises(WrongAccountError) as caught:
            account.verify("gh-tok", item=TEST_TOKEN_ITEM, slug=TEST_SLUG)

        assert caught.value.login == "mallory"
        assert caught.value.owner == "acme"
        assert "mallory" in str(caught.value)
        assert "acme" in str(caught.value)

    def test_a_login_matching_the_owner_in_another_case_passes(self) -> None:
        account = GuestAccount(login=lambda _token: "ACME")

        account.verify("gh-tok", item=TEST_TOKEN_ITEM, slug=TEST_SLUG)

    def test_the_same_token_is_looked_up_once_per_process(self) -> None:
        tokens: list[str] = []
        account = GuestAccount(login=lambda token: tokens.append(token) or "acme")

        account.verify("gh-tok", item=TEST_TOKEN_ITEM, slug=TEST_SLUG)
        account.verify("gh-tok", item=TEST_TOKEN_ITEM, slug=TEST_SLUG)

        assert tokens == ["gh-tok"]

    def test_a_different_token_is_looked_up_again(self) -> None:
        tokens: list[str] = []
        account = GuestAccount(login=lambda token: tokens.append(token) or "acme")

        account.verify("gh-tok", item=TEST_TOKEN_ITEM, slug=TEST_SLUG)
        account.verify("other-tok", item=TEST_TOKEN_ITEM, slug=TEST_SLUG)

        assert tokens == ["gh-tok", "other-tok"]


class TestAccountRequest:
    def test_the_lookup_is_an_authorized_timed_get_of_the_user_endpoint(self) -> None:
        request = account_request("gh-tok")

        assert request.url == "https://api.github.com/user"
        assert request.headers["Authorization"] == "Bearer gh-tok"
        assert request.headers["Accept"].startswith("application/vnd.github")
        assert request.timeout > 0


class _Answer:
    def __init__(self, status_code: int, payload: Mapping[str, object]) -> None:
        self.status_code: int = status_code
        self._payload: Mapping[str, object] = payload

    def json(self) -> Mapping[str, object]:
        return self._payload


def _answering(
    status_code: int,
    payload: Mapping[str, object],
    calls: list[tuple[tuple[object, ...], Mapping[str, object]]] | None = None,
) -> Callable[..., _Answer]:
    def _answer(*args: object, **kwargs: object) -> _Answer:
        if calls is not None:
            calls.append((args, kwargs))
        return _Answer(status_code, payload)

    return _answer


class TestFetchLogin:
    def test_an_unreachable_api_is_not_reported_as_a_mismatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _refuse(*_args: object, **_kwargs: object) -> object:
            raise requests.ConnectionError("connection refused")

        monkeypatch.setattr("batch.vm.requests.get", _refuse)

        with pytest.raises(AccountCheckError) as caught:
            _ = fetch_login("gh-tok")

        assert "connection refused" in str(caught.value)
        assert not isinstance(caught.value, WrongAccountError)

    def test_a_revoked_token_reports_the_status_it_was_refused_with(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("batch.vm.requests.get", _answering(401, {}))

        with pytest.raises(AccountCheckError) as caught:
            _ = fetch_login("gh-tok")

        assert "401" in str(caught.value)

    def test_a_body_that_is_not_a_login_is_not_taken_for_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("batch.vm.requests.get", _answering(200, {}))

        with pytest.raises(AccountCheckError) as caught:
            _ = fetch_login("gh-tok")

        assert "login" in str(caught.value)

    def test_the_call_carries_the_authorization_headers_and_a_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[tuple[object, ...], Mapping[str, object]]] = []
        monkeypatch.setattr(
            "batch.vm.requests.get", _answering(200, {"login": "acme"}, calls)
        )

        _ = fetch_login("gh-tok")

        (args, kwargs) = calls[0]
        headers = cast("Mapping[str, str]", kwargs["headers"])
        assert args == (ACCOUNT_URL,)
        assert headers["Authorization"] == "Bearer gh-tok"
        assert headers["Accept"].startswith("application/vnd.github")
        assert kwargs["timeout"] == ACCOUNT_TIMEOUT

    def test_an_answered_login_is_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("batch.vm.requests.get", _answering(200, {"login": "acme"}))

        assert fetch_login("gh-tok") == "acme"


def session(disk: Path) -> VmSession:
    return VmSession(
        worktree="issue-1499",
        disk=disk,
        config_dir=Path("/tmp/claude-config"),
        agent="tools/drive 1499",
    )


class TestSessionFor:
    def _slot(self, mount_root: Path, branch: str = "issue-1857") -> Slot:
        trees = mount_root / "widgets" / "worktrees"
        return Slot(
            branch=branch,
            worktree=trees / branch,
            disk=trees / f"{branch}.raw",
            alignment=Alignment.ALIGNED,
        )

    def test_vibe_runs_from_the_mount_root(self, tmp_path: Path) -> None:
        made = session_for(
            self._slot(tmp_path),
            mount_root=tmp_path,
            config_dir=tmp_path / "config",
            agent="claude",
        )

        assert made.cwd == tmp_path

    def test_the_worktree_travels_relative_to_the_mount_root(
        self, tmp_path: Path
    ) -> None:
        made = session_for(
            self._slot(tmp_path),
            mount_root=tmp_path,
            config_dir=tmp_path / "config",
            agent="claude",
        )

        assert made.worktree == "widgets/worktrees/issue-1857"

    def test_a_worktree_outside_the_mount_root_is_refused(self, tmp_path: Path) -> None:
        slot = self._slot(tmp_path / "elsewhere")

        with pytest.raises(UnmountedWorktreeError):
            _ = session_for(
                slot,
                mount_root=tmp_path / "botland",
                config_dir=tmp_path / "config",
                agent="claude",
            )


class TestLaunchCommand:
    def test_vibe_runs_under_dtach_with_the_console_tee_d_to_a_log(
        self, tmp_path: Path
    ) -> None:
        runner = VmRunner(tmp_path, environ={}, config=config)
        spec = session(tmp_path / "issue-1499.raw")

        launch = runner.launch_command(1499, spec)

        assert launch[:2] == ("dtach", "-n")
        assert launch[2] == str(runner.socket(1499))
        assert launch[3:5] == ("sh", "-c")
        piped = launch[5]
        assert piped.endswith(f" 2>&1 | tee {runner.log(1499)}")
        vibe = piped.removesuffix(f" 2>&1 | tee {runner.log(1499)}")
        assert shlex.split(vibe) == list(runner.vibe_command(spec, poweroff=True))

    def test_the_vibe_command_boots_the_given_disk(self, tmp_path: Path) -> None:
        disk = tmp_path / "issue-1499.raw"

        console = VmRunner(tmp_path, environ={}, config=config).vibe_command(
            session(disk)
        )

        assert console[0] == "vibe"
        assert console[-2:] == ("--", str(disk))
        assert "--mount" in console
        assert f"{Path('/tmp/claude-config')}:{CONFIG_MOUNT}:read-only" in console

    def test_the_runners_config_reaches_the_sends(self, tmp_path: Path) -> None:
        runner = VmRunner(tmp_path, environ={}, config=lambda: config("acme/gizmos"))

        console = runner.vibe_command(session(tmp_path / "issue-1499.raw"))

        assert "export GH_REPO=acme/gizmos" in console


class TestPerIssuePaths:
    def test_two_issues_share_no_socket_or_log(self, tmp_path: Path) -> None:
        runner = VmRunner(tmp_path, environ={}, config=config)

        paths = {runner.socket(1499), runner.log(1499), runner.socket(1500)}

        assert len(paths) == 3
        assert all(path.parent == tmp_path for path in paths)

    def test_the_run_root_is_expanded(self) -> None:
        runner = VmRunner(Path("~/.cache/batch"), environ={}, config=config)

        assert "~" not in str(runner.socket(1499))

    def test_attaching_takes_the_socket_first_and_never_redraws(
        self, tmp_path: Path
    ) -> None:
        runner = VmRunner(tmp_path, environ={}, config=config)

        assert runner.attach_command(1499) == (
            "dtach",
            "-a",
            str(runner.socket(1499)),
            "-r",
            "none",
        )


class TestAgentCommand:
    def test_model_reaches_ralph_and_is_absent_when_unset(self) -> None:
        assert agent_command(config(), issue=1499, model="opus") == (
            "tools/drive 1499 --model opus"
        )
        assert agent_command(config(), issue=1499) == "tools/drive 1499"

    def test_hostile_guidance_cannot_break_out_of_the_send(self) -> None:
        guidance = 'don\'t; poweroff # "quoted" $(echo hi)\nnewline'

        agent = agent_command(config(), issue=1499, guidance=guidance)
        sends = send_chain(config(), worktree="issue-1499", agent=agent, poweroff=True)

        head, flag, script = shlex.split(sends[-1])
        assert (head, flag) == ("bash", "-c")
        assert script.endswith("; poweroff")
        assert shlex.split(script.removesuffix("; poweroff"))[-1] == guidance

    def test_model_reaches_an_interactive_session(self) -> None:
        assert agent_command(config(), model="opus") == (
            "claude --model opus --allow-dangerously-skip-permissions"
            " --dangerously-skip-permissions"
        )


class TestDebugAgentCommand:
    def test_the_guest_decides_between_resuming_and_starting(self) -> None:
        assert debug_agent_command(config(), 1597) == (
            "tools/session 1597 --debug --"
            " --allow-dangerously-skip-permissions --dangerously-skip-permissions"
        )

    def test_the_model_reaches_the_resumed_session(self) -> None:
        assert " --model opus " in debug_agent_command(config(), 1597, model="opus")


class TestPlanCommands:
    def test_the_planning_vm_runs_one_driver_session_over_every_target(self) -> None:
        assert plan_batch_command(config(), (1492, 1601), model="opus") == (
            "tools/plan 1492 1601 --model opus"
        )
        assert plan_batch_command(config(), (1492,)) == "tools/plan 1492"

    def test_each_planning_invocation_names_its_own_slot(self) -> None:
        assert plan_slot_branch(4321) == "plan-4321"
        assert plan_slot_branch(4321) != plan_slot_branch(4322)


class TestStagedConfig:
    def _home(self, monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        _ = (home / ".claude.json").write_text('{"authed": true}\n')
        monkeypatch.setenv("HOME", str(home))

    def test_staging_copies_the_config_and_writes_the_secrets(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._home(monkeypatch, tmp_path / "home")
        items: list[str] = []
        runner = VmRunner(
            tmp_path / "run",
            environ={},
            config=config,
            token=lambda item: items.append(item) or fake_token(item),
            account=fake_account(),
        )

        staged = runner.config_dir(1499)
        runner.write_config(staged)

        assert items == [TEST_TOKEN_ITEM]
        assert (staged / ".claude.json").read_text() == '{"authed": true}\n'
        assert "export GITHUB_TOKEN=gh-tok" in (staged / "env").read_text()
        assert (staged / "env").stat().st_mode & 0o777 == 0o600

    def test_staging_refuses_a_keychain_item_holding_an_empty_password(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._home(monkeypatch, tmp_path / "home")
        runner = VmRunner(
            tmp_path / "run",
            environ={},
            config=config,
            token=lambda _item: "",
            account=fake_account(),
        )
        staged = runner.config_dir(1499)

        with pytest.raises(EmptyTokenError) as caught:
            runner.write_config(staged)

        assert caught.value.item == TEST_TOKEN_ITEM
        assert not (staged / "env").exists()

    def test_staging_refuses_a_token_owned_by_another_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._home(monkeypatch, tmp_path / "home")
        runner = VmRunner(
            tmp_path / "run",
            environ={},
            config=config,
            token=fake_token,
            account=fake_account("mallory"),
        )
        staged = runner.config_dir(1499)

        with pytest.raises(WrongAccountError):
            runner.write_config(staged)

        assert not (staged / "env").exists()

    def test_staging_fails_instead_of_reading_the_developers_claude_json(
        self, tmp_path: Path
    ) -> None:
        runner = VmRunner(
            tmp_path / "run",
            environ={},
            config=config,
            token=fake_token,
            account=fake_account(),
        )
        staged = runner.config_dir(1499)
        home = Path.home()

        with pytest.raises(OSError) as caught:
            runner.write_config(staged)

        assert caught.value.errno == errno.ENOTDIR
        assert str(home) in str(caught.value)
        assert not (staged / ".claude.json").exists()

    def test_a_headless_dir_gets_settings_that_deny_background_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._home(monkeypatch, tmp_path / "home")
        runner = VmRunner(
            tmp_path / "run",
            environ={},
            config=config,
            token=fake_token,
            account=fake_account(),
        )

        staged = runner.config_dir(1499)
        runner.write_config(staged, headless=True)

        settings = cast(
            "dict[str, object]",
            json.loads((staged / "settings.json").read_text()),
        )
        assert settings == {
            "permissions": {"deny": ["Monitor"]},
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash|Agent",
                        "hooks": [
                            {
                                "type": "command",
                                "command": '"$CLAUDE_PROJECT_DIR"/.claude/hooks/no-background.sh',
                            }
                        ],
                    }
                ]
            },
        }
        assert (staged / ".claude.json").exists()
        assert (staged / "env").exists()

    def test_an_interactive_dir_gets_no_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._home(monkeypatch, tmp_path / "home")
        runner = VmRunner(
            tmp_path / "run",
            environ={},
            config=config,
            token=fake_token,
            account=fake_account(),
        )

        staged = runner.named_config_dir("plan-1499")
        runner.write_config(staged)

        assert not (staged / "settings.json").exists()

    def test_reusing_a_headless_dir_interactively_drops_the_restrictions(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._home(monkeypatch, tmp_path / "home")
        runner = VmRunner(
            tmp_path / "run",
            environ={},
            config=config,
            token=fake_token,
            account=fake_account(),
        )
        staged = runner.config_dir(1499)
        runner.write_config(staged, headless=True)

        runner.write_config(staged)

        assert not (staged / "settings.json").exists()

    def test_the_secret_file_is_not_world_readable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._home(monkeypatch, tmp_path / "home")
        runner = VmRunner(
            tmp_path / "run",
            environ={},
            config=config,
            token=fake_token,
            account=fake_account(),
        )

        staged = runner.config_dir(1499)
        runner.write_config(staged)

        assert (staged / "env").stat().st_mode & 0o077 == 0

    def test_cleaning_removes_the_staged_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._home(monkeypatch, tmp_path / "home")
        runner = VmRunner(
            tmp_path / "run",
            environ={},
            config=config,
            token=fake_token,
            account=fake_account(),
        )
        runner.write_config(runner.config_dir(1499))

        assert runner.clean(1499) is True
        assert not runner.config_dir(1499).exists()
        assert runner.clean(1499) is False


class TestLaunchGuard:
    def test_a_second_launch_over_a_live_socket_is_refused(
        self, tmp_path: Path
    ) -> None:
        runner = VmRunner(tmp_path, environ={}, config=config)
        runner.socket(1499).touch()

        with pytest.raises(AlreadyRunningError):
            runner.launch(1499, session(tmp_path / "issue-1499.raw"))


class TestImplOnlyAgentCommand:
    def test_impl_only_replaces_the_guidance_positional(self) -> None:
        command = agent_command(
            config(), issue=1500, guidance="ignored", impl_only=True
        )

        assert command == "tools/drive 1500 --impl-only"

    def test_predecessors_are_a_comma_separated_list(self) -> None:
        command = agent_command(
            config(), issue=1500, base="issue-1499", predecessors=[1498, 1499]
        )

        assert command == (
            "tools/drive 1500 --base issue-1499 --predecessors 1498,1499"
        )

    def test_no_predecessors_leaves_the_flag_off(self) -> None:
        assert agent_command(config(), issue=1500, predecessors=()) == (
            "tools/drive 1500"
        )

    def test_the_planning_caps_follow_the_predecessors(self) -> None:
        command = agent_command(
            config(),
            issue=1500,
            max_tests=3,
            plan_guidance="skip the ui",
            model="opus",
        )

        assert command == (
            "tools/drive 1500 --max-tests 3 --plan-guidance 'skip the ui' --model opus"
        )

    def test_guidance_is_shell_quoted_as_one_token(self) -> None:
        command = agent_command(
            config(), issue=1500, guidance="no new tests; lint stays green"
        )

        assert shlex.split(command) == [
            "tools/drive",
            "1500",
            "no new tests; lint stays green",
        ]


PS_OUTPUT = """\
/sbin/launchd
/usr/libexec/logd
/usr/sbin/distnoted agent
/usr/libexec/rapportd
login -pf gina
"""


def idle(tmp_path: Path) -> VmRunner:
    return VmRunner(tmp_path, environ={}, config=config, disks=frozenset)


def vibe_line(tmp_path: Path, branch: str = "issue-9") -> str:
    """ps space-joins argv verbatim; shlex.join would quote the --send payloads."""
    session = VmSession(
        worktree=branch,
        disk=tmp_path / f"{branch}.raw",
        config_dir=tmp_path / f"{branch}.config",
        agent="tools/drive 9",
    )
    return " ".join(idle(tmp_path).vibe_command(session, poweroff=True))


class TestDisksInUse:
    def test_a_live_vibe_command_line_names_its_disk(self, tmp_path: Path) -> None:
        assert disks_in_use(f"{PS_OUTPUT}{vibe_line(tmp_path)}\n") == {"issue-9.raw"}

    def test_only_whole_argv_tokens_count_as_a_disk(self, tmp_path: Path) -> None:
        text = f"vibe -- {tmp_path}/plan-133.raw\nvibe -- {tmp_path}/plan-13.raw.bak\n"

        assert disks_in_use(text) == {"plan-133.raw"}

    def test_a_command_line_clipped_before_its_disk_names_nothing(
        self, tmp_path: Path
    ) -> None:
        # Why PS asks ps for -ww: the disk is the last argv token, and BSD ps
        # clips to the terminal width unless told not to.
        clipped = vibe_line(tmp_path)[:200]

        assert len(vibe_line(tmp_path)) > 750
        assert disks_in_use(f"{clipped}\n") == frozenset()
        assert "-Awwo" in PS


class TestStatus:
    def test_a_live_socket_reads_as_running_by_issue_and_by_branch(
        self, tmp_path: Path
    ) -> None:
        runner = idle(tmp_path)
        runner.socket(1499).touch()

        assert runner.status(1499) is VmStatus.RUNNING
        assert runner.status_branch("issue-1499") == runner.status(1499)

    def test_an_absent_socket_reads_as_exited_by_issue_and_by_branch(
        self, tmp_path: Path
    ) -> None:
        runner = idle(tmp_path)

        assert runner.status(1499) is VmStatus.EXITED
        assert runner.status_branch("issue-1499") == runner.status(1499)

    def test_an_ad_hoc_branch_is_tracked_by_its_own_socket(
        self, tmp_path: Path
    ) -> None:
        runner = idle(tmp_path)
        (tmp_path / "openfix.sock").touch()

        assert runner.status_branch("openfix") is VmStatus.RUNNING
        assert runner.status_branch("split") is VmStatus.EXITED

    def test_an_attached_console_reads_as_running_with_no_socket(
        self, tmp_path: Path
    ) -> None:
        runner = VmRunner(
            tmp_path,
            environ={},
            config=config,
            disks=lambda: frozenset({"issue-1499.raw"}),
        )

        assert not runner.socket(1499).exists()
        assert runner.status(1499) is VmStatus.RUNNING

    def test_an_existing_socket_answers_before_the_process_table_is_read(
        self, tmp_path: Path
    ) -> None:
        # `vm launch`'s "don't boot a second VM on this disk" guard must survive
        # a host whose process table cannot be read.
        runner = VmRunner(tmp_path, environ={}, config=config, disks=_unreadable_table)
        runner.socket(1499).touch()

        assert runner.status(1499) is VmStatus.RUNNING

    def test_an_unreadable_process_table_refuses_rather_than_reporting_exited(
        self, tmp_path: Path
    ) -> None:
        runner = VmRunner(tmp_path, environ={}, config=config, disks=_unreadable_table)

        with pytest.raises(OccupancyError):
            _ = runner.status(1499)

    def test_neither_signal_reads_as_exited(self, tmp_path: Path) -> None:
        runner = VmRunner(
            tmp_path,
            environ={},
            config=config,
            disks=lambda: frozenset({"issue-1500.raw"}),
        )

        assert runner.status_branch("issue-1499") is VmStatus.EXITED
