"""Search-provider interface placeholder for the next implementation task."""

from __future__ import annotations

from typing import Protocol


class SearchProvider(Protocol):
    """Interface implemented by pluggable search providers."""

    def search(self, query: str, *, limit: int = 5) -> list[dict[str, str]]:
        """Return normalized search results for a query."""
        ...
