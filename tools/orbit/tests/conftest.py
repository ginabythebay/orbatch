from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def bogus_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Points `$HOME` at a regular file and git at no config, so a test that
    reaches the real home or the developer's gitconfig fails here rather than
    behaving differently on CI."""
    home = tmp_path / "not-a-home"
    _ = home.write_text("")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "no-gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(tmp_path / "no-gitconfig"))
    return home
