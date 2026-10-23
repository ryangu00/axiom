#!/usr/bin/env python3
"""Detect repeated Bash failure clusters across success and failure events."""

from __future__ import annotations

import hashlib
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
DEFAULT_POLLING_PATTERNS = [
    r"^\s*until\b[\s\S]*?\bdo\b",
    r"^\s*while\b[\s\S]*?\bdo\b[\s\S]*?\bsleep\s",
    r"^\s*tail\s+-f(?:\s|$)",
    r"^\s*watch\s",
]
DEFAULT_SEARCH_TOOLS = ["WebSearch", "WebFetch", r"mcp__.*(?:search|fetch).*"]
# A command that exits 0 can still have failed: `pytest | tail -20` reports
# tail's status. These anchored signatures, looked for in the tail of the
# output, are what test runners and shells print when they fail.
MASKED_SCAN_CHARS = 6000
MASKED_FAILURE_PATTERNS = [
    re.compile(r"^Traceback \(most recent call last\):", re.MULTILINE),
    re.compile(r"^(?:FAILED|FAIL)\b|^--- FAIL:", re.MULTILINE),
    re.compile(r"\b[1-9]\d* (?:failed|failing)\b"),
    re.compile(r"\btest result: FAILED\b"),
    re.compile(r"^error\[E\d+\]", re.MULTILINE),
    re.compile(r"^npm ERR!", re.MULTILINE),
    # bash `x: command not found`, zsh `zsh: command not found: x`,
    # dash `sh: 1: x: not found`.
    re.compile(
        r": command not found$|^zsh: command not found: |^\S+: \d+: \S+: not found$",
        re.MULTILINE,
    ),
]
# Commands that print what already exists (a file, a log, history) or only
# change shell state. A failure signature in their output describes some
# earlier run, so a command made only of these is never scanned.
_READERS = frozenset(
    {
        "cat", "bat", "less", "more", "head", "tail", "grep", "egrep", "fgrep",
        "rg", "ag", "ack", "sed", "awk", "jq", "diff", "echo", "printf", "ls",
        "find", "wc", "sort", "uniq", "cut", "tr", "column", "xxd", "od",
        "strings", "journalctl", "dmesg", "cd", "pushd", "popd", "export",
        "set", "unset", "true", "false", ":", "sleep", "[", "[[", "test",
        "pwd", "which", "type", "man", "stat", "file", "du", "df", "tree",
        "realpath", "dirname", "basename", "date", "printenv", "tac", "nl",
        # Clients that print what a server already holds: CI logs, issues,
        # pull requests, pages. Their failures are someone else's run.
        "gh", "glab", "curl", "wget", "http", "xh",
        # git prints history, other people's messages and its own subject
        # lines ("Fix 2 failing tests"); a failing hook makes git exit
        # non-zero, which reaches PostToolUseFailure instead.
        "git",
    }
)  # fmt: skip
# Subcommands that print logs or state of containers and pods.
_CLUSTER_READERS = {
    "docker": frozenset({"logs", "inspect", "ps", "images", "events"}),
    "podman": frozenset({"logs", "inspect", "ps", "images", "events"}),
    "kubectl": frozenset({"logs", "describe", "get", "events"}),
}
_CLUSTER_VALUE_OPTIONS = frozenset(
    {"-n", "--namespace", "--context", "--kubeconfig", "-H", "--host", "--config"}
)
# Error signatures: the last line naming an error, else the first line that is
# not just an exit status, with volatile numbers collapsed. Stored hashed.
_EXIT_STATUS_LINE = re.compile(
    r"^(?:exit (?:code|status)|exited with(?: code)?)\b[\s:]*-?\d+$", re.IGNORECASE
)
_ERROR_TOKEN = re.compile(
    r"\b[A-Za-z_][\w.]*(?:Error|Exception|Panic)\b|\bFAILED\b|\berror\[E\d+\]"
    r"|npm ERR!|command not found"
)
_VOLATILE = re.compile(r"0x[0-9a-f]+|\d+", re.IGNORECASE)
MAX_ERROR_SIGNATURES = 16


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


def _patterns(
    settings: Mapping[str, Any], name: str, default: list[str]
) -> list[re.Pattern[str]]:
    configured = settings.get(name)
    values = configured if isinstance(configured, list) else default
    patterns: list[re.Pattern[str]] = []
    for value in values:
        if not isinstance(value, str):
            continue
        try:
            patterns.append(re.compile(value, re.IGNORECASE))
        except re.error:
            continue
    return patterns


