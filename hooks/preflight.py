#!/usr/bin/env python3
"""Provide non-blocking preflight advice for irreversible Bash command shapes."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import axiom_common as common

RULE = "preflight"
RM_COMMAND = re.compile(r"(?:^|[;&|]\s*)\s*(?:sudo\s+)?rm\s+[^;&|]+", re.IGNORECASE)
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("git_reset_hard", re.compile(r"\bgit\s+reset\s+--hard\b", re.IGNORECASE)),
    (
        "git_clean_force",
        re.compile(
            r"\bgit\s+clean\s+(?:-[A-Za-z]*f[A-Za-z]*|--force)\b", re.IGNORECASE
        ),
    ),
    ("drop_database", re.compile(r"\bDROP\s+(?:TABLE|DATABASE)\b", re.IGNORECASE)),
    (
        "disk_overwrite",
        re.compile(
            r"(?:\bmkfs(?:\.[A-Za-z0-9_-]+)?\b|\bdd\b[^\n;&|]*\bof\s*=)", re.IGNORECASE
        ),
    ),
    (
        "force_push",
        re.compile(r"\bgit\s+push\b[^\n;&|]*(?:--force\b|-f\b)", re.IGNORECASE),
    ),
]


def _is_tmp(path_value: str, environ: Mapping[str, str]) -> bool:
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        return False
    candidate = path.resolve()
    values = ["/tmp", "/var/tmp"]
    if environ.get("TMPDIR"):
        values.append(environ["TMPDIR"])
    for value in values:
        temporary = Path(value).expanduser().resolve()
        try:
            candidate.relative_to(temporary)
            return True
        except ValueError:
            continue
    return False


def _destructive_rm(command: str, environ: Mapping[str, str]) -> bool:
    for match in RM_COMMAND.finditer(command):
        try:
            argv = shlex.split(match.group(0).lstrip(";&| "))
        except ValueError:
            continue
        if argv and argv[0] == "sudo":
            argv = argv[1:]
        if not argv or argv[0] != "rm":
            continue
        options = [item for item in argv[1:] if item.startswith("-")]
        # Letter matching applies only to short-option bundles: "--force"
        # contains an 'r' but is not recursive, "--verbose" is neither.
        short = [item for item in options if not item.startswith("--")]
        recursive = (
            any("r" in item.lower() for item in short) or "--recursive" in options
        )
        forced = any("f" in item.lower() for item in short) or "--force" in options
        targets = [item for item in argv[1:] if not item.startswith("-")]
        if recursive and forced and any(not _is_tmp(item, environ) for item in targets):
            return True
    return False


def detect_pattern(
    command: str, *, environ: Mapping[str, str] | None = None
) -> str | None:
    """Return the first irreversible command class found in a Bash command."""
    environment = os.environ if environ is None else environ
    if _destructive_rm(command, environment):
        return "rm_recursive"
    for name, pattern in PATTERNS:
        if pattern.search(command):
            return name
    return None


def _settings(config: Mapping[str, Any]) -> Mapping[str, Any]:
    rules = config.get("rules", {})
    rules = rules if isinstance(rules, Mapping) else {}
    value = rules.get(RULE, {})
    return value if isinstance(value, Mapping) else {}


def process(
    payload: Mapping[str, Any],
    *,
    root: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    """Record or inject advisory context without denying the Bash tool call."""
    environment = os.environ if environ is None else environ
    cwd_value = payload.get("cwd")
    cwd = (
        Path(cwd_value).resolve()
        if isinstance(cwd_value, str) and cwd_value
        else Path.cwd()
    )
    paths = common.ensure_layout(root=root, cwd=cwd)
    tool_input = payload.get("tool_input", {})
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    pattern_class = detect_pattern(command, environ=environment)
    if pattern_class is None:
        return None

    config = common.load_hook_config(
        paths["config"], ledger=paths["ledger"], hook="preflight"
    ).data
    settings = _settings(config)
    cooldown_value = settings.get("cooldown_minutes", 10)
    cooldown_minutes = (
        float(cooldown_value) if isinstance(cooldown_value, (int, float)) else 10.0
    )
    cooldown = timedelta(minutes=max(0.0, cooldown_minutes))
    now = datetime.now(timezone.utc)
    state = common.read_json(paths["preflight"])
    seen = state.get("seen", {})
    seen = dict(seen) if isinstance(seen, Mapping) else {}
    last_seen = common._timestamp(seen.get(pattern_class))
    if last_seen is not None and now - last_seen < cooldown:
        return None
    seen[pattern_class] = now.isoformat()
    common.write_json(paths["preflight"], {"seen": seen})

    reason = (
        f"AXIOM preflight: detected irreversible command class {pattern_class}; "
        "expected explicit recovery and scope checks, actual command can destroy state. "
        "Is a current backup or recovery point available? "
        "Is the target and scope exactly verified? "
        "Is there a reversible alternative or dry run? "
        "Fix hint: answer all three before proceeding. "
        "Escape hatch: /axiom:enforce off preflight"
    )
    if common.rule_mode(config, RULE) == "observe":
        common.append_ledger(
            paths["ledger"],
            {
                "event": "would_have_blocked",
                "hook": "preflight",
                "rule": RULE,
                "basis": pattern_class,
                "summary": reason,
                "failed": [
                    {
                        "type": pattern_class,
                        "expected": "recovery and scope checks",
                        "actual": "irreversible command shape",
                    }
                ],
            },
        )
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": reason,
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
