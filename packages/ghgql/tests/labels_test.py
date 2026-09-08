from __future__ import annotations

import pytest

from ghgql.labels import CONFLICT, NO_LABEL, BatchLabel, batch_labels, glyph


class TestBatchLabels:
    def test_selects_only_batch_labels(self) -> None:
        assert batch_labels(("epic", "queued", "soon")) == [BatchLabel.QUEUED]


class TestGlyph:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("queued", "q"),
            ("planned", "p"),
            ("implementing", "i"),
            ("ready-for-review", "r"),
            ("stuck", "s"),
        ],
    )
    def test_each_label_maps_to_its_glyph(self, name: str, expected: str) -> None:
        assert glyph((name,)) == expected

    @pytest.mark.parametrize("names", [(), ("epic", "soon")])
    def test_no_batch_label_yields_one_space(self, names: tuple[str, ...]) -> None:
        assert glyph(names) == " "

    def test_two_batch_labels_yield_a_bang(self) -> None:
        assert glyph(("queued", "stuck")) == "!"

    def test_every_member_has_a_one_character_glyph(self) -> None:
        glyphs = {label: glyph((label.value,)) for label in BatchLabel}
        assert all(len(g) == 1 for g in glyphs.values())
        assert len(set(glyphs.values())) == len(BatchLabel)
        assert NO_LABEL not in glyphs.values()
        assert CONFLICT not in glyphs.values()
