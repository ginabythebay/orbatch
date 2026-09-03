from __future__ import annotations

import pytest

from batch.agent import PlanningAgent
from batch.github.client import TARGETS_PER_QUERY
from batch.models import (
    BatchLabel,
    ConflictingLabelsError,
    NotAChildError,
    NoTargetsError,
)
from batch.order import MAIN, base_for, base_under
from batch.polling import SettledTargets
from batch.testing.payloads import (
    EPIC,
    body_writes,
    child,
    children,
    epic,
    fetches,
    issue,
    label_ids,
    label_writes,
    missing,
    standalone,
    state,
    state_over,
    target,
    targets,
    transport,
)
from ghgql.errors import IssueNotFoundError
from ghgql.fake import Errors, Response


class TestBatch:
    def test_labeled_children_keep_sub_issue_order(self) -> None:
        response = children(
            child(70, labels=["queued"], title="Third by number"),
            child(12, labels=["planned"], title="First by number"),
            child(40, labels=["stuck"], title="Second by number"),
        )
        batch = state(response).batch((EPIC,))

        assert [issue.number for issue in batch.issues] == [70, 12, 40]
        assert [issue.state for issue in batch.issues] == [
            BatchLabel.QUEUED,
            BatchLabel.PLANNED,
            BatchLabel.STUCK,
        ]
        assert batch.issues[0].title == "Third by number"

    def test_unlabeled_children_are_excluded(self) -> None:
        response = children(
            child(1, labels=["queued"]),
            child(2),
            child(3, labels=["planned"]),
        )
        batch = state(response).batch((EPIC,))

        assert [issue.number for issue in batch.issues] == [1, 3]

    def test_closed_children_are_excluded(self) -> None:
        response = children(
            child(1, labels=["queued"]),
            child(2, state="CLOSED", labels=["ready-for-review"]),
        )
        batch = state(response).batch((EPIC,))

        assert [issue.number for issue in batch.issues] == [1]

    def test_two_batch_labels_on_one_child_is_an_error(self) -> None:
        response = children(child(7, labels=["implementing", "stuck"]))

        with pytest.raises(ConflictingLabelsError) as exc_info:
            state(response).batch((EPIC,))

        assert exc_info.value.number == 7
        assert "implementing" in str(exc_info.value)
        assert "stuck" in str(exc_info.value)

    def test_non_batch_labels_are_ignored(self) -> None:
        response = children(
            child(1, labels=["soon", "found in review"]),
            child(2, labels=["epic", "planned"]),
        )
        batch = state(response).batch((EPIC,))

        assert [issue.number for issue in batch.issues] == [2]
        assert batch.issues[0].state == BatchLabel.PLANNED

    def test_an_epic_with_no_labeled_children_is_an_empty_batch(self) -> None:
        batch = state(children(child(1), child(2))).batch((EPIC,))

        assert batch.issues == ()
        assert batch.targets == (EPIC,)

    def test_a_null_target_surfaces_issue_not_found(self) -> None:
        with pytest.raises(IssueNotFoundError):
            state(missing(9999)).batch((9999,))

    def test_a_target_github_refuses_names_the_number_it_refused(self) -> None:
        errors = Errors(
            [
                {
                    "type": "NOT_FOUND",
                    "message": "Could not resolve to an issue with the number of 9999.",
                }
            ]
        )

        with pytest.raises(RuntimeError, match="9999"):
            _ = state(errors).batch((9999,))


