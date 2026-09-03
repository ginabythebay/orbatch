from __future__ import annotations

import pytest

from batch.models import (
    BatchLabel,
    ConflictingLabelsError,
    RecoveryAction,
    RecoveryRefusal,
)
from batch.recovery import Recovery
from batch.testing.payloads import (
    EPIC,
    FakeVms,
    body_writes,
    child,
    children,
    issue,
    label_ids,
    label_writes,
    state_over,
    transport,
)
from batch.text_output import recovery_line
from ghgql.fake import FakeTransport


def recovery(fake: FakeTransport, live: tuple[int, ...] = ()) -> Recovery:
    return Recovery(state_over(fake), FakeVms(live))


def cleared(label: str) -> FakeTransport:
    node = issue(child(10, labels=[label]))
    return transport(node, node, label_ids(), {})


def closed_and_cleared(label: str, *, merged: bool = False) -> FakeTransport:
    node = issue(
        child(
            1503,
            state="CLOSED",
            labels=[label],
            closing_prs=(merged,),
        )
    )
    return transport(node, node, label_ids(), {})


class TestSkip:
    def test_a_stuck_issue_loses_its_batch_label(self) -> None:
        fake = cleared("stuck")

        result = recovery(fake).skip(10)

        assert result.refusal is None
        assert result.action is RecoveryAction.SKIP
        assert result.found is BatchLabel.STUCK
        assert label_writes(fake) == [("remove", "I_10", "LA_stuck")]

    def test_a_restarted_batch_ignores_the_skipped_issue(self) -> None:
        node = issue(child(10, labels=["stuck"]))
        fake = transport(
            node,
            node,
            label_ids(),
            {},
            children(child(10), child(11, labels=["planned"])),
        )
        state = state_over(fake)

        _ = Recovery(state, FakeVms()).skip(10)

        assert [issue.number for issue in state.batch((EPIC,)).issues] == [11]

    @pytest.mark.parametrize("label", ["planned", "queued"])
    def test_an_unstarted_issue_is_skippable_and_keeps_its_body(
        self, label: str
    ) -> None:
        fake = cleared(label)

        result = recovery(fake).skip(10)

        assert result.refusal is None
        assert label_writes(fake) == [("remove", "I_10", f"LA_{label}")]
        assert body_writes(fake) == []

    def test_ready_for_review_is_refused_with_the_label_left_alone(self) -> None:
        fake = transport(issue(child(10, labels=["ready-for-review"])))

        result = recovery(fake).skip(10)

        assert result.refusal is RecoveryRefusal.WRONG_STATE
        assert result.found is BatchLabel.READY_FOR_REVIEW
        assert label_writes(fake) == []

    def test_implementing_is_refused_while_its_vm_is_live(self) -> None:
        fake = transport(issue(child(10, labels=["implementing"])))

        result = recovery(fake, live=(10,)).skip(10)

        assert result.refusal is RecoveryRefusal.VM_LIVE
        assert label_writes(fake) == []

    def test_a_stuck_issue_is_refused_while_its_timed_out_vm_runs_on(self) -> None:
        fake = transport(issue(child(10, labels=["stuck"])))

        result = recovery(fake, live=(10,)).skip(10)

        assert result.refusal is RecoveryRefusal.VM_LIVE
        assert label_writes(fake) == []

    def test_an_orphaned_implementing_issue_is_skippable(self) -> None:
        fake = cleared("implementing")

        result = recovery(fake).skip(10)

        assert result.refusal is None
        assert label_writes(fake) == [("remove", "I_10", "LA_implementing")]

    def test_an_issue_outside_the_batch_is_refused(self) -> None:
        fake = transport(issue(child(10)))

        result = recovery(fake).skip(10)

        assert result.refusal is RecoveryRefusal.NOT_IN_BATCH
        assert result.found is None
        assert label_writes(fake) == []

    def test_a_closed_unmerged_issue_loses_its_label(self) -> None:
        fake = closed_and_cleared("planned")

        result = recovery(fake).skip(1503)

        assert result.refusal is None
        assert result.found is BatchLabel.PLANNED
        assert label_writes(fake) == [("remove", "I_1503", "LA_planned")]

    def test_a_closed_unmerged_issue_bypasses_the_skippable_labels(self) -> None:
        fake = closed_and_cleared("ready-for-review")

        result = recovery(fake).skip(1503)

        assert result.refusal is None
        assert result.found is BatchLabel.READY_FOR_REVIEW
        assert label_writes(fake) == [("remove", "I_1503", "LA_readyForReview")]

    @pytest.mark.parametrize("label", ["ready-for-review", "planned"])
    def test_a_merged_issue_keeps_the_label_teardown_reads(self, label: str) -> None:
        fake = closed_and_cleared(label, merged=True)

        result = recovery(fake).skip(1503)

        assert result.refusal is RecoveryRefusal.MERGED
        assert label_writes(fake) == []
        assert (
            recovery_line(result, prog="bin/acme")
            == "Cannot skip #1503: it merged — run `bin/acme cleanup <target>`"
        )

    def test_a_two_label_closed_issue_cannot_be_skipped_at_all(self) -> None:
        fake = transport(
            issue(child(1503, state="CLOSED", labels=["implementing", "stuck"]))
        )

        with pytest.raises(ConflictingLabelsError):
            _ = recovery(fake).skip(1503)

        assert label_writes(fake) == []

    def test_a_closed_issue_is_still_refused_while_its_vm_is_live(self) -> None:
        fake = closed_and_cleared("implementing")

        result = recovery(fake, live=(1503,)).skip(1503)

        assert result.refusal is RecoveryRefusal.VM_LIVE
        assert label_writes(fake) == []

    def test_a_closed_stranger_is_outside_the_batch_not_an_anomaly(self) -> None:
        fake = transport(issue(child(1503, state="CLOSED", closing_prs=(True,))))

        result = recovery(fake).skip(1503)

        assert result.refusal is RecoveryRefusal.NOT_IN_BATCH
        assert label_writes(fake) == []


