from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import NamedTuple, cast, override

from gql import gql
from gql.graphql_request import GraphQLRequest
from gql.transport.transport import Transport
from graphql import ExecutionResult, GraphQLError, print_ast


def normalized(query_text: str) -> str:
    """The form recorded in `Call.query_text`, for comparing against a source query."""
    return print_ast(gql(query_text).document)


class Call(NamedTuple):
    query_text: str
    variables: dict[str, object]


class Errors(NamedTuple):
    """A queued GraphQL error payload, as GitHub would return it."""

    errors: Sequence[Mapping[str, object]]


type Response = Mapping[str, object] | Errors


class FakeTransport(Transport):
    def __init__(self, responses: Sequence[Response]) -> None:
        self._responses: list[Response] = list(responses)
        self.calls: list[Call] = []

    @override
    def execute(
        self,
        request: GraphQLRequest,
        *args: object,
        **kwargs: object,
    ) -> ExecutionResult:
        variables = cast("dict[str, object]", request.variable_values or {})
        self.calls.append(Call(print_ast(request.document), dict(variables)))
        if not self._responses:
            raise AssertionError(
                f"FakeTransport ran out of responses on call {len(self.calls)}"
            )
        response = self._responses.pop(0)
        if isinstance(response, Errors):
            # GitHub's errors arrive as raw dicts, which is what the real
            # transport hands on despite the GraphQLError annotation.
            return ExecutionResult(
                data=None, errors=cast("list[GraphQLError]", response.errors)
            )
        return ExecutionResult(data=dict(response), errors=None)
