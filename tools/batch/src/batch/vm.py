from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import requests

from batch.config import BatchConfig
from batch.models import (
    DEFAULT_RAM,
    AccountCheckError,
    AlreadyRunningError,
    EmptyTokenError,
    KeychainError,
    Slot,
    UnmountedWorktreeError,
    VmSession,
    VmStatus,
    WrongAccountError,
)
from batch.occupancy import probe_output

CONFIG_MOUNT = "/mnt/claude-config"
ENV_FILE = "env"
ENV_SECRETS = (("OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),)
SECURITY = ("security", "find-generic-password")
HEADLESS_SETTINGS = {
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
DEFAULT_RUN_ROOT = Path("~/.cache/batch")
# -ww twice, not once: BSD ps clips argv to the width of whichever std stream
# is a tty, and the vibe command line runs past 750 characters before its disk.
PS = ("ps", "-Awwo", "args=")
ACCOUNT_URL = "https://api.github.com/user"
ACCOUNT_TIMEOUT = 15
SANDBOX_FLAGS = (
    "--allow-dangerously-skip-permissions",
    "--dangerously-skip-permissions",
)


def agent_command(
    config: BatchConfig,
    *,
    issue: int | None = None,
    guidance: str | None = None,
    base: str | None = None,
    model: str | None = None,
    impl_only: bool = False,
    rework: bool = False,
    headless: bool = False,
    predecessors: Sequence[int] = (),
    max_tests: int | None = None,
    plan_guidance: str | None = None,
) -> str:
    """The command the VM runs once setup finishes; no issue means a bare session."""
    if issue is None:
        if headless:
            raise ValueError("A bare session has no prompt to run headless.")
        argv = ["claude", *_model_flag(model), *SANDBOX_FLAGS]
    else:
        argv = [config.commands.agent, str(issue)]
        if rework:
            argv.append("--rework")
        elif impl_only:
            argv.append("--impl-only")
        elif guidance is not None:
            argv.append(guidance)
        if headless:
            argv.append("--headless")
        if base is not None:
            argv += ["--base", base]
        argv += _predecessors_flag(predecessors)
        if max_tests is not None:
            argv += ["--max-tests", str(max_tests)]
        if plan_guidance is not None:
            argv += ["--plan-guidance", plan_guidance]
        argv += _model_flag(model)
    return shlex.join(argv)


def debug_agent_command(
    config: BatchConfig, issue: int, model: str | None = None
) -> str:
    """Only the guest can tell whether the issue's session exists: the transcript
    is filed under the slug of the *guest's* worktree path.
    """
    return shlex.join(
        [
            config.commands.session,
            str(issue),
            "--debug",
            "--",
            *_model_flag(model),
            *SANDBOX_FLAGS,
        ]
    )


def plan_batch_command(
    config: BatchConfig, targets: Sequence[int], model: str | None = None
) -> str:
    """The command the planning VM runs: one session walking every target."""
    return shlex.join(
        [
            config.commands.plan_batch,
            *(str(number) for number in targets),
            *_model_flag(model),
        ]
    )


def plan_slot_branch(pid: int) -> str:
    """One throwaway worktree per planning invocation, so two sessions never
    share a checkout and neither has to lock the other out."""
    return f"plan-{pid}"


def _model_flag(model: str | None) -> list[str]:
    return ["--model", model] if model else []


def _predecessors_flag(predecessors: Sequence[int]) -> list[str]:
    if not predecessors:
        return []
    return ["--predecessors", ",".join(str(number) for number in predecessors)]


def session_for(
    slot: Slot,
    *,
    mount_root: Path,
    config_dir: Path,
    agent: str,
    ram: int = DEFAULT_RAM,
) -> VmSession:
    """vibe mounts the directory it is launched from as the guest's project dir.

    So the vibe process runs from the mount root — the tree holding every
    repo — and the worktree travels as a path relative to it,
    `<repo>/worktrees/<branch>`. An absolute host path lands in the guest as a
    path that does not exist there.
    """
    return VmSession(
        worktree=str(relative_worktree(slot.worktree, mount_root)),
        disk=slot.disk,
        config_dir=config_dir,
        agent=agent,
        ram=ram,
        cwd=mount_root,
    )


def relative_worktree(worktree: Path, mount_root: Path) -> Path:
    try:
        return worktree.relative_to(mount_root)
    except ValueError as exc:
        raise UnmountedWorktreeError(worktree, mount_root) from exc


def keychain_argv(item: str) -> tuple[str, ...]:
    return (*SECURITY, "-s", item, "-w")


def keychain_token(item: str) -> str:
    """The guest PAT the repo's `batch.toml` names, read from the macOS keychain."""
    try:
        found = subprocess.run(
            keychain_argv(item), capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as exc:
        raise KeychainError(item) from exc
    return found.stdout.rstrip("\n")


@dataclass(frozen=True)
class AccountRequest:
    url: str
    headers: Mapping[str, str]
    timeout: int


def account_request(token: str) -> AccountRequest:
    return AccountRequest(
        url=ACCOUNT_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=ACCOUNT_TIMEOUT,
    )


def fetch_login(token: str) -> str:
    request = account_request(token)
    try:
        response = requests.get(
            request.url, headers=dict(request.headers), timeout=request.timeout
        )
    except requests.RequestException as exc:
        raise AccountCheckError(f"{request.url}: {exc}") from exc
    if response.status_code != 200:
        raise AccountCheckError(f"{request.url} answered {response.status_code}")
    try:
        body = cast("object", response.json())
    except ValueError as exc:
        raise AccountCheckError(
            f"{request.url} answered unreadable JSON: {exc}"
        ) from exc
    missing = AccountCheckError(f"{request.url} answered without a login")
    if not isinstance(body, Mapping):
        raise missing
    login = cast("Mapping[str, object]", body).get("login")
    if not isinstance(login, str) or not login:
        raise missing
    return login


class GuestAccount:
    """Answers "does this token belong to the repo's owner" once per process.

    The memo is keyed on the token, not on a verified flag, so a token from a
    different keychain item is checked on its own.
    """

    def __init__(self, login: Callable[[str], str]) -> None:
        self._login: Callable[[str], str] = login
        self._verified: str | None = None

    def verify(self, token: str, *, item: str, slug: str) -> None:
        if not token:
            raise EmptyTokenError(item)
        if token == self._verified:
            return
        owner = slug.split("/")[0]
        login = self._login(token)
        if login.casefold() != owner.casefold():
            raise WrongAccountError(item=item, login=login, owner=owner)
        self._verified = token


def secret_env(token: str, environ: Mapping[str, str]) -> str:
    """Secrets travel by mounted file: a `--send` lands in argv and the tee'd log."""
    lines = [f"export GITHUB_TOKEN={shlex.quote(token)}\n"]
    lines += [
        f"export {name}={shlex.quote(environ.get(source, ''))}\n"
        for name, source in ENV_SECRETS
    ]
    return "".join(lines)


def send_chain(
    config: BatchConfig, *, worktree: str, agent: str, poweroff: bool
) -> tuple[str, ...]:
    launch = f"{shlex.quote(config.commands.setup)} && {agent}"
    if poweroff:
        launch = f"bash -c {shlex.quote(f'trap : INT; {launch}; poweroff')}"
    return (
        "export PATH=$PATH:~/.claude/bin:~/.local/bin",
        f". {CONFIG_MOUNT}/{ENV_FILE}",
        "export GIT_DISCOVERY_ACROSS_FILESYSTEM=1",
        "export IS_SANDBOX=1",
        "export EDITOR=emacs",
        "export GH_HOST=github.com",
        f"export GH_REPO={shlex.quote(config.slug)}",
        # The venv must live on the VM's own disk: a venv is bound to one
        # interpreter and project path, and the worktree is shared with the
        # macOS host, so a shared .venv leaves one side with a dead interpreter.
        "export UV_PROJECT_ENVIRONMENT=/root/agent-venv",
        # mise would auto-activate ./.venv and export a VIRTUAL_ENV that
        # UV_PROJECT_ENVIRONMENT overrides, warning on every uv command.
        "export MISE_PYTHON_UV_VENV_AUTO=false",
        # Debian's /usr/bin/python3.13 corrupts memory under GC-heavy loads
        # (#1241); the managed interpreter is baked into the prebaked image.
        "export UV_PYTHON_PREFERENCE=only-managed",
        f"cd {shlex.quote(worktree)}",
        "export PATH=$(dirname $(mise which claude)):$PATH",
        f"git config user.email {shlex.quote(config.author_email)}",
        f"git config user.name {shlex.quote(config.author_name)}",
        launch,
    )


def _running_disks() -> frozenset[str]:
    return disks_in_use(probe_output(PS))


def disks_in_use(text: str) -> frozenset[str]:
    """The `.raw` basenames named as a whole argv token in `ps -Ao args=` output.

    Whole tokens only: substring matching would let a live `plan-133` hold its
    unrelated neighbour `plan-13` hostage.
    """
    return frozenset(
        Path(token).name
        for line in text.splitlines()
        for token in line.split()
        if token.endswith(".raw")
    )


class VmRunner:
    def __init__(
        self,
        root: Path = DEFAULT_RUN_ROOT,
        *,
        config: Callable[[], BatchConfig],
        environ: Mapping[str, str] | None = None,
        disks: Callable[[], frozenset[str]] | None = None,
        token: Callable[[str], str] = keychain_token,
        account: GuestAccount | None = None,
    ) -> None:
        self._root: Path = root.expanduser()
        self._config: Callable[[], BatchConfig] = config
        self._environ: Mapping[str, str] = os.environ if environ is None else environ
        self._disks: Callable[[], frozenset[str]] = disks or _running_disks
        self._token: Callable[[str], str] = token
        self._account: GuestAccount = account or GuestAccount(login=fetch_login)

    def socket(self, issue: int) -> Path:
        return self._root / f"issue-{issue}.sock"

    def log(self, issue: int) -> Path:
        return self._root / f"issue-{issue}.log"

    def attach_command(self, issue: int) -> tuple[str, ...]:
        """dtach takes the socket immediately after the mode flag, options after.

        No redraw method can work here — the console is piped through `tee`, so
        nothing downstream of dtach holds a tty. Attaching stays blank until the
        VM writes again; the per-issue log is the scrollback. `-r none` is
        explicit because dtach otherwise defaults to `ctrl_l`.
        """
        return ("dtach", "-a", str(self.socket(issue)), "-r", "none")

    def vibe_command(
        self, session: VmSession, *, poweroff: bool = False
    ) -> tuple[str, ...]:
        sends = send_chain(
            self._config(),
            worktree=session.worktree,
            agent=session.agent,
            poweroff=poweroff,
        )
        mount = f"{session.config_dir}:{CONFIG_MOUNT}:read-only"
        args: list[str] = ["vibe", "--ram", str(session.ram), "--mount", mount]
        for send in sends:
            args += ["--send", send]
        return (*args, "--", str(session.disk))

    def launch_command(self, issue: int, session: VmSession) -> tuple[str, ...]:
        console = shlex.join(self.vibe_command(session, poweroff=True))
        piped = f"{console} 2>&1 | tee {shlex.quote(str(self.log(issue)))}"
        return ("dtach", "-n", str(self.socket(issue)), "sh", "-c", piped)

    def debug_command(self, issue: int, session: VmSession) -> tuple[str, ...]:
        """Detached, so the socket exists for the session's life: it is the only
        signal `launch` and `status` consult before booting a second VM on the
        disk. Unlike `launch_command` it does not tee — the per-issue log holds
        the failed run being debugged, and tee would truncate it.
        """
        return ("dtach", "-n", str(self.socket(issue)), *self.vibe_command(session))

    def config_dir(self, issue: int) -> Path:
        """A durable claude-config mount; a tmpdir would not outlive a detached VM."""
        return self.named_config_dir(f"issue-{issue}")

    def named_config_dir(self, name: str) -> Path:
        return self._root / f"{name}.config"

    def write_config(self, config_dir: Path, *, headless: bool = False) -> None:
        """Stages what the VM mounts read-only: the login, the secrets, and for
        a headless run the settings `commands.agent` layers on with `--settings`.

        The removal is the load-bearing half: a per-issue config dir outlives
        the run that made it, so a stale settings.json would silently restrict
        a later interactive session in the same directory.
        """
        config = self._config()
        token = self._token(config.github_token_item)
        self._account.verify(token, item=config.github_token_item, slug=config.slug)
        config_dir.mkdir(parents=True, exist_ok=True)
        _ = shutil.copy(
            Path("~/.claude.json").expanduser(), config_dir / ".claude.json"
        )
        env = config_dir / ENV_FILE
        _ = env.write_text(secret_env(token, self._environ))
        env.chmod(0o600)
        settings = config_dir / "settings.json"
        if headless:
            _ = settings.write_text(json.dumps(HEADLESS_SETTINGS, indent=2) + "\n")
        else:
            settings.unlink(missing_ok=True)

    def clean(self, issue: int) -> bool:
        return self.clean_config(self.config_dir(issue))

    def clean_config(self, config_dir: Path) -> bool:
        if not config_dir.is_dir():
            return False
        shutil.rmtree(config_dir)
        return True

    def launch(self, issue: int, session: VmSession) -> None:
        if self.status(issue) is VmStatus.RUNNING:
            raise AlreadyRunningError(issue)
        self._root.mkdir(parents=True, exist_ok=True)
        _ = subprocess.run(
            self.launch_command(issue, session), check=True, cwd=session.cwd
        )

    def status(self, issue: int) -> VmStatus:
        return self.status_branch(f"issue-{issue}")

    def status_branch(self, branch: str) -> VmStatus:
        """Two signals, either of which means live: dtach removes the socket when
        the master exits, and a console started attached never had one, so the
        vibe process naming the slot's disk is the only trace it leaves.

        Exit carries no code and means only "stopped": whether the work is
        done is read from the pull request, never from the guest.
        """
        socket = self._root / f"{branch}.sock"
        if socket.exists():
            return VmStatus.RUNNING
        return VmStatus.RUNNING if f"{branch}.raw" in self._disks() else VmStatus.EXITED

    def claim_path(self, branch: str) -> Path:
        return self._root / f"{branch}.claim"

    @contextmanager
    def claimed(self, branch: str) -> Generator[None]:
        """Marks a slot as held for as long as this process lives.

        Outlives the vibe process itself: a claim taken before boot covers the
        window in which no VM is running yet and the slot is still not free.
        """
        path = self.claim_path(branch)
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(f"{os.getpid()}\n")
        try:
            yield
        finally:
            path.unlink(missing_ok=True)

    def claim_pid(self, branch: str) -> int | None:
        try:
            return int(self.claim_path(branch).read_text().strip())
        except (OSError, ValueError):
            return None

    def release_claim(self, branch: str) -> None:
        self.claim_path(branch).unlink(missing_ok=True)
