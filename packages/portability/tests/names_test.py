from __future__ import annotations

from pathlib import Path

import pytest

from portability import names
from portability.names import DIGESTS, NAMES, forbidden_words

_PUBLISHED = frozenset(
    {
        "e1bec087e777e94ae726031ad4c17efc14911bbe08d6a3d75c2e33215f50e41c",
        "61680150219e784f81be0014be0564d74068e494e9c83a8761c42413d98aa027",
    }
)

_PLAIN = next(name for name in NAMES if "-" not in name)


class TestNames:
    def test_the_stored_digests_are_the_published_ones(self) -> None:
        assert DIGESTS == _PUBLISHED

    def test_the_declaring_module_needs_no_self_exemption(self) -> None:
        source = Path(names.__file__)

        assert forbidden_words(source.read_text()) == set()

    @pytest.mark.parametrize("name", NAMES, ids=("plain", "hyphenated"))
    def test_a_planted_name_is_found(self, tmp_path: Path, name: str) -> None:
        planted = tmp_path / "fixture.py"
        _ = planted.write_text(f'REPO = "{name}"\n')

        assert forbidden_words(planted.read_text()) == {name}

    def test_a_name_embedded_in_a_path_is_found(self, tmp_path: Path) -> None:
        planted = tmp_path / "config.md"
        _ = planted.write_text(f'path = "~/Source/example/{_PLAIN}"\n')

        assert forbidden_words(planted.read_text()) == {_PLAIN}

    def test_a_clean_text_reports_nothing(self) -> None:
        assert forbidden_words('path = "~/Source/example/widget"\n') == set()
