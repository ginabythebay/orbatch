"""Per-invocation lookups every verb shares, cached on the click context.

Their own module rather than `cli`'s, so a command module can resolve the repo
and the config without importing the group that mounts it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import click

from batch.config import BatchConfig, ConfigError, load_config
from batch.stack import main_repo

CONFIG_KEY = "batch.config"
REPO_KEY = "batch.repo"
MAIN_REPO_KEY = "batch.main_repo"


def main_repo_for(ctx: click.Context) -> Path:
    """Cached: `main_repo` shells out, and a poll loop asks once per status."""
    cached = ctx.meta.get(MAIN_REPO_KEY)
    if isinstance(cached, Path):
        return cached
    try:
        found = main_repo(cast("Path | None", ctx.meta.get(REPO_KEY)))
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(
            "Not inside a git checkout; run batch from the repository or pass --repo."
        ) from exc
    ctx.meta[MAIN_REPO_KEY] = found
    return found


def config_for(ctx: click.Context) -> BatchConfig:
    """The repo's `batch.toml`, read once per invocation and cached.

    Cached on `ctx.meta` rather than `ctx.obj`, which the isinstance
    dispatch in the other resolvers already claims for injected fakes.
    """
    cached = ctx.meta.get(CONFIG_KEY)
    if isinstance(cached, BatchConfig):
        return cached
    try:
        config = load_config(main_repo_for(ctx))
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    ctx.meta[CONFIG_KEY] = config
    return config