def _polling_patterns(settings: Mapping[str, Any]) -> list[re.Pattern[str]]:
    # A leading sleep is deliberately not a default: it can hide a real failure.
    return _patterns(settings, "polling_patterns", DEFAULT_POLLING_PATTERNS)


def _pending_searches(state: Mapping[str, Any], now: datetime) -> list[dict[str, Any]]:
    value = state.get("pending_searches", [])
    value = value if isinstance(value, list) else []
    pending: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        timestamp = common._timestamp(item.get("advice_at"))
        if timestamp is not None and timedelta(0) <= now - timestamp <= timedelta(
            minutes=30
        ):
            pending.append(dict(item))
    return pending


def process_search_event(
    payload: Mapping[str, Any], *, root: Path | str | None = None
) -> None:
    """Record the first successful search after the latest advice in a session."""
    if payload.get("hook_event_name") != "PostToolUse":
        return
    tool = payload.get("tool_name")
    if not isinstance(tool, str):
        return
    cwd_value = payload.get("cwd")
    cwd = (
        Path(cwd_value).resolve()
        if isinstance(cwd_value, str) and cwd_value
        else Path.cwd()
    )
    paths = common.ensure_layout(root=root, cwd=cwd)
    config = common.hook_config(paths, payload, "stuck_search").data
    if not any(
        pattern.fullmatch(tool)
        for pattern in _patterns(
            _settings(config), "search_tools", DEFAULT_SEARCH_TOOLS
        )
    ):
        return
    now = datetime.now(timezone.utc)
    state = common.read_json(paths["stuck_search"])
    pending = _pending_searches(state, now)
    session_id = payload.get("session_id", "")
    for item in pending:
        if item.get("session_id") != session_id:
            continue
        timestamp = common._timestamp(item["advice_at"])
        assert timestamp is not None
        record: dict[str, Any] = {
            "event": "search_after_trigger",
            "hook": "stuck_search",
            "rule": RULE,
            "lag_seconds": (now - timestamp).total_seconds(),
        }
        if isinstance(item.get("finding_id"), str):
            record["finding_id"] = item["finding_id"]
        common.append_ledger(paths["ledger"], record)
        pending.remove(item)
        state["pending_searches"] = pending
        _purge_legacy(state)
        common.write_json(paths["stuck_search"], state)
        return


def _cluster_finding_id(cluster: Mapping[str, Any]) -> str:
    """One id per failure cluster: the rule and the moment the cluster opened.

    The cluster's latest tokens move as it absorbs commands, so they cannot
    name it; `first_at` never changes. CONTRACTS §6: identity, never evidence.
    """
    return common.finding_id(RULE, str(cluster.get("first_at", "")))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def token_digests(command: str) -> set[str]:
    """Fingerprint each normalized token so the state file holds no command text.

    Jaccard over digests equals Jaccard over the tokens (barring a 64-bit
    collision). A fingerprint is not encryption: a guessable token can be
    confirmed by hashing the guess.
    """
    return {_digest(token) for token in normalized_tokens(command)}


def error_signature(text: str) -> str | None:
    """Fingerprint what a failure said, or None when it said nothing usable."""
    lines = [line.strip() for line in text[-MASKED_SCAN_CHARS:].splitlines()]
    lines = [line for line in lines if line and not _EXIT_STATUS_LINE.match(line)]
    if not lines:
        return None
    named = [line for line in lines if _ERROR_TOKEN.search(line)]
    line = named[-1] if named else lines[0]
    return _digest(" ".join(_VOLATILE.sub("#", line.lower()).split())[:200])


def _command(payload: Mapping[str, Any]) -> str:
    tool_input = payload.get("tool_input", {})
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    value = tool_input.get("command")
    return value if isinstance(value, str) else ""


