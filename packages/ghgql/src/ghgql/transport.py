from __future__ import annotations

import os
from collections.abc import Mapping
from threading import Lock, get_ident
from typing import NamedTuple, cast, final, override

import requests
from gql import Client, gql
from gql.graphql_request import GraphQLRequest
from gql.transport.exceptions import TransportConnectionFailed, TransportQueryError
from gql.transport.requests import RequestsHTTPTransport
from gql.transport.transport import Transport
from graphql import ExecutionResult

from ghgql.errors import GitHubTimeoutError, IssueNotFoundError, RateLimitError

_URL = "https://api.github.com/graphql"
DEFAULT_TIMEOUT = 30


class RateLimit(NamedTuple):
    """What a query's own `rateLimit` selection reported. Selecting it is free."""

    cost: int
    remaining: int
    limit: int
    reset_at: str


@final
class GitHubTransport(Transport):
    """Authenticates from GITHUB_TOKEN, read on connect so construction never fails.

    The wrapped requests transport is per-thread: gql opens a session on
    connect and drops it on close around every call, and callers run
    concurrent queries on worker threads, which one shared session
    cannot survive.
    """

    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.timeout: int = timeout
        self._transports: dict[int, RequestsHTTPTransport] = {}
        self._lock: Lock = Lock()

    def _requests(self) -> RequestsHTTPTransport:
        thread = get_ident()
        with self._lock:
            transport = self._transports.get(thread)
            if transport is None:
                transport = RequestsHTTPTransport(
                    url=_URL, verify=True, timeout=self.timeout
                )
                self._transports[thread] = transport
            return transport

    @override
    def connect(self) -> None:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise RuntimeError("GITHUB_TOKEN environment variable is not set")
        transport = self._requests()
        transport.headers = {"Authorization": f"bearer {token}"}
        transport.connect()

    @override
    def execute(
        self,
        request: GraphQLRequest,
        timeout: int | None = None,
        extra_args: dict[str, object] | None = None,
        upload_files: bool = False,
    ) -> ExecutionResult:
        return self._requests().execute(
            request,
            timeout=timeout,
            extra_args=extra_args,
            upload_files=upload_files,
        )

    @override
    def close(self) -> None:
        self._requests().close()


class GitHubGraphQL:
    def __init__(self, transport: Transport) -> None:
        self._transport: Transport = transport
        self.rate_limit: RateLimit | None = None

    def run(self, query_text: str, **variables: str | int | None) -> dict[str, object]:
        client = Client(transport=self._transport, parse_results=False)
        query = gql(query_text)
        query.variable_values = variables
        try:
            data = client.execute(query)
        except TransportQueryError as exc:
            raise _query_error(exc, variables, self.rate_limit) from exc
        except TransportConnectionFailed as exc:
            if not isinstance(exc.__cause__, requests.exceptions.Timeout):
                raise
            raise GitHubTimeoutError(_URL, _timeout_of(self._transport)) from exc
        self.rate_limit = _rate_limit(data) or self.rate_limit
        return data


def _rate_limit(data: Mapping[str, object]) -> RateLimit | None:
    reported = data.get("rateLimit")
    if not isinstance(reported, Mapping):
        return None
    fields = cast("Mapping[str, object]", reported)
    try:
        return RateLimit(
            cost=int(cast("int", fields["cost"])),
            remaining=int(cast("int", fields["remaining"])),
            limit=int(cast("int", fields["limit"])),
            reset_at=str(fields["resetAt"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _timeout_of(transport: Transport) -> int:
    if isinstance(transport, GitHubTransport):
        return transport.timeout
    return DEFAULT_TIMEOUT


def _query_error(
    exc: TransportQueryError,
    variables: dict[str, str | int | None],
    rate_limit: RateLimit | None,
) -> Exception:
    errors = cast("list[Mapping[str, object]]", exc.errors or [])
    number = variables.get("number")
    if isinstance(number, int) and any(
        error.get("type") == "NOT_FOUND" for error in errors
    ):
        return IssueNotFoundError(number)
    messages = [str(error.get("message", error)) for error in errors] or [str(exc)]
    joined = "; ".join(messages)
    if _rate_limited(errors, messages):
        return RateLimitError(joined, rate_limit.reset_at if rate_limit else None)
    return RuntimeError(joined)


def _rate_limited(errors: list[Mapping[str, object]], messages: list[str]) -> bool:
    """GitHub types the refusal, but has answered with an untyped message before now."""
    return any(error.get("type") == "RATE_LIMITED" for error in errors) or any(
        "rate limit" in message.lower() for message in messages
    )
