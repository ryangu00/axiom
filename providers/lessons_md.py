"""Append-only markdown memory using the shared Axiom state layer."""

from __future__ import annotations

import importlib
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


class LessonsMarkdownProvider(MemoryProvider):
    """Persist lessons in the project-scoped shared state file."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config or {})

    def _path(self) -> Path:
        common = importlib.import_module("axiom_common")
        data_root_value = self.config.get("data_root")
        argv = (
            ["--data-root", str(data_root_value)] if data_root_value is not None else []
        )
        root = Path(common.data_root(argv))
        cwd = Path(self.config.get("cwd", Path.cwd())).resolve()
        pid = common.project_id(cwd)
        return root / "v1" / "projects" / pid / "lessons.md"

    def recall(self, query: str, limit: int = 5) -> list[Lesson]:
        path = self._path()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        parsed = [item for line in lines if (item := parse_lesson(line)) is not None]
        ranked = rank_lessons(parsed, query, len(parsed))
        prepared = prepare_recalled_lessons(
            ranked,
            allow_quarantined=allow_quarantined(self.config),
            include_recall_label=True,
        )
        return prepared[: max(limit, 0)]

    def persist(self, lessons: list[Lesson]) -> int:
        validated = validate_lessons(lessons)
        if not validated:
            return 0
        path = self._path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for item in validated:
                handle.write(serialize_lesson(item))
        return len(validated)
