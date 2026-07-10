"""Shared lesson validation, parsing, scoring, and recall safeguards."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import re
from typing import Iterable

from . import Lesson


UNVERIFIED_PREFIX = "[unverified memory]"
INJECTION_PATTERNS = (
    re.compile(r"\bignore\s+(?:all\s+)?previous(?:\s+instructions?)?\b", re.IGNORECASE),
    re.compile(r"\bdisregard\b", re.IGNORECASE),
    re.compile(r"\byou\s+must\b", re.IGNORECASE),
    re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE),
    re.compile(r"\bdeveloper\s+(?:message|instructions?)\b", re.IGNORECASE),
    re.compile(
        r"\boverride\s+(?:the\s+)?(?:prior|previous|system)\s+instructions?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\breveal\s+(?:the\s+)?(?:hidden\s+)?prompt\b", re.IGNORECASE),
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
_LINE_RE = re.compile(r"^- \[([^\]]+)\] \[([^\]]+)\] (.*)$")


def is_instruction_like(text: str) -> bool:
    """Return whether text resembles prompt or instruction manipulation."""

    return any(pattern.search(text) for pattern in INJECTION_PATTERNS)


def prepare_recalled_lessons(
    lessons: Iterable[Lesson],
    *,
    allow_quarantined: bool = False,
    include_recall_label: bool = False,
) -> list[Lesson]:
    """Quarantine suspicious lessons and mark every returned lesson unverified."""

    prepared: list[Lesson] = []
    marker = f"{UNVERIFIED_PREFIX}"
    if include_recall_label:
        marker += " unverified recall"
    for item in lessons:
        quarantined = is_instruction_like(item.text)
        if quarantined and not allow_quarantined:
            continue
        tags = list(item.tags)
        if quarantined and "quarantined" not in tags:
            tags.append("quarantined")
        text = item.text
        if not text.startswith(UNVERIFIED_PREFIX):
            text = f"{marker} {text}"
        prepared.append(replace(item, text=text, tags=tags))
    return prepared


def allow_quarantined(config: dict[str, object]) -> bool:
    """Read the explicit opt-in for returning quarantined content."""

    return bool(
        config.get("allow_quarantined_lessons", config.get("allow_quarantined", False))
    )


def validate_lessons(lessons: Iterable[Lesson]) -> list[Lesson]:
    """Validate metadata required by markdown-backed memory providers."""

    validated = list(lessons)
    for item in validated:
        if not item.timestamp.strip():
            raise ValueError("lesson timestamp must not be empty")
        if not item.source.strip():
            raise ValueError("lesson source must not be empty")
    return validated


def serialize_lesson(item: Lesson) -> str:
    """Serialize a lesson as one append-only markdown line."""

    timestamp = " ".join(item.timestamp.split())
    source = " ".join(item.source.split())
    text = " ".join(item.text.split())
    tags = "".join(f" #{tag.lstrip('#')}" for tag in item.tags if tag.lstrip("#"))
    return f"- [{timestamp}] [{source}] {text}{tags}\n"


def parse_lesson(line: str) -> Lesson | None:
    """Parse one provider-owned markdown lesson line."""

    match = _LINE_RE.match(line.strip())
    if not match:
        return None
    timestamp, source, remainder = match.groups()
    words = remainder.split()
    tags: list[str] = []
    while words and words[-1].startswith("#") and len(words[-1]) > 1:
        tags.append(words.pop()[1:])
    tags.reverse()
    return Lesson(text=" ".join(words), source=source, timestamp=timestamp, tags=tags)


def rank_lessons(lessons: Iterable[Lesson], query: str, limit: int) -> list[Lesson]:
    """Rank by keyword overlap first and timestamp recency second."""

    if limit <= 0:
        return []
    query_tokens = set(_TOKEN_RE.findall(query.lower()))
    scored: list[tuple[float, Lesson]] = []
    for item in lessons:
        item_tokens = set(_TOKEN_RE.findall(" ".join([item.text, *item.tags]).lower()))
        overlap = len(query_tokens & item_tokens)
        if query_tokens and overlap == 0:
            continue
        try:
            timestamp = datetime.fromisoformat(
                item.timestamp.replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, OverflowError):
            timestamp = 0.0
        score = overlap * 10.0 + max(timestamp, 0.0) / 10_000_000_000.0
        scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:limit]]
