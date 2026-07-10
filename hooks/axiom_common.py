#!/usr/bin/env python3
"""Shared state primitives for Axiom Claude Code hooks."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
        "active_claim": project_root / "claims" / "active.json",
        "stuck_search": project_root / "stuck-search.json",
        "preflight": project_root / "preflight.json",
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


def read_json(path: Path | str) -> dict[str, Any]:
    """Read an object-shaped JSON state file, returning empty state on failure."""
    return read_config(path)


def write_json(path: Path | str, payload: Mapping[str, Any]) -> None:
    """Atomically write an object-shaped JSON state file."""
    write_config(path, payload)


@contextlib.contextmanager
def _claim_lock(active_path: Path):
    """Exclusive per-project lock covering register/clear of the active claim so
    a concurrent session cannot slip a new claim between our compare and our
    unlink (closes the compare-and-clear TOCTOU). Degrades to no-lock on
    filesystems without flock support (documented limitation), never wedging."""
    lock_path = active_path.parent / "claim.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock_path, "w")
    try:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def read_ledger(path: Path | str) -> list[dict[str, Any]]:
    """Read valid object records from a JSONL ledger."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _file_snapshot(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
        if not path.is_file():
            return {"exists": True, "sha256": None, "mtime_ns": stat.st_mtime_ns}
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return {"exists": True, "sha256": digest, "mtime_ns": stat.st_mtime_ns}
    except OSError:
        return {"exists": False, "sha256": None, "mtime_ns": None}


def register_claim(
    claim: Mapping[str, Any],
    *,
    root: Path | str | None = None,
    cwd: Path | str | None = None,
) -> dict[str, Any]:
    """Register a claim and capture baselines for declared changed files."""
    working_directory = _canonical(Path.cwd() if cwd is None else Path(cwd))
    registered = dict(claim)
    predicates = registered.get("predicates", [])
    predicates = predicates if isinstance(predicates, list) else []
    files: dict[str, dict[str, Any]] = {}
    for predicate in predicates:
        if (
            not isinstance(predicate, Mapping)
            or predicate.get("type") != "file_changed"
        ):
            continue
        path_value = predicate.get("path")
        if not isinstance(path_value, str) or not path_value:
            continue
        target = Path(path_value)
        if not target.is_absolute():
            target = working_directory / target
        files[path_value] = _file_snapshot(_canonical(target))
    registered["predicates"] = [
        dict(item) if isinstance(item, Mapping) else item for item in predicates
    ]
    registered["baseline"] = {
        "git_head": _git_value(working_directory, "HEAD"),
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    paths = ensure_layout(root=root, cwd=working_directory)
    with _claim_lock(paths["active_claim"]):
        write_json(paths["active_claim"], registered)
    return registered


def read_active_claim(
    *, root: Path | str | None = None, cwd: Path | str | None = None
) -> dict[str, Any] | None:
    """Read the active claim for the current project or worktree."""
    claim = read_json(state_paths(root=root, cwd=cwd)["active_claim"])
    return claim or None


def clear_active_claim(
    *,
    root: Path | str | None = None,
    cwd: Path | str | None = None,
    expected_registered_at: str | None = None,
) -> bool:
    """Remove the active claim after all declared predicates verify.

    Compare-and-clear guard against a cross-session TOCTOU: if
    ``expected_registered_at`` is given, only clear when the claim currently on
    disk still carries that registration timestamp. A different value means
    another session replaced the claim between our read and this clear, so we
    leave it untouched. Returns True if a claim was cleared, False otherwise.
    """
    active_path = state_paths(root=root, cwd=cwd)["active_claim"]
    with _claim_lock(active_path):
        if expected_registered_at is not None:
            current = read_json(active_path) or {}
            current_token = (current.get("baseline") or {}).get("registered_at")
            if current_token != expected_registered_at:
                return False
        try:
            active_path.unlink(missing_ok=True)
        except OSError:
            raise
        return True


def register_goal_claim(
    *, root: Path | str | None = None, cwd: Path | str | None = None
) -> dict[str, Any] | None:
    """Register predicates from the first local goal acceptance JSON block."""
    working_directory = _canonical(Path.cwd() if cwd is None else Path(cwd))
    if read_active_claim(root=root, cwd=working_directory) is not None:
        return None
    for goal_path in sorted(working_directory.glob("*.goal.md")):
        try:
            text = goal_path.read_text(encoding="utf-8")
        except OSError:
            continue
        section = re.search(
            r"^##\s+acceptance\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
            text,
            re.IGNORECASE | re.MULTILINE | re.DOTALL,
        )
        if section is None:
            continue
        body = section.group("body")
        fenced = re.search(r"```(?:json)?\s*(?P<json>.*?)\s*```", body, re.DOTALL)
        candidates = [fenced.group("json")] if fenced else []
        candidates.extend(
            line.lstrip()[2:].strip()
            for line in body.splitlines()
            if line.lstrip().startswith("- {")
        )
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, list):
                claim: dict[str, Any] = {"predicates": parsed}
            elif isinstance(parsed, dict):
                claim = parsed
            else:
                continue
            predicates = claim.get("predicates")
            if not isinstance(predicates, list) or not predicates:
                continue
            claim.setdefault("label", goal_path.name.removesuffix(".goal.md"))
            return register_claim(claim, root=root, cwd=working_directory)
    return None


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


