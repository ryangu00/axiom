"""Canonical evaluator for Axiom write-verification predicates."""

from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

UNSAFE_COMMAND = re.compile(r"[;|&$`<>\n\r]")
ALLOWED_EXECUTABLES = {
    "cargo",
    "go",
    "git",
    "make",
    "node",
    "npm",
    "npx",
    "pnpm",
    "pytest",
    "ruby",
    "uv",
    "yarn",
}


def resolve_target(cwd: Path, value: Any) -> Path | None:
    """Resolve a predicate or hook path against an explicit cwd."""
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (cwd / path).resolve()


def snapshot(path: Path | None) -> dict[str, Any]:
    """Snapshot a file's existence/content hash.

    Shared by both halves of the ``file_changed`` contract: claim registration
    records the baseline through this function and evaluation records the
    current state through it, so the two sides cannot drift apart.
    """
    if path is None:
        return {"exists": False, "sha256": None, "mtime_ns": None}
    try:
        stat = path.stat()
        digest = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
        return {"exists": True, "sha256": digest, "mtime_ns": stat.st_mtime_ns}
    except OSError:
        return {"exists": False, "sha256": None, "mtime_ns": None}


def _command_argv(value: Any) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        argv = list(value)
    elif isinstance(value, str) and value and not UNSAFE_COMMAND.search(value):
        argv = shlex.split(value)
    else:
        raise ValueError(
            "command must be an argv list or a simple command without shell metacharacters"
        )
    if not argv:
        raise ValueError("command is empty")
    executable = Path(argv[0]).name
    if executable not in ALLOWED_EXECUTABLES and not re.fullmatch(
        r"python[0-9.]*", executable
    ):
        raise ValueError(f"executable is not allowlisted: {executable}")
    return argv


def _failed(
    predicate_type: Any,
    expected: str,
    actual: str,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "type": predicate_type,
        "passed": False,
        "expected": expected,
        "actual": actual,
        **fields,
    }


def evaluate_predicate(
    predicate: object, *, cwd: Path, baseline: object
) -> dict[str, Any]:
    """Evaluate one predicate using explicit cwd and caller-injected baseline."""
    cwd = Path(cwd).resolve()
    if not isinstance(predicate, Mapping):
        return _failed(
            None,
            "well-formed predicate object",
            f"malformed entry ({type(predicate).__name__})",
        )

    predicate_type = predicate.get("type")
    path_value = predicate.get("path")
    target = resolve_target(cwd, path_value)

    if predicate_type == "file_exists":
        if target is None:
            return _failed(
                predicate_type,
                "file exists",
                "missing path",
                path=path_value,
            )
        exists = target.exists()
        return {
            "type": predicate_type,
            "path": path_value,
            "passed": exists,
            "expected": "file exists",
            "actual": "exists" if exists else "missing",
        }

    if predicate_type == "file_contains":
        pattern = predicate.get("pattern")
        if target is None:
            return _failed(
                predicate_type,
                "file contains regex pattern",
                "missing path",
                path=path_value,
            )
        if not isinstance(pattern, str) or not pattern:
            return _failed(
                predicate_type,
                "file contains non-empty regex pattern",
                "missing pattern",
                path=path_value,
            )
        expected = f"contains {pattern!r}"
        try:
            found = re.search(pattern, target.read_text(encoding="utf-8")) is not None
        except re.error as error:
            return _failed(
                predicate_type,
                expected,
                f"invalid pattern: {error}",
                path=path_value,
            )
        except (OSError, UnicodeError) as error:
            return _failed(
                predicate_type,
                expected,
                f"unreadable or missing: {error}",
                path=path_value,
            )
        return {
            "type": predicate_type,
            "path": path_value,
            "passed": found,
            "expected": expected,
            "actual": "pattern found" if found else "pattern absent",
        }

    if predicate_type == "file_changed":
        current = snapshot(target)
        baseline = baseline if isinstance(baseline, Mapping) else {}
        files = baseline.get("files", {})
        files = files if isinstance(files, Mapping) else {}
        before = files.get(path_value, {})
        before = before if isinstance(before, Mapping) else {}
        passed = bool(
            current.get("exists")
            and (
                not before.get("exists")
                or current.get("sha256") != before.get("sha256")
            )
        )
        return {
            "type": predicate_type,
            "path": path_value,
            "passed": passed,
            "expected": "content hash differs from registered baseline",
            "actual": "changed"
            if passed
            else ("unchanged" if current.get("exists") else "missing"),
            "baseline_sha256": before.get("sha256"),
            "current_sha256": current.get("sha256"),
        }

    if predicate_type == "cmd_succeeds":
        command = predicate.get("cmd")
        timeout_value = predicate.get("timeout", 120)
        timeout = timeout_value if isinstance(timeout_value, int) else 120
        timeout = max(1, min(timeout, 600))
        try:
            argv = _command_argv(command)
            completed = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
            )
            passed = completed.returncode == 0
            actual = f"exit {completed.returncode}"
        except subprocess.TimeoutExpired:
            passed = False
            actual = f"timeout after {timeout}s"
        except (OSError, ValueError) as error:
            passed = False
            actual = f"rejected or unavailable: {error}"
        return {
            "type": predicate_type,
            "cmd": command,
            "passed": passed,
            "expected": "fresh command exits 0",
            "actual": actual,
        }

    return _failed(
        predicate_type,
        "one supported v1 predicate type",
        "unsupported or malformed predicate",
    )