class TestDroppedChildren:
    def test_an_unlabeled_open_child_is_dropped_with_its_reason(self) -> None:
        response = children(child(1, labels=["queued"]), child(2))

        batch = state(response).batch((EPIC,))

        assert [issue.number for issue in batch.issues] == [1]
        assert [(drop.number, drop.reason) for drop in batch.dropped] == [
            (2, "no batch label")
        ]

    def test_a_closed_child_keeps_the_label_it_still_carries(self) -> None:
        response = children(child(1503, state="CLOSED", labels=["stuck"]))

        batch = state(response).batch((EPIC,))

        assert batch.issues == ()
        assert [(drop.number, drop.reason) for drop in batch.dropped] == [
            (1503, "closed, labelled stuck")
        ]
        assert batch.dropped[0].labels == (BatchLabel.STUCK,)

    def test_a_closed_unlabeled_child_reports_plain_closed(self) -> None:
        response = children(child(9, state="CLOSED"))

        batch = state(response).batch((EPIC,))

        assert [(drop.number, drop.reason) for drop in batch.dropped] == [(9, "closed")]
        assert batch.dropped[0].labels == ()

    def test_dropped_children_keep_sub_issue_order_and_titles(self) -> None:
        response = children(
            child(70, title="Third by number"),
            child(12, state="CLOSED", title="First by number"),
            child(40, labels=["planned"], title="Second by number"),
        )

        batch = state(response).batch((EPIC,))

        assert [(drop.number, drop.title) for drop in batch.dropped] == [
            (70, "Third by number"),
            (12, "First by number"),
        ]

    def test_a_closed_child_with_two_batch_labels_is_dropped_not_an_error(self) -> None:
        response = children(child(7, state="CLOSED", labels=["implementing", "stuck"]))

        batch = state(response).batch((EPIC,))

        assert batch.dropped[0].labels == (BatchLabel.IMPLEMENTING, BatchLabel.STUCK)
        assert batch.dropped[0].reason == "closed, labelled implementing, stuck"

    def test_a_merged_and_closed_labeled_child_is_dropped_but_not_an_anomaly(
        self,
    ) -> None:
        response = children(
            child(
                1600,
                state="CLOSED",
                labels=["ready-for-review"],
                closing_prs=[True],
            )
        )

        batch = state(response).batch((EPIC,))

        assert [drop.number for drop in batch.dropped] == [1600]
        assert batch.anomalies == ()

    def test_a_hand_closed_labeled_child_is_still_an_anomaly(self) -> None:
        response = children(child(1600, state="CLOSED", labels=["ready-for-review"]))

        batch = state(response).batch((EPIC,))

        assert [drop.number for drop in batch.anomalies] == [1600]

    def test_a_closed_planned_child_with_no_pull_request_is_still_an_anomaly(
        self,
    ) -> None:
        response = children(child(1600, state="CLOSED", labels=["planned"]))

        batch = state(response).batch((EPIC,))

        assert [drop.number for drop in batch.anomalies] == [1600]

    def test_a_closed_child_whose_linked_pull_requests_never_merged_is_an_anomaly(
        self,
    ) -> None:
        response = children(
            child(
                1600,
                state="CLOSED",
                labels=["ready-for-review"],
                closing_prs=[False, False],
            )
        )

        batch = state(response).batch((EPIC,))

        assert [drop.number for drop in batch.anomalies] == [1600]

    def test_an_open_child_with_a_merged_pull_request_is_still_a_batch_issue(
        self,
    ) -> None:
        response = children(
            child(1600, labels=["ready-for-review"], closing_prs=[True])
        )

        batch = state(response).batch((EPIC,))

        assert [issue.number for issue in batch.issues] == [1600]
        assert batch.dropped == ()

    def test_a_merged_child_with_two_batch_labels_is_still_an_anomaly(self) -> None:
        response = children(
            child(
                1600,
                state="CLOSED",
                labels=["implementing", "ready-for-review"],
                closing_prs=[True],
            )
        )

        batch = state(response).batch((EPIC,))

        assert [drop.number for drop in batch.anomalies] == [1600]

    def test_an_all_eligible_epic_drops_nothing(self) -> None:
        response = children(child(1, labels=["queued"]), child(2, labels=["planned"]))

        batch = state(response).batch((EPIC,))

        assert batch.dropped == ()
        assert [issue.number for issue in batch.issues] == [1, 2]


