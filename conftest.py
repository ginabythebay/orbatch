import os
from typing import cast

import pytest

from orbit.core import NO_BROWSER_VAR

pytest_plugins = ("pytest_asyncio",)

# Set before collection so subprocesses spawned by tests inherit it: no test
# run opens a browser, on any platform. See orbit.core.open_url.
os.environ.setdefault(NO_BROWSER_VAR, "1")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--slow",
        action="store_true",
        help="include slow tests (always included on CI, with -m/-k, or explicit paths)",
    )


def _markexpr_passed(config: pytest.Config) -> bool:
    return any(
        (arg.startswith("-m") and not arg.startswith("--"))
        or arg.startswith("--markexpr")
        for arg in config.invocation_params.args
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    if (
        os.environ.get("CI")
        or cast(bool, config.getoption("--slow"))
        or _markexpr_passed(config)
        or cast(str, config.getoption("keyword"))
        or cast(list[str], config.getoption("file_or_dir"))
    ):
        return
    deselected = [item for item in items if item.get_closest_marker("slow")]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if not item.get_closest_marker("slow")]
