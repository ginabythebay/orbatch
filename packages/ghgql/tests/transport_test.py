# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest
import requests
from gql import gql
from gql.graphql_request import GraphQLRequest
from gql.transport.exceptions import TransportConnectionFailed
from gql.transport.requests import RequestsHTTPTransport
from graphql import ExecutionResult

from ghgql.errors import GitHubTimeoutError
from ghgql.transport import DEFAULT_TIMEOUT, GitHubGraphQL, GitHubTransport

_QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    id
  }
}
"""

_RESPONSE = {"repository": {"id": "R_1"}}


def _canned(
    _transport: RequestsHTTPTransport, _request: GraphQLRequest, **_kwargs: object
) -> ExecutionResult:
    return ExecutionResult(data=_RESPONSE, errors=None)


def _capture_requests(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    timeouts: list[object] = []

    def request(
        _session: requests.Session, _method: str, _url: str, **kwargs: object
    ) -> requests.Response:
        timeouts.append(kwargs.get("timeout"))
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps({"data": _RESPONSE}).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return timeouts


def _failing_requests(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def request(
        _session: requests.Session, _method: str, _url: str, **_kwargs: object
    ) -> requests.Response:
        raise exc

    monkeypatch.setattr(requests.Session, "request", request)


class TestGitHubTransport:
    def test_constructing_without_a_token_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        GitHubTransport()

    def test_connecting_without_a_token_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        transport = GitHubTransport()
        with pytest.raises(
            RuntimeError, match="GITHUB_TOKEN environment variable is not set"
        ):
            transport.connect()

    def test_authenticates_against_the_github_endpoint(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
        transport = GitHubTransport()
        transport.connect()
        try:
            assert transport._requests().url == "https://api.github.com/graphql"
            assert transport._requests().headers == {"Authorization": "bearer t0ken"}
        finally:
            transport.close()

    def test_the_same_instance_serves_repeated_queries(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
        graphql = GitHubGraphQL(GitHubTransport())
        with patch.object(RequestsHTTPTransport, "execute", _canned):
            assert graphql.run(_QUERY, owner="o", name="n") == _RESPONSE
            assert graphql.run(_QUERY, owner="o", name="n") == _RESPONSE

    def test_concurrent_queries_do_not_share_a_session(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
        graphql = GitHubGraphQL(GitHubTransport())
        # Both threads sit inside execute at once: one requests session
        # shared between them raises TransportAlreadyConnected on the
        # second connect, or TransportClosed when the first closes.
        barrier = Barrier(2, timeout=10)

        def blocking(
            transport: RequestsHTTPTransport,
            request: GraphQLRequest,
            **kwargs: object,
        ) -> ExecutionResult:
            _ = barrier.wait()
            return _canned(transport, request, **kwargs)

        def query() -> dict[str, object]:
            return graphql.run(_QUERY, owner="o", name="n")

        with (
            patch.object(RequestsHTTPTransport, "execute", blocking),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(query) for _ in range(2)]
            assert [f.result(timeout=10) for f in futures] == [_RESPONSE, _RESPONSE]


class TestRequestTimeout:
    def test_the_per_thread_transport_carries_the_default_timeout(self) -> None:
        assert GitHubTransport()._requests().default_timeout == DEFAULT_TIMEOUT

    def test_a_constructor_argument_overrides_the_default(self) -> None:
        assert GitHubTransport(timeout=5)._requests().default_timeout == 5

    def test_a_per_call_timeout_beats_the_constructor_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
        captured = _capture_requests(monkeypatch)
        transport = GitHubTransport(timeout=30)
        transport.connect()
        try:
            _ = transport.execute(
                GraphQLRequest(
                    gql(_QUERY), variable_values={"owner": "o", "name": "n"}
                ),
                timeout=2,
            )
        finally:
            transport.close()
        assert captured == [2]

    def test_a_caller_that_passes_no_timeout_gets_the_configured_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
        captured = _capture_requests(monkeypatch)
        graphql = GitHubGraphQL(GitHubTransport())
        assert graphql.run(_QUERY, owner="o", name="n") == _RESPONSE
        assert captured == [DEFAULT_TIMEOUT]

    def test_a_read_timeout_becomes_a_github_timeout_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
        _failing_requests(monkeypatch, requests.exceptions.ReadTimeout("too slow"))
        graphql = GitHubGraphQL(GitHubTransport(timeout=7))
        with pytest.raises(GitHubTimeoutError) as exc_info:
            _ = graphql.run(_QUERY, owner="o", name="n")
        assert (
            str(exc_info.value)
            == "https://api.github.com/graphql did not respond within 7s"
        )

    def test_a_timeout_is_a_runtime_error(self) -> None:
        assert issubclass(GitHubTimeoutError, RuntimeError)

    def test_a_connect_timeout_is_wrapped_too(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
        _failing_requests(
            monkeypatch, requests.exceptions.ConnectTimeout("no handshake")
        )
        graphql = GitHubGraphQL(GitHubTransport())
        with pytest.raises(GitHubTimeoutError):
            _ = graphql.run(_QUERY, owner="o", name="n")

    def test_a_dropped_connection_is_not_wrapped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
        _failing_requests(
            monkeypatch, requests.exceptions.ConnectionError("connection reset")
        )
        graphql = GitHubGraphQL(GitHubTransport())
        with pytest.raises(TransportConnectionFailed) as exc_info:
            _ = graphql.run(_QUERY, owner="o", name="n")
        assert "connection reset" in str(exc_info.value)