class TestSetState:
    @pytest.mark.parametrize(
        ("label", "label_id"),
        [
            (BatchLabel.QUEUED, "LA_queued"),
            (BatchLabel.PLANNED, "LA_planned"),
            (BatchLabel.IMPLEMENTING, "LA_implementing"),
            (BatchLabel.READY_FOR_REVIEW, "LA_readyForReview"),
            (BatchLabel.STUCK, "LA_stuck"),
        ],
    )
    def test_a_transition_adds_the_new_label_then_removes_the_old(
        self, label: BatchLabel, label_id: str
    ) -> None:
        start = BatchLabel.STUCK if label is BatchLabel.QUEUED else BatchLabel.QUEUED
        fake = transport(
            issue(child(42, labels=[start.value])),
            label_ids(),
            {},
            {},
        )

        state_over(fake).set_state(42, label)

        assert label_writes(fake) == [
            ("add", "I_42", label_id),
            (
                "remove",
                "I_42",
                f"LA_{'stuck' if start is BatchLabel.STUCK else 'queued'}",
            ),
        ]

    def test_setting_the_state_an_issue_already_holds_writes_nothing(self) -> None:
        fake = transport(issue(child(42, labels=["planned"])))

        state_over(fake).set_state(42, BatchLabel.PLANNED)

        assert label_writes(fake) == []


class TestTargetResolution:
    def test_a_standalone_issue_resolves_to_a_batch_of_itself(self) -> None:
        fake = transport(standalone(child(1769)), label_ids(), {})

        result = state_over(fake).queue(targets=(1769,))

        assert result.labeled == (1769,)
        assert result.epic is None
        assert label_writes(fake) == [("add", "I_1769", "LA_queued")]

    def test_an_epic_expands_to_its_children_in_sub_issue_order(self) -> None:
        fake = transport(
            children(child(70), child(12), child(40)), label_ids(), {}, {}, {}
        )

        result = state_over(fake).queue(targets=(EPIC,))

        assert result.labeled == (70, 12, 40)

    def test_epics_and_standalone_issues_concatenate_in_argument_order(self) -> None:
        fake = transport(
            targets(epic(child(70), child(12)), target(child(1769))),
            label_ids(),
            {},
            {},
            {},
        )

        result = state_over(fake).queue(targets=(EPIC, 1769))

        assert result.labeled == (70, 12, 1769)
        assert fetches(fake) == 1

    def test_an_issue_reachable_twice_keeps_its_earliest_position(self) -> None:
        fake = transport(
            targets(target(child(12)), epic(child(70), child(12))),
            label_ids(),
            {},
            {},
        )

        result = state_over(fake).queue(targets=(12, EPIC))

        assert result.labeled == (12, 70)

    def test_an_epic_with_no_eligible_children_contributes_nothing(self) -> None:
        fake = transport(
            targets(epic(child(70, labels=["planned"])), target(child(1769))),
            label_ids(),
            {},
        )

        result = state_over(fake).queue(targets=(EPIC, 1769))

        assert result.labeled == (1769,)
        assert [skip.number for skip in result.skipped] == [70]

    def test_targets_from_two_epics_need_no_shared_parent(self) -> None:
        fake = transport(standalone(child(1601), child(1700)), label_ids(), {}, {})

        result = state_over(fake).queue(targets=(1601, 1700))

        assert result.labeled == (1601, 1700)

    def test_a_number_that_resolves_to_nothing_fails_before_writing(self) -> None:
        fake = transport({"repository": {"t1601": target(child(1601)), "t9999": None}})

        with pytest.raises(IssueNotFoundError, match="9999"):
            _ = state_over(fake).queue(targets=(1601, 9999))

        assert label_writes(fake) == []

    def test_more_targets_than_fit_one_query_are_fetched_in_chunks(self) -> None:
        numbers = list(range(1601, 1601 + TARGETS_PER_QUERY + 2))
        nodes = [target(child(number)) for number in numbers]
        writes: list[Response] = [{} for _ in numbers]
        fake = transport(
            targets(*nodes[:TARGETS_PER_QUERY]),
            targets(*nodes[TARGETS_PER_QUERY:]),
            label_ids(),
            *writes,
        )

        result = state_over(fake).queue(targets=numbers)

        assert result.labeled == tuple(numbers)
        assert fetches(fake) == 2

    def test_a_conflicted_named_issue_aborts_before_writing(self) -> None:
        fake = transport(
            standalone(child(1601), child(1602, labels=["queued", "stuck"]))
        )

        with pytest.raises(ConflictingLabelsError) as exc_info:
            _ = state_over(fake).queue(targets=(1601, 1602))

        assert exc_info.value.number == 1602
        assert label_writes(fake) == []

    def test_a_repeated_number_is_acted_on_once(self) -> None:
        fake = transport(standalone(child(1601)), label_ids(), {})

        result = state_over(fake).queue(targets=(1601, 1601))

        assert result.labeled == (1601,)
        assert label_writes(fake) == [("add", "I_1601", "LA_queued")]

    def test_the_bare_form_still_needs_a_target(self) -> None:
        fake = transport()

        with pytest.raises(NoTargetsError):
            _ = state_over(fake).queue()

        assert fake.calls == []


