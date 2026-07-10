#!/usr/bin/env python3
"""Shared state primitives for Axiom Claude Code hooks."""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "v1"


def _canonical(path: Path) -> Path:
    return Path(os.path.realpath(os.path.expanduser(str(path))))


def data_root(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve state root from argv, environment, then the home fallback."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    environment = os.environ if environ is None else environ

    for index, argument in enumerate(arguments):
        if argument == "--data-root" and index + 1 < len(arguments):
            return _canonical(Path(arguments[index + 1]))
        if argument.startswith("--data-root="):
            return _canonical(Path(argument.split("=", 1)[1]))

    configured = environment.get("CLAUDE_PLUGIN_DATA")
    if configured:
        return _canonical(Path(configured))

    resolved_home = Path.home() if home is None else home
    return _canonical(resolved_home / ".axiom")


def _git_value(cwd: Path, argument: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", argument],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None
    value = result.stdout.strip()
    return value or None


def _resolve_git_path(cwd: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return _canonical(candidate)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "worktree"


def project_id(cwd: Path | str | None = None) -> str:
    """Return a stable project id, distinguishing linked Git worktrees."""
    working_directory = _canonical(Path.cwd() if cwd is None else Path(cwd))
    git_dir_value = _git_value(working_directory, "--git-dir")
    common_dir_value = _git_value(working_directory, "--git-common-dir")

    if not git_dir_value or not common_dir_value:
        basis = str(working_directory)
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]

    git_dir = _resolve_git_path(working_directory, git_dir_value)
    common_dir = _resolve_git_path(working_directory, common_dir_value)
    base = hashlib.sha256(str(common_dir).encode("utf-8")).hexdigest()[:12]
    if git_dir == common_dir:
        return base

    top_level_value = _git_value(working_directory, "--show-toplevel")
    worktree_name = (
        _resolve_git_path(working_directory, top_level_value).name
        if top_level_value
        else working_directory.name
    )
    return f"{base}-wt-{_slug(worktree_name)}"


def state_paths(
    *, root: Path | str | None = None, cwd: Path | str | None = None
) -> dict[str, Path]:
    """Return all versioned state paths for one project."""
    resolved_root = data_root() if root is None else _canonical(Path(root))
    version_root = resolved_root / SCHEMA_VERSION
    project_root = version_root / "projects" / project_id(cwd)
    return {
        "data_root": resolved_root,
        "version_root": version_root,
        "project_root": project_root,
        "ledger": project_root / "ledger.jsonl",
        "config": project_root / "config.json",
        "lessons": project_root / "lessons.md",
        "global": version_root / "global.json",
    }


def ensure_layout(
    *, root: Path | str | None = None, cwd: Path | str | None = None
) -> dict[str, Path]:
    """Create the current schema directories without modifying older versions."""
    paths = state_paths(root=root, cwd=cwd)
    paths["project_root"].mkdir(parents=True, exist_ok=True)
    return paths


def append_ledger(path: Path | str, record: Mapping[str, Any]) -> None:
    """Append one complete JSON object while holding an exclusive file lock."""
    ledger_path = Path(path)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    entry = dict(record)
    entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    encoded = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"

    with ledger_path.open("a", encoding="utf-8") as ledger:
        fcntl.flock(ledger.fileno(), fcntl.LOCK_EX)
        try:
            ledger.write(encoded)
            ledger.flush()
            os.fsync(ledger.fileno())
        finally:
            fcntl.flock(ledger.fileno(), fcntl.LOCK_UN)


def write_config(path: Path | str, config: Mapping[str, Any]) -> None:
    """Atomically replace a JSON config using a same-directory temporary file."""
    config_path = Path(path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{config_path.name}.", suffix=".tmp", dir=config_path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            json.dump(config, temporary, ensure_ascii=False, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.rename(temporary_path, config_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def read_config(path: Path | str) -> dict[str, Any]:
    """Read a project config, treating absence or a non-object as empty."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def parse_payload(payload: Mapping[str, Any] | None) -> dict[str, str]:
    """Normalize documented and missing hook payload fields without exceptions."""
    source = payload if isinstance(payload, Mapping) else {}
    event = _text(source.get("hook_event_name"))
    response = source.get("tool_response", {})
    response = response if isinstance(response, Mapping) else {}
    error = source.get("error") if event == "PostToolUseFailure" else ""
    return {
        "event": event,
        "error": _text(error),
        "stdout": _text(response.get("stdout")),
        "stderr": _text(response.get("stderr")),
    }


def rule_mode(config: Mapping[str, Any] | None, rule: str) -> str:
    """Return a rule's mode, defaulting invalid or missing values to observe."""
    source = config if isinstance(config, Mapping) else {}
    rules = source.get("rules", {})
    rules = rules if isinstance(rules, Mapping) else {}
    rule_config = rules.get(rule, {})
    rule_config = rule_config if isinstance(rule_config, Mapping) else {}
    mode = rule_config.get("mode", "observe")
    return mode if mode in {"observe", "enforce"} else "observe"


def record_would_have_blocked(
    ledger: Path | str, *, rule: str, basis: str, summary: str
) -> None:
    """Record an observe-mode decision with its rule, basis, and field summary."""
    append_ledger(
        ledger,
        {
            "event": "would_have_blocked",
            "rule": rule,
            "basis": basis,
            "summary": summary,
        },
    )


def manifest(
    *, root: Path | str | None = None, cwd: Path | str | None = None
) -> dict[str, Any]:
    """Enumerate every runtime path managed by this schema for safe uninstall."""
    paths = state_paths(root=root, cwd=cwd)
    return {
        "schema_version": SCHEMA_VERSION,
        "managed_paths": [
            str(paths["global"]),
            str(paths["ledger"]),
            str(paths["config"]),
            str(paths["lessons"]),
        ],
    }
