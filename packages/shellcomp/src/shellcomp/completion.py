from __future__ import annotations

import os

import click
from click.shell_completion import get_completion_class

_SOURCE = "_source"
_SHELL = "bash"


def source_with_alias(
    cli: click.Command, prog_name: str, complete_var: str, alias: str
) -> str | None:
    """The bash completion script with `alias` registered alongside `prog_name`,
    or None when click's own handling already covers the request."""
    instruction = os.environ.get(complete_var, "")
    if instruction != f"{_SHELL}{_SOURCE}" or alias == prog_name:
        return None
    comp = get_completion_class(_SHELL)(cli, {}, prog_name, complete_var)
    return f"{comp.source()}complete -o nosort -F {comp.func_name} {alias}\n"