def record_heartbeat(ledger: Path | str) -> None:
    """Record one SessionStart heartbeat for coverage reporting."""
    append_ledger(ledger, {"event": "heartbeat", "hook": "session_start"})


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def get_report_data(ledger: Path | str) -> dict[str, Any]:
    """Return rule findings, recent scenes, and hook coverage data."""
    records = read_ledger(ledger)
    rules: dict[str, dict[str, Any]] = {}
    hook_last_active: dict[str, str] = {}
    heartbeats: list[datetime] = []
    for record in records:
        timestamp = _timestamp(record.get("timestamp"))
        hook = record.get("hook")
        if isinstance(hook, str) and timestamp is not None:
            previous = _timestamp(hook_last_active.get(hook))
            if previous is None or timestamp > previous:
                hook_last_active[hook] = timestamp.isoformat()
        if record.get("event") == "heartbeat" and timestamp is not None:
            heartbeats.append(timestamp)
        rule = record.get("rule")
        event = record.get("event")
        if not isinstance(rule, str) or not isinstance(event, str):
            continue
        group = rules.setdefault(rule, {"recent": []})
        group[event] = int(group.get(event, 0)) + 1
        if event == "would_have_blocked":
            claim = record.get("claim", {})
            claim = claim if isinstance(claim, Mapping) else {}
            failed = record.get("failed", [])
            failed = failed if isinstance(failed, list) else []
            group["recent"].append(
                {
                    "timestamp": record.get("timestamp", ""),
                    "claim_label": _text(claim.get("label")),
                    "failed": failed,
                }
            )
            group["recent"] = group["recent"][-3:]
    heartbeat_days = 0
    if heartbeats:
        heartbeat_days = (max(heartbeats).date() - min(heartbeats).date()).days
    return {
        "rules": rules,
        "coverage": {
            "heartbeat_days": heartbeat_days,
            "event_count": len(records),
            "hook_last_active": hook_last_active,
        },
    }


def calibration_notice(report: Mapping[str, Any]) -> str:
    """Return the observe-phase conclusion prompt when calibration is mature."""
    rules = report.get("rules", {})
    rules = rules if isinstance(rules, Mapping) else {}
    finding_count = 0
    threshold_met = False
    for group in rules.values():
        if not isinstance(group, Mapping):
            continue
        count = group.get("would_have_blocked", 0)
        count = count if isinstance(count, int) else 0
        finding_count += count
        threshold_met = threshold_met or count >= 3
    coverage = report.get("coverage", {})
    coverage = coverage if isinstance(coverage, Mapping) else {}
    days = coverage.get("heartbeat_days", 0)
    days = days if isinstance(days, int) else 0
    if not threshold_met and days < 7:
        return ""
    return (
        f"axiom observe 期已积累 {finding_count} 条发现,跑 /axiom:report 查看,"
        "/axiom:enforce <rule> 启用拦截"
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
            str(paths["active_claim"]),
            str(paths["stuck_search"]),
            str(paths["preflight"]),
        ],
    }


def session_start_main() -> int:
    """Record a heartbeat and emit a calibration notice when one is due."""
    try:
        payload = json.load(sys.stdin)
        payload = payload if isinstance(payload, Mapping) else {}
    except (json.JSONDecodeError, OSError):
        payload = {}
    cwd_value = payload.get("cwd")
    cwd = Path(cwd_value) if isinstance(cwd_value, str) and cwd_value else Path.cwd()
    try:
        paths = ensure_layout(cwd=cwd)
        register_goal_claim(cwd=cwd)
        record_heartbeat(paths["ledger"])
        notice = calibration_notice(get_report_data(paths["ledger"]))
        if notice:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "SessionStart",
                            "additionalContext": notice,
                        }
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(session_start_main())