class TestWaitingTargets:
    def test_a_target_holding_a_queued_issue_is_still_waited_on(self) -> None:
        fake = transport(
            targets(epic(child(70, labels=["queued"])), target(child(1769)))
        )

        assert state_over(fake).waiting_targets((EPIC, 1769)) == (EPIC,)

    def test_a_standalone_target_reports_its_own_label(self) -> None:
        fake = transport(standalone(child(1769, labels=["queued"])))

        assert state_over(fake).waiting_targets((1769,)) == (1769,)

    def test_nothing_queued_anywhere_is_nothing_to_wait_on(self) -> None:
        fake = transport(
            targets(epic(child(70, labels=["planned"])), target(child(1769)))
        )

        assert state_over(fake).waiting_targets((EPIC, 1769)) == ()

    def test_a_closed_queued_issue_holds_nothing_up(self) -> None:
        fake = transport(children(child(70, state="CLOSED", labels=["queued"])))

        assert state_over(fake).waiting_targets((EPIC,)) == ()


class TestQueue:
    def test_labels_every_open_unlabeled_child(self) -> None:
        fake = transport(
            children(child(1), child(2, labels=["queued"]), child(3)),
            label_ids(),
            {},
            {},
        )

        result = state_over(fake).queue(EPIC)

        assert result.labeled == (1, 3)
        assert label_writes(fake) == [
            ("add", "I_1", "LA_queued"),
            ("add", "I_3", "LA_queued"),
        ]

    def test_listed_issues_restrict_the_scope(self) -> None:
        fake = transport(children(child(1), child(2), child(3)), label_ids(), {})

        result = state_over(fake).queue(EPIC, (3,))

        assert result.labeled == (3,)
        assert label_writes(fake) == [("add", "I_3", "LA_queued")]

    def test_children_already_carrying_a_batch_label_are_left_alone(self) -> None:
        fake = transport(
            children(child(1, labels=["planned"]), child(2, labels=["queued"]))
        )

        result = state_over(fake).queue(EPIC)

        assert result.labeled == ()
        assert [(item.number, item.reason) for item in result.skipped] == [
            (1, "already planned"),
            (2, "already queued"),
        ]
        assert label_writes(fake) == []

    def test_closed_children_are_never_queued(self) -> None:
        fake = transport(children(child(1, state="CLOSED"), child(2)), label_ids(), {})

        result = state_over(fake).queue(EPIC)

        assert result.labeled == (2,)
        assert [(item.number, item.reason) for item in result.skipped] == [
            (1, "closed")
        ]

    def test_an_epic_with_no_sub_issues_contributes_no_children(self) -> None:
        fake = transport(standalone(child(EPIC)))

        result = state_over(fake).queue(EPIC)

        assert result.labeled == ()
        assert label_writes(fake) == []

    def test_a_number_outside_the_epic_fails_before_writing(self) -> None:
        fake = transport(children(child(1), child(2)))

        with pytest.raises(NotAChildError, match="#999 is not a child of #1492"):
            state_over(fake).queue(EPIC, (1, 999))

        assert label_writes(fake) == []


class TestUnqueue:
    def test_removes_the_label_from_children_still_queued(self) -> None:
        fake = transport(
            children(child(1, labels=["queued"]), child(2, labels=["queued"])),
            label_ids(),
            {},
            {},
        )

        result = state_over(fake).unqueue(EPIC)

        assert result.labeled == (1, 2)
        assert label_writes(fake) == [
            ("remove", "I_1", "LA_queued"),
            ("remove", "I_2", "LA_queued"),
        ]

    def test_issues_past_queued_are_left_alone_and_reported(self) -> None:
        fake = transport(
            children(
                child(1, labels=["planned"]),
                child(2, labels=["implementing"]),
                child(3),
            )
        )

        result = state_over(fake).unqueue(EPIC)

        assert result.labeled == ()
        assert [(item.number, item.reason) for item in result.skipped] == [
            (1, "already planned"),
            (2, "already implementing"),
            (3, "not queued"),
        ]
        assert label_writes(fake) == []

    def test_listed_issues_restrict_the_scope(self) -> None:
        fake = transport(
            children(child(1, labels=["queued"]), child(2, labels=["queued"])),
            label_ids(),
            {},
        )

        result = state_over(fake).unqueue(EPIC, (2,))

        assert result.labeled == (2,)
        assert label_writes(fake) == [("remove", "I_2", "LA_queued")]