def _count(cluster: Mapping[str, Any]) -> int:
    value = cluster.get("count", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _digest_list(value: Any) -> bool:
    """A cluster's `seed`/`tokens`: a list of digest strings, nothing else."""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _purge_legacy(state: dict[str, Any]) -> None:
    """Drop clusters older versions wrote, with raw command and error text."""
    value = state.get("clusters")
    if isinstance(value, list):
        state["clusters"] = [
            item
            for item in value
            if isinstance(item, Mapping)
            and _digest_list(item.get("seed"))
            and _digest_list(item.get("tokens"))
        ]


def _active_clusters(
    state: Mapping[str, Any], *, now: datetime, window: timedelta
) -> tuple[list[dict[str, Any]], bool]:
    """Return the clusters still inside the window, and whether any were dropped.

    Clusters without a `seed` were written by versions that stored raw command
    and error text; they are dropped so the next write purges that text.
    """
    value = state.get("clusters", [])
    value = value if isinstance(value, list) else []
    active: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        if not _digest_list(item.get("seed")) or not _digest_list(item.get("tokens")):
            continue
        timestamp = common._timestamp(item.get("updated_at"))
        if timestamp is not None and now - timestamp <= window:
            active.append(dict(item))
    return active, len(active) != len(value)


def _matching_cluster(
    clusters: list[dict[str, Any]], digests: set[str], threshold: float
) -> dict[str, Any] | None:
    """Match against the cluster's first command and its latest one.

    Comparing with an ever-growing union would let a long cluster drift until
    the command that keeps failing no longer resembles it.
    """
    best: tuple[float, dict[str, Any]] | None = None
    for item in clusters:
        score = max(
            jaccard(set(item["seed"]), digests), jaccard(set(item["tokens"]), digests)
        )
        if score >= threshold and (best is None or score > best[0]):
            best = (score, item)
    return best[1] if best is not None else None


def _is_reader(argv: list[str]) -> bool:
    name, arguments = common.command_name(argv)
    if name in _CLUSTER_READERS:
        words: list[str] = []
        index = 0
        while index < len(arguments) and len(words) < 2:
            word = arguments[index]
            if word.startswith("-"):
                index += 2 if word in _CLUSTER_VALUE_OPTIONS else 1
                continue
            words.append(word)
            index += 1
        # `docker compose logs`, `docker container logs`: the verb is second.
        if words[:1] in (["compose"], ["container"]):
            words = words[1:]
        return bool(words) and words[0] in _CLUSTER_READERS[name]
    return name == "" or name in _READERS


def _only_readers(shape: common.ShellShape, depth: int = 0) -> bool:
    """Every command a shape runs is a reader, its substitutions included:
    `out=$(pytest); echo "$out"` runs pytest."""
    if not all(_is_reader(argv) for argv in shape.commands):
        return False
    for script in shape.substitutions:
        inner = common.shell_shape(script)
        if inner is None or depth >= 2 or not _only_readers(inner, depth + 1):
            return False
    return True


def masked_failure(command: str, stdout: str, stderr: str) -> str | None:
    """Return the scanned output when a command that exited 0 shows a failure.

    Commands made only of readers (`cat`, `grep`, `git log`, `cd` ...) are
    skipped: what they print was already there. A command that does not
    tokenize is skipped too; its shape is unknown.
    """
    shape = common.shell_shape(command)
    if shape is None or _only_readers(shape):
        return None
    text = f"{stdout[-MASKED_SCAN_CHARS:]}\n{stderr[-MASKED_SCAN_CHARS:]}"
    text = text.replace("\r\n", "\n")
    if any(pattern.search(text) for pattern in MASKED_FAILURE_PATTERNS):
        return text
    return None


def success_is_evidence(command: str) -> bool:
    """Whether an exit status of 0 says the whole command succeeded.

    Only for a single command or an `&&` chain. A pipeline reports its last
    stage, `;` and newlines report the last command, `||` reports a fallback,
    and `&` reports nothing.
    """
    shape = common.shell_shape(command)
    return shape is not None and set(shape.operators) <= {"&&"}


def process(
    payload: Mapping[str, Any], *, root: Path | str | None = None
) -> dict[str, Any] | None:
    """Count failures (exit status or masked) into clusters; decay on success."""
    event = payload.get("hook_event_name")
    if event not in ("PostToolUse", "PostToolUseFailure"):
        return None
    if event == "PostToolUseFailure" and payload.get("is_interrupt") is True:
        return None
    command = _command(payload)
    digests = token_digests(command)
    if not digests:
        return None
    cwd_value = payload.get("cwd")
    cwd = (
        Path(cwd_value).resolve()
        if isinstance(cwd_value, str) and cwd_value
        else Path.cwd()
    )
    paths = common.ensure_layout(root=root, cwd=cwd)
    config = common.hook_config(paths, payload, "stuck_search").data
    settings = _settings(config)
    threshold = min(1.0, max(0.0, _number(settings, "jaccard_threshold", 0.4)))
    failure_threshold = max(1, int(_number(settings, "failure_threshold", 3)))
    window_minutes = max(1.0, _number(settings, "window_minutes", 30))
    now = datetime.now(timezone.utc)
    state = common.read_json(paths["stuck_search"])
    clusters, dropped = _active_clusters(
        state,
        now=now,
        window=timedelta(minutes=window_minutes),
    )
    matched = _matching_cluster(clusters, digests, threshold)
    state["clusters"] = clusters
    polling = any(pattern.search(command) for pattern in _polling_patterns(settings))
    fields = common.parse_payload(payload)

    if event == "PostToolUse":
        tool_input = payload.get("tool_input", {})
        if (
            isinstance(tool_input, Mapping)
            and tool_input.get("run_in_background") is True
        ):
            # The tool returned at launch: exit status and output say nothing
            # about the command yet.
            if dropped:
                common.write_json(paths["stuck_search"], state)
            return None
        masked = (
            None
            if polling
            else masked_failure(command, fields["stdout"], fields["stderr"])
        )
        if masked is None:
            if matched is not None and success_is_evidence(command):
                # One success is evidence, not proof: a passing neighbour of
                # a command that keeps failing must not wipe the cluster.
                matched["count"] = _count(matched) - 1
                if matched["count"] <= 0:
                    clusters.remove(matched)
                dropped = True
            if dropped:
                common.write_json(paths["stuck_search"], state)
            return None
        failure_text = masked
    else:
        if polling:
            return None
        failure_text = fields["error"] or f"{fields['stderr']}\n{fields['stdout']}"

    if matched is None:
        matched = {"seed": sorted(digests), "count": 0, "first_at": now.isoformat()}
        clusters.append(matched)
    matched["tokens"] = sorted(digests)
    matched["count"] = _count(matched) + 1
    matched["updated_at"] = now.isoformat()
    stored = matched.get("errors")
    errors = (
        [item for item in stored if isinstance(item, str)]
        if isinstance(stored, list)
        else []
    )
    signature = error_signature(failure_text)
    if signature and signature not in errors and len(errors) < MAX_ERROR_SIGNATURES:
        errors.append(signature)
    matched["errors"] = errors
    matched.setdefault("first_at", now.isoformat())
    finding = _cluster_finding_id(matched)
    common.write_json(paths["stuck_search"], state)
    if matched["count"] < failure_threshold:
        return None

    masked_run = event == "PostToolUse"
    reason = (
        f"AXIOM stuck-search: similar Bash commands failed {matched['count']} times"
        f"{' (this run exited 0, but its output shows a failure)' if masked_run else ''}; "
        f"expected progress within {failure_threshold} attempts, actual failures remain clustered. "
        "Stop retrying, inspect the root cause, and search for a verified solution. "
        f"Escape hatch: {common.escape_hatch(RULE)}"
    )
    if common.rule_mode(config, RULE) == "observe":
        common.append_ledger(
            paths["ledger"],
            {
                "event": "would_have_blocked",
                "hook": "stuck_search",
                "rule": RULE,
                "finding_id": finding,
                "basis": "repeated similar command failures",
                "summary": reason,
                "masked_failure": masked_run,
                "failed": [
                    {
                        "type": "failure_cluster",
                        "expected": f"fewer than {failure_threshold} similar failures",
                        "actual": matched["count"],
                        "distinct_errors": len(errors),
                    }
                ],
            },
        )
        return None
    cooldown = timedelta(minutes=max(0.0, _number(settings, "cooldown_minutes", 10)))
    last_advice = common._timestamp(matched.get("last_advice_at"))
    if last_advice is not None and now - last_advice < cooldown:
        return None
    matched["last_advice_at"] = now.isoformat()
    session_id = payload.get("session_id", "")
    pending = [
        item
        for item in _pending_searches(state, now)
        if item.get("session_id") != session_id
    ]
    pending.append(
        {"session_id": session_id, "advice_at": now.isoformat(), "finding_id": finding}
    )
    state["pending_searches"] = pending
    common.write_json(paths["stuck_search"], state)
    # Best-effort (CONTRACTS §6): the cooldown is already spent, so a failed
    # append must not also cost the agent the advice it was about to get.
    common.record_best_effort(
        paths["ledger"],
        {
            "event": "advice_injected",
            "hook": "stuck_search",
            "rule": RULE,
            "finding_id": finding,
            "count": matched["count"],
            "distinct_errors": len(errors),
            "masked_failure": masked_run,
        },
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": reason,
        }
    }


def main() -> int:
    payload: Any = {}
    try:
        payload = json.load(sys.stdin)
        payload = payload if isinstance(payload, Mapping) else {}
        if "--search-event" in sys.argv:
            process_search_event(payload)
            return 0
        response = process(payload)
        if response:
            print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    except Exception as error:
        # Fail open, but never silently (an invisible fail-open is
        # indistinguishable from a hook that was never installed).
        return common.fail_open_main("stuck_search", RULE, error, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
