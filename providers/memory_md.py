"""Opt-in adapter for Claude Code's project auto-memory directory."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import Lesson, MemoryProvider
from ._util import (
    allow_quarantined,
    parse_lesson,
    prepare_recalled_lessons,
    rank_lessons,
    serialize_lesson,
    validate_lessons,
)


class MemoryMarkdownProvider(MemoryProvider):
    """Read and append only the dedicated ``axiom-lessons.md`` memory file."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config or {})
        if self.config.get("memory_provider") != "memory_md":
            raise ValueError("memory_md requires config memory_provider='memory_md'")
        cwd = Path(self.config.get("cwd", Path.cwd())).resolve()
        project_slug = str(cwd).replace("/", "-")
        self._memory_path = (
            Path.home()
            / ".claude"
            / "projects"
            / project_slug
            / "memory"
            / "axiom-lessons.md"
        )

    def recall(self, query: str, limit: int = 5) -> list[Lesson]:
        try:
            lines = self._memory_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        parsed = [item for line in lines if (item := parse_lesson(line)) is not None]
        ranked = rank_lessons(parsed, query, len(parsed))
        prepared = prepare_recalled_lessons(
            ranked,
            allow_quarantined=allow_quarantined(self.config),
        )
        return prepared[: max(limit, 0)]

    def persist(self, lessons: list[Lesson]) -> int:
        validated = validate_lessons(lessons)
        if not validated:
            return 0
        self._memory_path.parent.mkdir(parents=True, exist_ok=True)
        with self._memory_path.open("a", encoding="utf-8") as handle:
            for item in validated:
                handle.write(serialize_lesson(item))
        return len(validated)