class TestApprove:
    def test_moves_every_queued_child_to_planned(self) -> None:
        fake = transport(
            children(child(1, labels=["queued"]), child(2, labels=["queued"])),
            label_ids(),
            {},
            {},
            {},
            {},
        )

        result = state_over(fake).approve(EPIC)

        assert result.approved == (1, 2)
        assert label_writes(fake) == [
            ("add", "I_1", "LA_planned"),
            ("remove", "I_1", "LA_queued"),
            ("add", "I_2", "LA_planned"),
            ("remove", "I_2", "LA_queued"),
        ]
        assert body_writes(fake) == []

    def test_listed_issues_restrict_the_scope(self) -> None:
        fake = transport(
            children(child(1, labels=["queued"]), child(2, labels=["queued"])),
            label_ids(),
            {},
            {},
        )

        result = state_over(fake).approve(EPIC, (1,))

        assert result.approved == (1,)
        assert label_writes(fake) == [
            ("add", "I_1", "LA_planned"),
            ("remove", "I_1", "LA_queued"),
        ]

    def test_children_not_queued_are_left_alone(self) -> None:
        fake = transport(
            children(
                child(1, labels=["implementing"]),
                child(2, labels=["planned"]),
                child(3),
            )
        )

        result = state_over(fake).approve(EPIC)

        assert result.approved == ()
        assert [(item.number, item.reason) for item in result.skipped] == [
            (1, "already implementing"),
            (2, "already planned"),
            (3, "not queued"),
        ]
        assert label_writes(fake) == []

    def test_guidance_appends_a_section_and_keeps_the_body(self) -> None:
        fake = transport(
            children(child(1, labels=["queued"], body="## What to build\n\nA thing")),
            {},
            label_ids(),
            {},
            {},
        )

        result = state_over(fake).approve(EPIC, guidance="No new tests.")

        assert result.approved == (1,)
        assert body_writes(fake) == [
            ("I_1", "## What to build\n\nA thing\n\n## Test Guidance\n\nNo new tests.")
        ]

    def test_guidance_replaces_an_existing_section(self) -> None:
        body = "Intro\n\n## Test Guidance\n\nOld text\n\n## Notes\n\nKeep me"
        fake = transport(
            children(child(1, labels=["queued"], body=body)),
            {},
            label_ids(),
            {},
            {},
        )

        state_over(fake).approve(EPIC, guidance="New text")

        assert body_writes(fake) == [
            ("I_1", "Intro\n\n## Test Guidance\n\nNew text\n\n## Notes\n\nKeep me")
        ]

    def test_guidance_is_inserted_literally_not_as_a_regex_template(self) -> None:
        body = "Intro\n\n## Test Guidance\n\nOld text"
        fake = transport(
            children(child(1, labels=["queued"], body=body)),
            {},
            label_ids(),
            {},
            {},
        )

        state_over(fake).approve(EPIC, guidance=r"match \1 and \g<0> in the log")

        assert body_writes(fake) == [
            (
                "I_1",
                "Intro\n\n## Test Guidance\n\nmatch \\1 and \\g<0> in the log",
            )
        ]

    def test_guidance_on_an_empty_body_is_the_whole_body(self) -> None:
        fake = transport(
            children(child(1, labels=["queued"], body="")),
            {},
            label_ids(),
            {},
            {},
        )

        state_over(fake).approve(EPIC, guidance="No new tests.")

        assert body_writes(fake) == [("I_1", "## Test Guidance\n\nNo new tests.")]

    def test_a_body_with_a_test_plan_refuses_the_guidance_write(self) -> None:
        body = "Intro\n\n## Test Plan\n\n1. Something"
        fake = transport(
            children(child(1, labels=["queued"], body=body)),
            label_ids(),
            {},
            {},
        )

        result = state_over(fake).approve(EPIC, guidance="No new tests.")

        assert result.approved == (1,)
        assert result.guidance_refused == (1,)
        assert body_writes(fake) == []
        assert label_writes(fake) == [
            ("add", "I_1", "LA_planned"),
            ("remove", "I_1", "LA_queued"),
        ]

    def test_without_guidance_no_body_is_written(self) -> None:
        fake = transport(
            children(child(1, labels=["queued"], body="Intro")),
            label_ids(),
            {},
            {},
        )

        state_over(fake).approve(EPIC)

        assert body_writes(fake) == []


