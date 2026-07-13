"""
Generic example adapter for connecting to a gbrain-like external knowledge CLI.

This module intentionally contains no product-specific backend URL or filesystem
location. Commands are supplied entirely by caller configuration.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from . import Lesson, MemoryProvider
from ._util import allow_quarantined, prepare_recalled_lessons


class GbrainAdapter(MemoryProvider):
    """Fail-soft subprocess adapter for a configured external knowledge CLI."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config or {})

    @staticmethod
    def _command(template: object, values: dict[str, str]) -> list[str]:
        if isinstance(template, str):
            parts = shlex.split(template)
        elif isinstance(template, (list, tuple)) and all(
            isinstance(part, str) for part in template
        ):
            parts = list(template)
        else:
            raise ValueError("command template must be a string or argv array")
        return [part.format(**values) for part in parts]

    def _run(
        self, template: object, values: dict[str, str]
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            argv = self._command(template, values)
            if not argv:
                return None
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                shell=False,
            )
            if completed.returncode != 0:
                return None
            return completed
        except Exception:
            return None

    def recall(self, query: str, limit: int = 5) -> list[Lesson]:
        try:
            template = self.config.get("recall_cmd")
            completed = self._run(template, {"query": query, "limit": str(limit)})
            if completed is None:
                return []
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            try:
                payload = json.loads(completed.stdout)
            except json.JSONDecodeError:
                payload = [
                    line for line in completed.stdout.splitlines() if line.strip()
                ]
            if isinstance(payload, dict):
                payload = payload.get("results", payload.get("lessons", []))
            if not isinstance(payload, list):
                return []
            lessons: list[Lesson] = []
            for item in payload:
                if isinstance(item, str):
                    lessons.append(Lesson(item, "external-cli", now, []))
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    tags = item.get("tags", [])
                    lessons.append(
                        Lesson(
                            text=item["text"],
                            source=str(item.get("source") or "external-cli"),
                            timestamp=str(item.get("timestamp") or now),
                            tags=[str(tag) for tag in tags]
                            if isinstance(tags, list)
                            else [],
                        )
                    )
        except Exception:
            return []
        prepared = prepare_recalled_lessons(
            lessons,
            allow_quarantined=allow_quarantined(self.config),
        )
        return prepared[: max(limit, 0)]

    def persist(self, lessons: list[Lesson]) -> int:
        try:
            template = self.config.get("persist_cmd")
            serialized = json.dumps(
                [asdict(item) for item in lessons],
                separators=(",", ":"),
            )
            template_text = (
                template if isinstance(template, str) else " ".join(template or [])
            )
            item_fields = ("{text}", "{source}", "{timestamp}", "{tags}")
            if any(field in template_text for field in item_fields):
                for item in lessons:
                    completed = self._run(
                        template,
                        {
                            "text": item.text,
                            "source": item.source,
                            "timestamp": item.timestamp,
                            "tags": ",".join(item.tags),
                        },
                    )
                    if completed is None:
                        return 0
                return len(lessons)

            completed = self._run(
                template,
                {"lessons": serialized, "count": str(len(lessons))},
            )
            if completed is None:
                return 0
            output = completed.stdout.strip()
            if not output:
                return len(lessons)
            try:
                return int(output)
            except ValueError:
                payload = json.loads(output)
                return (
                    int(payload.get("count", len(lessons)))
                    if isinstance(payload, dict)
                    else 0
                )
        except Exception:
            return 0
