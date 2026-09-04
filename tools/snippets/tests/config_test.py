from __future__ import annotations

from pathlib import Path

import pytest

from snippets.config import ConfigError, RepoSpec, config_path, load_repos


def _written(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "snippets.toml"
    _ = path.write_text(text)
    return path


class TestLoadRepos:
    def test_absent_config_is_not_an_error(self, tmp_path: Path) -> None:
        assert load_repos(tmp_path / "nothing.toml") is None

    def test_reads_paths_and_optional_deploy_tags(self, tmp_path: Path) -> None:
        path = _written(
            tmp_path,
            """
            [[repo]]
            path = "/src/orbatch"

            [[repo]]
            path = "/src/widget"
            deploy_tag = "deploy"
            """,
        )
        assert load_repos(path) == (
            RepoSpec(path=Path("/src/orbatch")),
            RepoSpec(path=Path("/src/widget"), deploy_tag="deploy"),
        )

    def test_expands_a_home_relative_path(self, tmp_path: Path) -> None:
        path = _written(tmp_path, '[[repo]]\npath = "~/Source/widget"\n')
        specs = load_repos(path)
        assert specs is not None
        (spec,) = specs
        assert not str(spec.path).startswith("~")
        assert spec.path.name == "widget"

    def test_reports_every_problem_at_once(self, tmp_path: Path) -> None:
        path = _written(
            tmp_path,
            '[[repo]]\nname = "orbatch"\n\n[[repo]]\npath = ""\n',
        )
        with pytest.raises(ConfigError) as info:
            _ = load_repos(path)
        assert info.value.problems == (
            'repo #1 has an unknown key "name"',
            'repo #1 needs a non-empty "path"',
            'repo #2 needs a non-empty "path"',
        )

    def test_an_unknown_key_is_a_problem(self, tmp_path: Path) -> None:
        path = _written(tmp_path, '[[repo]]\npath = "/src/widget"\ndeploy = "yes"\n')
        with pytest.raises(ConfigError, match='unknown key "deploy"'):
            _ = load_repos(path)

    def test_an_empty_deploy_tag_is_a_problem(self, tmp_path: Path) -> None:
        path = _written(tmp_path, '[[repo]]\npath = "/src/widget"\ndeploy_tag = ""\n')
        with pytest.raises(ConfigError, match='empty "deploy_tag"'):
            _ = load_repos(path)

    @pytest.mark.parametrize("text", ["# nothing here\n", "repo = []\n"])
    def test_a_config_naming_no_repos_says_so(self, tmp_path: Path, text: str) -> None:
        with pytest.raises(ConfigError, match=r"no \[\[repo\]\] entries"):
            _ = load_repos(_written(tmp_path, text))

    def test_a_non_list_repo_key_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"must be a list of \[\[repo\]\] tables"):
            _ = load_repos(_written(tmp_path, 'repo = "/src/widget"\n'))

    def test_a_non_table_entry_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match=r"repo #1 must be a \[\[repo\]\] table"):
            _ = load_repos(_written(tmp_path, "repo = [1]\n"))

    def test_unparseable_toml_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="could not be parsed"):
            _ = load_repos(_written(tmp_path, "[[repo]\n"))


class TestConfigPath:
    def test_honours_xdg_config_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("XDG_CONFIG_HOME", "/somewhere/config")
        assert config_path() == Path("/somewhere/config/orbatch/snippets.toml")

    def test_falls_back_to_dot_config_under_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert config_path() == tmp_path / ".config" / "orbatch" / "snippets.toml"
