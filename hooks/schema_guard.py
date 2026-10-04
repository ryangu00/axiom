#!/usr/bin/env python3
"""Detect persistent file and shell write targets in temporary paths."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import axiom_common as common

RULE = "schema-guard"
DEFAULT_PATTERNS = [r"ledger", r"state", r"config", r"db", r"history", r"\.jsonl?$"]
REDIRECT_TARGET = re.compile(
    r"""(?<![\d&>])>{1,2}(?![>&])\s*((?:"(?:\\.|[^"\\])*"|'[^']*'|\\.|[^\s;&|<>()'"])+)"""
)


def _settings(config: Mapping[str, Any]) -> Mapping[str, Any]:
    rules = config.get("rules", {})
    rules = rules if isinstance(rules, Mapping) else {}
    value = rules.get(RULE, {})
    return value if isinstance(value, Mapping) else {}


def _tmp_paths(settings: Mapping[str, Any], environ: Mapping[str, str]) -> list[Path]:
    return common.temp_roots(settings.get("tmp_paths"), environ=environ)


def _exempt_roots(
    settings: Mapping[str, Any], environ: Mapping[str, str]
) -> list[Path]:
    configured = settings.get("exempt_paths", [])
    values = configured if isinstance(configured, list) else []
    roots = [
        Path(value).expanduser().resolve()
        for value in values
        if isinstance(value, str) and value
    ]
    if hasattr(os, "getuid"):
        roots.extend(
            (directory / f"claude-{os.getuid()}").resolve()
            for directory in common.temp_roots(environ=environ)
        )
    return roots


def _bash_write_targets(command: str) -> list[str]:
    """Extract simple shell targets without evaluating the command."""
    targets: list[str] = []
    # Descriptor numbers must touch the operator; `echo 1 > file` is stdout.
    # One unparsable match (for example an unclosed quote) must not discard
    # the targets already found, so each match is parsed on its own.
    for match in REDIRECT_TARGET.finditer(command):
        try:
            parts = shlex.split(match.group(1))
        except ValueError:
            continue
        if parts and parts[0] and not parts[0].isdigit():
            targets.append(parts[0])
    try:
        tokens = list(shlex.shlex(command, posix=True, punctuation_chars=";&|()<>"))
    except ValueError:
        tokens = []
    operators = {";", "&", "&&", "|", "||", "(", ")", "<", "<<", ">", ">>", "&>", ">&"}
    for index, token in enumerate(tokens):
        previous = tokens[index - 1] if index else ""
        if previous and previous not in {";", "&&", "||", "|", "&", "("}:
            continue
        name = Path(token).name
        if name not in {"tee", "sqlite3"}:
            continue
        for target in tokens[index + 1 :]:
            if target in operators:
                break
            if target.startswith("-"):
                continue
            targets.append(target)
            if name == "sqlite3":
                break
    return targets


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
    """Deny file-tool writes in enforce mode; shell findings only advise."""
    environment = os.environ if environ is None else environ
    cwd_value = payload.get("cwd")
    cwd = (
        Path(cwd_value).resolve()
        if isinstance(cwd_value, str) and cwd_value
        else Path.cwd()
    )
    paths = common.ensure_layout(root=root, cwd=cwd)
    config = common.load_hook_config(
        paths["config"], ledger=paths["ledger"], hook="schema_guard"
    ).data
    settings = _settings(config)
    tool_input = payload.get("tool_input", {})
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    shell = payload.get("tool_name") == "Bash"
    if shell:
        command = tool_input.get("command")
        path_values = _bash_write_targets(command) if isinstance(command, str) else []
    else:
        path_value = tool_input.get("file_path")
        path_values = [path_value] if isinstance(path_value, str) and path_value else []
    temporary_roots = _tmp_paths(settings, environment)
    exempt_roots = _exempt_roots(settings, environment)
    patterns = _patterns(settings)
    for path_value in path_values:
        candidate = Path(path_value).expanduser()
        candidate = (
            candidate.resolve()
            if candidate.is_absolute()
            else (cwd / candidate).resolve()
        )
        if any(_inside(candidate, directory) for directory in exempt_roots):
            continue
        temporary_root = next(
            (
                directory
                for directory in temporary_roots
                if _inside(candidate, directory)
            ),
            None,
        )
        if temporary_root is not None and any(
            pattern.search(candidate.name) for pattern in patterns
        ):
            break
    else:
        return None

    reason = (
        f"AXIOM schema guard: {candidate.name} is under temporary storage; "
        "expected a durable project or plugin data path, actual path is temporary. "
        "Move the persistent artifact to a durable location. "
        f"Escape hatch: {common.escape_hatch(RULE)}"
    )
    if common.rule_mode(config, RULE) == "observe":
        common.append_ledger(
            paths["ledger"],
            {
                "event": "would_have_blocked",
                "hook": "schema_guard",
                "rule": RULE,
                "basis": "shell write target in temporary storage"
                if shell
                else "persistent filename in temporary storage",
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
    if shell:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
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
    except Exception as error:
        # Fail open, but never silently (an invisible fail-open is
        # indistinguishable from a hook that was never installed).
        print(
            f"axiom schema_guard: fail-open ({type(error).__name__}: {error})",
            file=sys.stderr,
        )
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
