from __future__ import annotations

import runpy
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

import orbit
from orbit.cli import main

_MODULE_PATH = str(Path(orbit.__file__).parent / "__main__.py")


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    env: Mapping[str, str] | None = None,
) -> int:
    # Click infers the program name from these two; the console script runs
    # `python -m orbit`, which is what makes an inferred name unusable.
    monkeypatch.setattr(sys, "argv", [_MODULE_PATH, *argv[1:]])
    monkeypatch.setattr(sys.modules["__main__"], "__package__", "orbit")
    monkeypatch.delenv("_ORBIT_COMPLETE", raising=False)
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("orbit.__main__", run_name="__main__")
    return 0 if caught.value.code is None else int(caught.value.code)


def test_bash_source_emits_a_completion_script(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run_main(monkeypatch, ["orbit"], {"_ORBIT_COMPLETE": "bash_source"})
    out = capsys.readouterr().out
    assert code == 0
    assert "complete -o nosort -F _orbit_completion orbit" in out


def test_subcommand_completion_fires(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run_main(
        monkeypatch,
        ["orbit"],
        {
            "_ORBIT_COMPLETE": "bash_complete",
            "COMP_WORDS": "orbit spr",
            "COMP_CWORD": "1",
        },
    )

    assert code == 0
    assert capsys.readouterr().out.split() == ["plain,sprint"]


def test_help_is_unchanged_without_a_completion_var(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run_main(monkeypatch, ["orbit", "--help"])
    out = capsys.readouterr().out

    assert code == 0
    assert out.startswith("Usage: orbit [OPTIONS] [COMMAND] [ARGS]...")
    listed = {line.split()[0] for line in out.splitlines() if line.startswith("  ")}
    assert {"sprint", "create", "create-epic", "move", "schedule"} <= listed


def test_main_parses_its_argument_not_sys_argv(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["orbit", "--help"])

    with pytest.raises(SystemExit) as caught:
        main(["--no-such-option"])

    assert caught.value.code == 2
    assert "No such option '--no-such-option'" in capsys.readouterr().err


def test_bash_source_registers_the_console_script_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code = _run_main(monkeypatch, ["orbit"], {"_ORBIT_COMPLETE": "bash_source"})
    out = capsys.readouterr().out
    registrations = [
        line.strip()
        for line in out.splitlines()
        if line.strip().startswith("complete ")
    ]

    assert code == 0
    assert registrations == ["complete -o nosort -F _orbit_completion orbit"]
