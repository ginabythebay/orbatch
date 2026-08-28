from __future__ import annotations


class IssueNotFoundError(RuntimeError):
    def __init__(self, number: int) -> None:
        super().__init__(f"#{number} does not exist")
        self.number: int = number


class GitHubTimeoutError(RuntimeError):
    def __init__(self, url: str, timeout: int) -> None:
        super().__init__(f"{url} did not respond within {timeout}s")
        self.url: str = url
        self.timeout: int = timeout


class RateLimitError(RuntimeError):
    def __init__(self, message: str, reset_at: str | None = None) -> None:
        super().__init__(
            message if reset_at is None else f"{message} Resets {reset_at}"
        )
        self.reset_at: str | None = reset_at
