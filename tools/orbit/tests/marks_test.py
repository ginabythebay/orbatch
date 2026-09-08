from __future__ import annotations

from orbit.marks import Marks


class TestMarks:
    def test_toggle_marks_then_unmarks_the_same_number(self) -> None:
        marks = Marks()

        marks.toggle(42)
        assert 42 in marks

        marks.toggle(42)
        assert 42 not in marks

    def test_unmark_on_an_unmarked_number_is_a_no_op(self) -> None:
        marks = Marks()
        marks.toggle(7)

        marks.unmark(42)

        assert 7 in marks
        assert 42 not in marks

    def test_clear_empties_and_count_tracks_every_operation(self) -> None:
        marks = Marks()
        assert marks.count == 0

        marks.toggle(1)
        marks.toggle(2)
        assert marks.count == 2

        marks.unmark(1)
        assert marks.count == 1

        marks.clear()
        assert marks.count == 0
        assert 2 not in marks

    def test_numbers_come_back_in_marking_order(self) -> None:
        marks = Marks()

        marks.toggle(30)
        marks.toggle(10)
        marks.toggle(20)
        marks.toggle(10)
        marks.toggle(10)

        assert marks.numbers == (30, 20, 10)
