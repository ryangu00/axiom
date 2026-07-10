#!/usr/bin/env python3
"""Advise against storing persistent Write/Edit artifacts in temporary paths."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import axiom_common as common

RULE = "schema-guard"
DEFAULT_PATTERNS = [r"ledger", r"state", r"config", r"db", r"history", r"\.jsonl?$"]


def _settings(config: Mapping[str, Any]) -> Mapping[str, Any]:
    rules = config.get("rules", {})
    rules = rules if isinstance(rules, Mapping) else {}
    value = rules.get(RULE, {})
    return value if isinstance(value, Mapping) else {}


def _tmp_paths(settings: Mapping[str, Any], environ: Mapping[str, str]) -> list[Path]:
    configured = settings.get("tmp_paths")
    values = configured if isinstance(configured, list) else ["/tmp", "/var/tmp"]
    tmpdir = environ.get("TMPDIR")
    if tmpdir:
        values = [*values, tmpdir]
    paths: list[Path] = []
    for value in values:
        if isinstance(value, str) and value:
            paths.append(Path(value).expanduser().resolve())
    return paths


def _patterns(settings: Mapping[str, Any]) -> list[re.Pattern[str]]:
    configured = settings.get("persist_patterns")
    values = configured if isinstance(configured, list) else DEFAULT_PATTERNS
    patterns: list[re.Pattern[str]] = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            patterns.append(re.compile(value, re.IGNORECASE))
        except re.error:
            continue
    return patterns


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def process(
    payload: Mapping[str, Any],
    *,
    root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Return a deny only in enforce mode; observe mode records the finding."""
    environment = os.environ if environ is None else environ
    cwd_value = payload.get("cwd")
    cwd = (
        Path(cwd_value).resolve()
        if isinstance(cwd_value, str) and cwd_value
        else Path.cwd()
    )
    paths = common.ensure_layout(root=root, cwd=cwd)
    config = common.read_config(paths["config"])
    settings = _settings(config)
    tool_input = payload.get("tool_input", {})
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    path_value = tool_input.get("file_path")
    if not isinstance(path_value, str) or not path_value:
        return None
    candidate = Path(path_value).expanduser()
    candidate = (
        candidate.resolve() if candidate.is_absolute() else (cwd / candidate).resolve()
    )
    temporary_root = next(
        (
            directory
            for directory in _tmp_paths(settings, environment)
            if _inside(candidate, directory)
        ),
        None,
    )
    if temporary_root is None or not any(
        pattern.search(candidate.name) for pattern in _patterns(settings)
    ):
        return None

    reason = (
        f"AXIOM schema guard: {candidate.name} is under temporary storage; "
        "expected a durable project or plugin data path, actual path is temporary. "
        "Move the persistent artifact to a durable location. "
        "Escape hatch: /axiom:enforce off schema-guard"
    )
    if common.rule_mode(config, RULE) == "observe":
        common.append_ledger(
            paths["ledger"],
            {
                "event": "would_have_blocked",
                "hook": "schema_guard",
                "rule": RULE,
                "basis": "persistent filename in temporary storage",
                "summary": reason,
                "failed": [
                    {
                        "type": "durable_path",
                        "path": str(candidate),
                        "expected": "durable path",
                        "actual": f"temporary path under {temporary_root}",
                    }
                ],
            },
        )
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        payload = payload if isinstance(payload, Mapping) else {}
        response = process(payload)
        if response:
            print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
