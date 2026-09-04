from __future__ import annotations

import click
import pytest

from shellcomp.completion import source_with_alias

_COMPLETE_VAR = "_WIDGET_COMPLETE"


@click.group()
def cli() -> None:
    pass


def _complete_lines(source: str) -> list[str]:
    return [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith("complete ")
    ]


def test_bash_source_registers_the_alias_alongside_the_prog_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_COMPLETE_VAR, "bash_source")

    source = source_with_alias(cli, "widget", _COMPLETE_VAR, "dev-widget")

    assert source is not None
    assert f"{_COMPLETE_VAR}=bash_complete" in source
    assert [line.split()[-1] for line in _complete_lines(source)] == [
        "widget",
        "dev-widget",
    ]


def test_an_alias_equal_to_the_prog_name_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_COMPLETE_VAR, "bash_source")

    assert source_with_alias(cli, "widget", _COMPLETE_VAR, "widget") is None


@pytest.mark.parametrize("instruction", ["bash_complete", "zsh_source", "zsh_complete"])
def test_any_other_instruction_is_left_to_click(
    monkeypatch: pytest.MonkeyPatch, instruction: str
) -> None:
    monkeypatch.setenv(_COMPLETE_VAR, instruction)

    assert source_with_alias(cli, "widget", _COMPLETE_VAR, "dev-widget") is None


def test_an_unset_complete_var_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_COMPLETE_VAR, raising=False)

    assert source_with_alias(cli, "widget", _COMPLETE_VAR, "dev-widget") is None


def test_both_registrations_name_the_same_completion_function(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_COMPLETE_VAR, "bash_source")

    source = source_with_alias(cli, "widget", _COMPLETE_VAR, "dev-widget")

    assert source is not None
    functions = {line.split()[-2] for line in _complete_lines(source)}
    assert len(functions) == 1
    assert f"{functions.pop()}()" in source
