"""Public provider interfaces and built-in provider registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class VerifyResult:
    """Normalized result returned by a write verifier."""

    ok: bool
    expected: Any
    actual: Any


@dataclass
class Lesson:
    """Portable lesson representation shared by memory providers."""

    text: str
    source: str
    timestamp: str
    tags: list[str] = field(default_factory=list)


class WriteVerifier(ABC):
    """Verify that a write or related side effect actually occurred."""

    @abstractmethod
    def verify(self, predicate: dict[str, Any]) -> VerifyResult:
        """Evaluate one predicate and return expected and actual values."""


class MemoryProvider(ABC):
    """Recall and persist portable lessons."""

    @abstractmethod
    def recall(self, query: str, limit: int = 5) -> list[Lesson]:
        """Return up to ``limit`` lessons relevant to ``query``."""

    @abstractmethod
    def persist(self, lessons: list[Lesson]) -> int:
        """Persist lessons and return the number accepted."""


def get_provider(kind: str, name: str, config: dict[str, Any] | None = None) -> object:
    """Construct a built-in provider by category and name."""

    provider_config = dict(config or {})
    normalized_kind = kind.strip().lower().replace("-", "_")
    normalized_name = name.strip().lower().replace("-", "_")

    if normalized_kind == "write_verifier" and normalized_name == "fs_git":
        from .fs_git import FsGitWriteVerifier

        return FsGitWriteVerifier(provider_config)
    if normalized_kind == "memory":
        if normalized_name == "lessons_md":
            from .lessons_md import LessonsMarkdownProvider

            return LessonsMarkdownProvider(provider_config)
        if normalized_name == "memory_md":
            from .memory_md import MemoryMarkdownProvider

            return MemoryMarkdownProvider(provider_config)
        if normalized_name == "gbrain_adapter":
            from .gbrain_adapter import GbrainAdapter

            return GbrainAdapter(provider_config)
    raise ValueError(f"unknown provider: kind={kind!r}, name={name!r}")


__all__ = [
    "Lesson",
    "MemoryProvider",
    "VerifyResult",
    "WriteVerifier",
    "get_provider",
]