class TestWriteVerbGuards:
    def test_closed_children_are_never_approved(self) -> None:
        fake = transport(children(child(1, state="CLOSED", labels=["queued"])))

        result = state_over(fake).approve(EPIC)

        assert result.approved == ()
        assert [(item.number, item.reason) for item in result.skipped] == [
            (1, "closed")
        ]
        assert label_writes(fake) == []

    def test_closed_children_are_never_unqueued(self) -> None:
        fake = transport(children(child(1, state="CLOSED", labels=["queued"])))

        result = state_over(fake).unqueue(EPIC)

        assert result.labeled == ()
        assert [(item.number, item.reason) for item in result.skipped] == [
            (1, "closed")
        ]
        assert label_writes(fake) == []

    def test_closed_children_are_never_fast_tracked(self) -> None:
        fake = transport(children(child(1, state="CLOSED")))

        result = state_over(fake).fast_track(EPIC)

        assert result.approved == ()
        assert [(item.number, item.reason) for item in result.skipped] == [
            (1, "closed")
        ]
        assert label_writes(fake) == []

    def test_a_conflicted_child_stops_a_write_verb_before_it_writes(self) -> None:
        fake = transport(
            children(child(1, labels=["queued"]), child(2, labels=["queued", "stuck"]))
        )

        with pytest.raises(ConflictingLabelsError) as exc_info:
            state_over(fake).approve(EPIC)

        assert exc_info.value.number == 2
        assert label_writes(fake) == []

    def test_set_state_refuses_a_conflicted_issue(self) -> None:
        fake = transport(issue(child(42, labels=["implementing", "stuck"])))

        with pytest.raises(ConflictingLabelsError):
            state_over(fake).set_state(42, BatchLabel.PLANNED)

        assert label_writes(fake) == []

    def test_clear_state_refuses_a_conflicted_issue(self) -> None:
        fake = transport(issue(child(42, labels=["implementing", "stuck"])))

        with pytest.raises(ConflictingLabelsError):
            state_over(fake).clear_state(42)

        assert label_writes(fake) == []

    def test_a_missing_label_in_the_repo_is_a_loud_error(self) -> None:
        fake = transport(children(child(1)), label_ids(missing=["stuck"]))

        with pytest.raises(RuntimeError, match="Labels not found in repo: stuck"):
            state_over(fake).queue(EPIC)

        assert label_writes(fake) == []


class TestBatchBodies:
    def test_the_body_travels_with_each_batch_issue(self) -> None:
        response = children(child(7, labels=["planned"], body="## Test Plan\n\n1. x\n"))

        batch = state(response).batch((EPIC,))

        assert batch.issues[0].body == "## Test Plan\n\n1. x\n"


class TestFinished:
    def test_only_closed_children_that_still_carry_a_label_are_candidates(
        self,
    ) -> None:
        response = children(
            child(10, state="CLOSED", labels=["ready-for-review"]),
            child(11, state="OPEN", labels=["implementing"]),
            child(12, state="CLOSED", labels=[]),
            child(13, state="CLOSED", labels=["stuck"], title="Merged anyway"),
        )

        finished = state(response).finished((EPIC,))

        assert [issue.number for issue in finished] == [10, 13]
        assert [issue.state for issue in finished] == [
            BatchLabel.READY_FOR_REVIEW,
            BatchLabel.STUCK,
        ]
        assert finished[1].title == "Merged anyway"

    def test_a_merged_and_closed_labeled_child_is_still_teardown_work(self) -> None:
        response = children(
            child(
                1600,
                state="CLOSED",
                labels=["ready-for-review"],
                closing_prs=[True],
            )
        )

        finished = state(response).finished((EPIC,))

        assert [issue.number for issue in finished] == [1600]

    def test_a_closed_child_with_two_batch_labels_is_rejected(self) -> None:
        response = children(
            child(10, state="CLOSED", labels=["ready-for-review", "stuck"])
        )

        with pytest.raises(ConflictingLabelsError):
            _ = state(response).finished((EPIC,))


