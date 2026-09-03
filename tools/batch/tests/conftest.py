from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def bogus_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Points `$HOME` at a regular file, so a test that reaches the real home
    -- `DEFAULT_RUN_ROOT`, the staged `~/.claude.json`, the seed image -- gets
    an `OSError` rather than the developer's machine state. `expanduser()`
    never stats the path, so a test that only builds paths is unaffected.
    """
    home = tmp_path / "not-a-home"
    _ = home.write_text("")
    monkeypatch.setenv("HOME", str(home))
    return home
