#!/usr/bin/env python3
"""Detect repeated Bash failure clusters across success and failure events."""

from __future__ import annotations

import json
import re
import shlex
import sys
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import axiom_common as common

RULE = "stuck-search"
VARIABLE_TOKEN = re.compile(r"^(?:\d+|0x[0-9a-f]+)$", re.IGNORECASE)


def normalized_tokens(command: str) -> set[str]:
    """Return a stable token set while collapsing volatile numeric values."""
    try:
        raw = shlex.split(command)
    except ValueError:
        raw = re.findall(r"[A-Za-z0-9_./:=+-]+", command)
    tokens: set[str] = set()
    for token in raw:
        value = token.lower()
        tokens.add("<number>" if VARIABLE_TOKEN.fullmatch(value) else value)
    return tokens


def jaccard(left: set[str], right: set[str]) -> float:
    """Return set Jaccard similarity, treating two empty sets as zero."""
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _settings(config: Mapping[str, Any]) -> Mapping[str, Any]:
    rules = config.get("rules", {})
    rules = rules if isinstance(rules, Mapping) else {}
    value = rules.get(RULE, {})
    return value if isinstance(value, Mapping) else {}


def _number(settings: Mapping[str, Any], name: str, default: float) -> float:
    value = settings.get(name, default)
    return float(value) if isinstance(value, (int, float)) else default


def _command(payload: Mapping[str, Any]) -> str:
    tool_input = payload.get("tool_input", {})
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    value = tool_input.get("command")
    return value if isinstance(value, str) else ""


def _active_clusters(
    state: Mapping[str, Any], *, now: datetime, window: timedelta
) -> list[dict[str, Any]]:
    value = state.get("clusters", [])
    value = value if isinstance(value, list) else []
    active: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        timestamp = common._timestamp(item.get("updated_at"))
        if timestamp is not None and now - timestamp <= window:
            active.append(dict(item))
    return active


def _matching_cluster(
    clusters: list[dict[str, Any]], tokens: set[str], threshold: float
) -> dict[str, Any] | None:
    candidates = [
        (jaccard(set(item.get("tokens", [])), tokens), item)
        for item in clusters
        if isinstance(item.get("tokens"), list)
    ]
    candidates = [item for item in candidates if item[0] >= threshold]
    return max(candidates, key=lambda item: item[0])[1] if candidates else None


def process(
    payload: Mapping[str, Any], *, root: Path | str | None = None
) -> dict[str, Any] | None:
    """Update failure clusters, clear them on success, and emit threshold advice."""
    cwd_value = payload.get("cwd")
    cwd = (
        Path(cwd_value).resolve()
        if isinstance(cwd_value, str) and cwd_value
        else Path.cwd()
    )
    paths = common.ensure_layout(root=root, cwd=cwd)
    config = common.load_hook_config(
        paths["config"], ledger=paths["ledger"], hook="stuck_search"
    ).data
    settings = _settings(config)
    threshold = min(1.0, max(0.0, _number(settings, "jaccard_threshold", 0.4)))
    failure_threshold = max(1, int(_number(settings, "failure_threshold", 3)))
    window_minutes = max(1.0, _number(settings, "window_minutes", 30))
    now = datetime.now(timezone.utc)
    clusters = _active_clusters(
        common.read_json(paths["stuck_search"]),
        now=now,
        window=timedelta(minutes=window_minutes),
    )
    command = _command(payload)
    tokens = normalized_tokens(command)
    if not tokens:
        return None
    matched = _matching_cluster(clusters, tokens, threshold)
    event = payload.get("hook_event_name")

    if event == "PostToolUse":
        if matched is not None:
            clusters.remove(matched)
        common.write_json(paths["stuck_search"], {"clusters": clusters})
        return None
    if event != "PostToolUseFailure":
        return None

    error = common.parse_payload(payload)["error"]
    if matched is None:
        matched = {
            "tokens": sorted(tokens),
            "count": 0,
            "first_at": now.isoformat(),
        }
        clusters.append(matched)
    else:
        matched["tokens"] = sorted(set(matched.get("tokens", [])) | tokens)
    matched["count"] = int(matched.get("count", 0)) + 1
    matched["updated_at"] = now.isoformat()
    matched["last_command"] = command
    matched["last_error"] = error
    common.write_json(paths["stuck_search"], {"clusters": clusters})
    if matched["count"] < failure_threshold:
        return None

    reason = (
        f"AXIOM stuck-search: similar Bash commands failed {matched['count']} times; "
        f"expected progress within {failure_threshold} attempts, actual failures remain clustered. "
        "Stop retrying, inspect the root cause, and search for a verified solution. "
        "Escape hatch: /axiom:enforce off stuck-search"
    )
    if common.rule_mode(config, RULE) == "observe":
        common.append_ledger(
            paths["ledger"],
            {
                "event": "would_have_blocked",
                "hook": "stuck_search",
                "rule": RULE,
                "basis": "repeated similar command failures",
                "summary": reason,
                "failed": [
                    {
                        "type": "failure_cluster",
                        "expected": f"fewer than {failure_threshold} similar failures",
                        "actual": matched["count"],
                    }
                ],
            },
        )
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUseFailure",
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
    except Exception as error:
        # Fail open, but never silently (an invisible fail-open is
        # indistinguishable from a hook that was never installed).
        print(
            f"axiom stuck_search: fail-open ({type(error).__name__}: {error})",
            file=sys.stderr,
        )
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
