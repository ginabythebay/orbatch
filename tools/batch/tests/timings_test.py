from __future__ import annotations

from batch.testing.payloads import FakeClock
from batch.timings import Timings


class TestTimings:
    def test_an_unknown_issue_has_no_elapsed(self) -> None:
        assert Timings(FakeClock().monotonic).elapsed(10) is None

    def test_a_started_issue_counts_up(self) -> None:
        clock = FakeClock()
        timings = Timings(clock.monotonic)

        timings.start(10)
        clock.sleep(65.0)

        assert timings.elapsed(10) == 65.0

    def test_a_finished_issue_freezes_at_its_total(self) -> None:
        clock = FakeClock()
        timings = Timings(clock.monotonic)

        timings.start(10)
        clock.sleep(65.0)
        timings.finish(10)
        clock.sleep(600.0)

        assert timings.elapsed(10) == 65.0

    def test_finishing_an_issue_that_never_started_records_nothing(self) -> None:
        timings = Timings(FakeClock().monotonic)

        timings.finish(10)

        assert timings.elapsed(10) is None

    def test_a_restarted_issue_counts_from_its_new_start(self) -> None:
        clock = FakeClock()
        timings = Timings(clock.monotonic)

        timings.start(10)
        clock.sleep(65.0)
        timings.finish(10)
        clock.sleep(10.0)
        timings.start(10)
        clock.sleep(5.0)

        assert timings.elapsed(10) == 5.0
