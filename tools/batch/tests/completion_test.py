from __future__ import annotations

import runpy
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

import batch
from batch.cli import main
from batch.testing.payloads import config_at, no_config_at, outside_a_checkout

_MODULE_PATH = str(Path(batch.__file__).parent / "__main__.py")


def no_vms(monkeypatch: pytest.MonkeyPatch) -> None:
    """`vm status` builds its own runner, so the process-table probe is reachable
    only through the module global that runner resolves at construction."""
    monkeypatch.setattr("batch.vm._running_disks", frozenset)


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    env: Mapping[str, str] | None = None,
) -> int:
    # Click infers the program name from these two; dev/batch runs
    # `python -m batch`, which is what makes an inferred name unusable.
    monkeypatch.setattr(sys, "argv", [_MODULE_PATH, *argv[1:]])
    monkeypatch.setattr(sys.modules["__main__"], "__package__", "batch")
    monkeypatch.delenv("_BATCH_COMPLETE", raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("batch.__main__", run_name="__main__")
    return 0 if caught.value.code is None else int(caught.value.code)


def _completion(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    words: str,
    cword: int,
) -> list[str]:
    code = _run_main(
        monkeypatch,
        ["dev/batch"],
        {
            "_BATCH_COMPLETE": "bash_complete",
            "COMP_WORDS": words,
            "COMP_CWORD": str(cword),
        },
    )
    assert code == 0
    return capsys.readouterr().out.split()


def test_bash_source_emits_a_completion_script(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _ = config_at(monkeypatch, tmp_path / "repo")

    code = _run_main(monkeypatch, ["dev/batch"], {"_BATCH_COMPLETE": "bash_source"})
    out = capsys.readouterr().out
    assert code == 0
    assert "complete -o nosort -F _binacme_completion bin/acme" in out


def test_subcommand_completion_fires(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    completions = _completion(monkeypatch, capsys, "dev/batch qu", 1)

    assert completions == ["plain,queue"]


def test_option_completion_fires(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    completions = _completion(monkeypatch, capsys, "dev/batch run --", 2)

    assert "plain,--watch-interval" in completions
    assert "plain,--cli" in completions


def test_the_usage_line_names_the_configured_wrapper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _ = config_at(monkeypatch, tmp_path / "repo")

    code = _run_main(monkeypatch, ["dev/batch", "--help"])
    out = capsys.readouterr().out

    assert code == 0
    assert out.startswith("Usage: bin/acme [OPTIONS] COMMAND [ARGS]...")
    listed = {line.split()[0] for line in out.splitlines() if line.startswith("  ")}
    assert {"queue", "approve", "plan", "run", "verify"} <= listed


@pytest.mark.parametrize("text", [None, "[commands"], ids=["absent", "malformed"])
def test_an_unreadable_config_leaves_the_generic_name(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    text: str | None,
) -> None:
    root = tmp_path / "repo"
    _ = (
        no_config_at(monkeypatch, root)
        if text is None
        else config_at(monkeypatch, root, text)
    )

    code = _run_main(monkeypatch, ["dev/batch", "--help"])

    assert code == 0
    assert capsys.readouterr().out.startswith("Usage: batch [OPTIONS]")


def test_a_socket_only_command_runs_without_a_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _ = no_config_at(monkeypatch, tmp_path / "repo")
    no_vms(monkeypatch)

    with pytest.raises(SystemExit) as caught:
        main(["vm", "--run-root", str(tmp_path), "status", "1499"])

    assert caught.value.code == 1
    assert "#1499 exited" in capsys.readouterr().out


def test_a_cwd_outside_any_checkout_leaves_the_generic_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    outside_a_checkout(monkeypatch)

    code = _run_main(monkeypatch, ["dev/batch", "--help"])

    assert code == 0
    assert capsys.readouterr().out.startswith("Usage: batch [OPTIONS]")

    no_vms(monkeypatch)

    with pytest.raises(SystemExit) as caught:
        main(["vm", "--run-root", str(tmp_path), "status", "1499"])

    assert caught.value.code == 1
    assert "pass --repo" in capsys.readouterr().err


def test_a_subcommand_dispatches_through_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "argv", ["dev/batch", "--help"])
    _ = config_at(monkeypatch, tmp_path / "repo")
    no_vms(monkeypatch)

    with pytest.raises(SystemExit) as caught:
        main(["vm", "--run-root", str(tmp_path), "status", "1499"])

    assert caught.value.code == 1
    assert "#1499 exited" in capsys.readouterr().out


def _registrations(out: str) -> list[str]:
    return [
        line.strip()
        for line in out.splitlines()
        if line.strip().startswith("complete ")
    ]


def test_bash_source_also_registers_the_console_script_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _ = config_at(monkeypatch, tmp_path / "repo")

    code = _run_main(monkeypatch, ["dev/batch"], {"_BATCH_COMPLETE": "bash_source"})
    out = capsys.readouterr().out

    assert code == 0
    assert _registrations(out) == [
        "complete -o nosort -F _binacme_completion bin/acme",
        "complete -o nosort -F _binacme_completion batch",
    ]


def test_bash_source_registers_the_console_script_name_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _ = no_config_at(monkeypatch, tmp_path / "repo")

    code = _run_main(monkeypatch, ["dev/batch"], {"_BATCH_COMPLETE": "bash_source"})
    out = capsys.readouterr().out

    assert code == 0
    assert _registrations(out) == ["complete -o nosort -F _batch_completion batch"]
