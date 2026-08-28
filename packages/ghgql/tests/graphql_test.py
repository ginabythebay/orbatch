from __future__ import annotations

import pytest

from ghgql.errors import IssueNotFoundError, RateLimitError
from ghgql.fake import Call, Errors, FakeTransport, normalized
from ghgql.transport import GitHubGraphQL, RateLimit

_REPO_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    id
  }
}
"""

_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      title
    }
  }
}
"""


class TestRun:
    def test_returns_the_queued_response(self) -> None:
        response: dict[str, object] = {
            "repository": {"issue": {"title": "Fix the widget"}}
        }
        graphql = GitHubGraphQL(FakeTransport([response]))
        assert graphql.run(_QUERY, owner="o", name="n", number=42) == response

    def test_records_the_query_and_variables(self) -> None:
        transport = FakeTransport([{}])
        GitHubGraphQL(transport).run(_QUERY, owner="o", name="n", number=42)
        assert transport.calls == [
            Call(normalized(_QUERY), {"owner": "o", "name": "n", "number": 42})
        ]

    def test_returns_queued_responses_in_order(self) -> None:
        first: dict[str, object] = {"repository": {"issue": {"title": "first"}}}
        second: dict[str, object] = {"repository": {"issue": {"title": "second"}}}
        graphql = GitHubGraphQL(FakeTransport([first, second]))
        assert graphql.run(_QUERY, owner="o", name="n", number=1) == first
        assert graphql.run(_QUERY, owner="o", name="n", number=2) == second

    def test_an_exhausted_queue_fails_loudly(self) -> None:
        graphql = GitHubGraphQL(FakeTransport([{}]))
        graphql.run(_QUERY, owner="o", name="n", number=1)
        with pytest.raises(AssertionError, match="ran out of responses on call 2"):
            graphql.run(_QUERY, owner="o", name="n", number=2)


class TestErrorMapping:
    def test_a_missing_issue_becomes_issue_not_found(self) -> None:
        errors = Errors(
            [
                {
                    "type": "NOT_FOUND",
                    "message": "Could not resolve to an issue with the number of 9999.",
                }
            ]
        )
        graphql = GitHubGraphQL(FakeTransport([errors]))
        with pytest.raises(IssueNotFoundError) as exc_info:
            graphql.run(_QUERY, owner="o", name="n", number=9999)
        assert exc_info.value.number == 9999
        assert "#9999 does not exist" in str(exc_info.value)

    def test_other_errors_become_runtime_errors(self) -> None:
        errors = Errors([{"type": "FORBIDDEN", "message": "no access"}])
        graphql = GitHubGraphQL(FakeTransport([errors]))
        with pytest.raises(RuntimeError) as exc_info:
            graphql.run(_QUERY, owner="o", name="n", number=42)
        assert not isinstance(exc_info.value, IssueNotFoundError)
        assert "no access" in str(exc_info.value)

    def test_not_found_without_an_issue_number_stays_a_runtime_error(self) -> None:
        errors = Errors(
            [{"type": "NOT_FOUND", "message": "Could not resolve to a Repository"}]
        )
        graphql = GitHubGraphQL(FakeTransport([errors]))
        with pytest.raises(RuntimeError) as exc_info:
            graphql.run(_REPO_QUERY, owner="o", name="nope")
        assert not isinstance(exc_info.value, IssueNotFoundError)
        assert "Could not resolve to a Repository" in str(exc_info.value)


_BUDGET = {
    "cost": 2,
    "remaining": 4998,
    "limit": 5000,
    "resetAt": "2026-08-21T13:00:00Z",
}


class TestRateLimit:
    def test_a_query_that_asks_records_what_it_was_told(self) -> None:
        graphql = GitHubGraphQL(FakeTransport([{"rateLimit": _BUDGET}]))
        graphql.run(_REPO_QUERY, owner="o", name="n")
        assert graphql.rate_limit == RateLimit(
            cost=2, remaining=4998, limit=5000, reset_at="2026-08-21T13:00:00Z"
        )

    def test_a_query_that_does_not_ask_leaves_the_last_reading_alone(self) -> None:
        graphql = GitHubGraphQL(
            FakeTransport([{"rateLimit": _BUDGET}, {"repository": {"id": "R_1"}}])
        )
        graphql.run(_REPO_QUERY, owner="o", name="n")
        graphql.run(_REPO_QUERY, owner="o", name="n")
        assert graphql.rate_limit is not None
        assert graphql.rate_limit.remaining == 4998

    def test_exhaustion_becomes_a_rate_limit_error_carrying_the_reset(self) -> None:
        errors = Errors(
            [
                {
                    "type": "RATE_LIMITED",
                    "message": "API rate limit exceeded for user ID 1.",
                }
            ]
        )
        graphql = GitHubGraphQL(FakeTransport([{"rateLimit": _BUDGET}, errors]))
        graphql.run(_REPO_QUERY, owner="o", name="n")
        with pytest.raises(RateLimitError) as exc_info:
            graphql.run(_REPO_QUERY, owner="o", name="n")
        assert exc_info.value.reset_at == "2026-08-21T13:00:00Z"
        assert "Resets 2026-08-21T13:00:00Z" in str(exc_info.value)

    def test_an_untyped_refusal_is_recognised_by_its_message(self) -> None:
        errors = Errors([{"message": "API rate limit already exceeded for user ID 1."}])
        graphql = GitHubGraphQL(FakeTransport([errors]))
        with pytest.raises(RateLimitError):
            graphql.run(_REPO_QUERY, owner="o", name="n")

    def test_a_rate_limit_error_is_a_runtime_error(self) -> None:
        assert issubclass(RateLimitError, RuntimeError)
