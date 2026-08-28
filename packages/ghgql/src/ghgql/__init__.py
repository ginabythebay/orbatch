"""Shared GitHub GraphQL plumbing for host-side tooling.

The transport is injected, never patched. Production builds a
``GitHubGraphQL`` over a ``GitHubTransport``; tests build one over a
``FakeTransport`` carrying a queue of canned responses::

    graphql = GitHubGraphQL(GitHubTransport())
    graphql = GitHubGraphQL(FakeTransport([{"repository": ...}]))

Because the seam sits below the error mapping, error-path tests run
through the real ``gql`` client and the real mapping: a queued
``errors`` list makes ``gql`` raise the same ``TransportQueryError``
GitHub would.

Consumers import from the submodules, not from here.
"""