class TestRelaunch:
    def test_a_stuck_issue_goes_back_to_planned(self) -> None:
        node = issue(child(10, labels=["stuck"]))
        fake = transport(node, node, label_ids(), {}, {})

        result = recovery(fake).relaunch(10)

        assert result.refusal is None
        assert result.action is RecoveryAction.RELAUNCH
        assert label_writes(fake) == [
            ("add", "I_10", "LA_planned"),
            ("remove", "I_10", "LA_stuck"),
        ]

    def test_a_live_vm_refuses_the_relaunch(self) -> None:
        fake = transport(issue(child(10, labels=["stuck"])))

        result = recovery(fake, live=(10,)).relaunch(10)

        assert result.refusal is RecoveryRefusal.VM_LIVE
        assert label_writes(fake) == []

    @pytest.mark.parametrize(
        "label", ["queued", "planned", "implementing", "ready-for-review"]
    )
    def test_every_state_other_than_stuck_is_refused(self, label: str) -> None:
        fake = transport(issue(child(10, labels=[label])))

        result = recovery(fake).relaunch(10)

        assert result.refusal is RecoveryRefusal.WRONG_STATE
        assert result.found is BatchLabel(label)
        assert label_writes(fake) == []

    def test_an_issue_outside_the_batch_is_refused(self) -> None:
        fake = transport(issue(child(10)))

        result = recovery(fake).relaunch(10)

        assert result.refusal is RecoveryRefusal.NOT_IN_BATCH
        assert label_writes(fake) == []

    def test_a_closed_issue_is_refused_and_says_so(self) -> None:
        fake = transport(issue(child(1503, state="CLOSED", labels=["stuck"])))

        result = recovery(fake).relaunch(1503)

        assert result.refusal is RecoveryRefusal.CLOSED
        assert label_writes(fake) == []
        assert (
            recovery_line(result, prog="bin/acme")
            == "Cannot relaunch #1503: it is closed"
        )

    def test_a_closed_planned_issue_is_refused_for_being_closed(self) -> None:
        fake = transport(issue(child(1503, state="CLOSED", labels=["planned"])))

        result = recovery(fake).relaunch(1503)

        assert result.refusal is RecoveryRefusal.CLOSED
        assert label_writes(fake) == []

    def test_a_merged_issue_is_refused_for_being_closed_not_for_merging(self) -> None:
        fake = transport(
            issue(child(1503, state="CLOSED", labels=["stuck"], closing_prs=(True,)))
        )

        result = recovery(fake).relaunch(1503)

        assert result.refusal is RecoveryRefusal.CLOSED
        assert label_writes(fake) == []
