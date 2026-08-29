"""The user's `snippets.toml`: which checkouts a rollup covers.

The set of repos someone writes snippets about belongs to the person,
not to any one checkout, so this config lives under the user's config
directory rather than in a repo. With no file, snippets reports on the
surrounding checkout alone.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

CONFIG_RELATIVE = Path("orbatch") / "snippets.toml"


class ConfigError(RuntimeError):
    def __init__(self, path: Path, problems: Sequence[str]) -> None:
        self.problems: tuple[str, ...] = tuple(problems)
        detail = "\n".join(f"  - {problem}" for problem in problems)
        super().__init__(f"{path} is invalid:\n{detail}")


@dataclass(frozen=True)
class RepoSpec:
    """One checkout to report on. `deploy_tag` is a Grafana annotation tag."""

    path: Path
    deploy_tag: str | None = None


def config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / CONFIG_RELATIVE


def load_repos(path: Path) -> tuple[RepoSpec, ...] | None:
    """The configured checkouts, or None when the user has no config file."""
    if not path.is_file():
        return None
    return _parse(path, path.read_text())


def _parse(path: Path, text: str) -> tuple[RepoSpec, ...]:
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(path, [f"could not be parsed: {exc}"]) from exc

    raw = document.get("repo")
    if raw is not None and not isinstance(raw, list):
        raise ConfigError(path, ['"repo" must be a list of [[repo]] tables'])
    if not raw:
        raise ConfigError(path, ["has no [[repo]] entries"])

    problems: list[str] = []
    specs: list[RepoSpec] = []
    for index, entry in enumerate(cast("list[object]", raw)):
        spec = _parse_repo(index, entry, problems)
        if spec is not None:
            specs.append(spec)
    if problems:
        raise ConfigError(path, problems)
    return tuple(specs)


def _parse_repo(index: int, raw: object, problems: list[str]) -> RepoSpec | None:
    label = f"repo #{index + 1}"
    if not isinstance(raw, dict):
        problems.append(f"{label} must be a [[repo]] table")
        return None
    entry = cast("dict[str, object]", raw)
    unknown = sorted(set(entry) - {"path", "deploy_tag"})
    for key in unknown:
        problems.append(f'{label} has an unknown key "{key}"')
    path = entry.get("path")
    if not isinstance(path, str) or not path.strip():
        problems.append(f'{label} needs a non-empty "path"')
        return None
    tag = entry.get("deploy_tag")
    if tag is not None and (not isinstance(tag, str) or not tag.strip()):
        problems.append(f'{label} has an empty "deploy_tag"')
        return None
    return RepoSpec(
        path=Path(path).expanduser(),
        deploy_tag=tag if isinstance(tag, str) else None,
    )