class TestLabelState:
    @pytest.mark.parametrize(
        ("closing_prs", "expected"), [((True,), True), ((False,), False), ((), False)]
    )
    def test_the_merge_that_closed_the_issue_reaches_recovery(
        self, closing_prs: tuple[bool, ...], expected: bool
    ) -> None:
        response = issue(
            child(1503, state="CLOSED", labels=["planned"], closing_prs=closing_prs)
        )

        found = state(response).label_state(1503)

        assert found.closed is True
        assert found.label is BatchLabel.PLANNED
        assert found.closed_by_merge is expected


class TestPolledBatch:
    def test_a_poll_without_a_cache_asks_github_every_time(self) -> None:
        response = children(child(70, labels=["planned"], state="CLOSED"))
        fake = transport(response, response)
        polled = state_over(fake)

        _ = polled.batch((EPIC,))
        _ = polled.batch((EPIC,))

        assert fetches(fake) == 2

    def test_a_settled_target_is_bought_once_and_then_remembered(self) -> None:
        response = children(child(70, labels=["planned"], state="CLOSED"))
        fake = transport(response)
        polled, cache = state_over(fake), SettledTargets()

        first = polled.batch((EPIC,), settled=cache)
        second = polled.batch((EPIC,), settled=cache)

        assert fetches(fake) == 1
        assert second.dropped == first.dropped

    def test_a_target_with_an_open_child_is_asked_for_again(self) -> None:
        response = children(child(70, labels=["planned"]))
        fake = transport(response, response)
        polled, cache = state_over(fake), SettledTargets()

        _ = polled.batch((EPIC,), settled=cache)
        _ = polled.batch((EPIC,), settled=cache)

        assert fetches(fake) == 2

    def test_the_batch_carries_the_budget_the_last_query_reported(self) -> None:
        fake = transport(children(child(70, labels=["planned"])))

        found = state_over(fake).batch((EPIC,)).rate_limit

        assert found is not None
        assert found.remaining == 4998


class TestStackOrder:
    def _bases(self, response: Response, *named: int) -> list[str]:
        batch = state(response).batch(named)
        return [base_under(batch, issue) for issue in batch.issues]

    def test_the_stack_does_not_restart_at_a_target_boundary(self) -> None:
        started = ["ready-for-review"]
        response = targets(
            epic(child(70, labels=started), child(12, labels=started)),
            target(child(1769, labels=started)),
        )

        assert self._bases(response, EPIC, 1769) == [MAIN, "issue-70", "issue-12"]

    def test_reversing_the_targets_reverses_the_bases(self) -> None:
        started = ["ready-for-review"]
        response = targets(
            target(child(1769, labels=started)),
            epic(child(70, labels=started), child(12, labels=started)),
        )

        assert self._bases(response, 1769, EPIC) == [MAIN, "issue-1769", "issue-70"]

    def test_the_next_issue_to_start_is_cut_from_the_tip_of_the_stack(self) -> None:
        response = targets(
            epic(child(70, labels=["ready-for-review"])),
            target(child(1769, labels=["planned"])),
        )
        batch = state(response).batch((EPIC, 1769))

        assert base_for(batch, batch.issues[1]) == "issue-70"


class TestPlanningWalk:
    def test_the_next_queued_issue_comes_from_whichever_target_holds_it(self) -> None:
        response = targets(
            epic(child(70, labels=["planned"])),
            target(child(1769, labels=["queued"])),
        )

        found = PlanningAgent(state(response)).next_issue((EPIC, 1769))

        assert found is not None
        assert found.number == 1769
        assert found.predecessors == (70,)
