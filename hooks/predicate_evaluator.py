"""Canonical evaluator for Axiom write-verification predicates."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

UNSAFE_COMMAND = re.compile(r"[;|&$`<>\n\r]")
# Windows spells the same tools with a suffix -- `git` resolves to `git.exe`,
# and sys.executable is `python.exe` -- so the allowlist below would reject
# every command on that platform. Only `.exe` is stripped: `.bat` and `.cmd`
# are interpreted by cmd.exe, a wider injection surface than this allowlist is
# meant to admit, so tools that ship only as `.cmd` on Windows (npm, npx, yarn)
# stay unavailable rather than being quietly waved through.
_WINDOWS_EXECUTABLE_SUFFIX = ".exe"
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


def baseline_paths(predicate: object) -> list[str]:
    """Paths this predicate needs snapshotted when the claim is registered.

    Registration and evaluation must agree on exactly this set, so both ask
    here rather than each deciding for itself -- the same reason the snapshot
    goes through ``resolve_target``. A path that is guarded but never
    baselined cannot be proven unchanged, and the evaluator treats that as a
    violation rather than a pass.
    """
    if not isinstance(predicate, Mapping):
        return []
    predicate_type = predicate.get("type")
    if predicate_type == "file_changed":
        path_value = predicate.get("path")
        return [path_value] if isinstance(path_value, str) and path_value else []
    if predicate_type == "cmd_succeeds":
        guards = predicate.get("guard_paths", [])
        if not isinstance(guards, list):
            return []
        return [item for item in guards if isinstance(item, str) and item]
    return []


def guard_paths_error(predicate: Mapping[str, Any]) -> str | None:
    """Reject a `guard_paths` value that cannot be checked, instead of ignoring it.

    ``baseline_paths`` returns an empty list for a malformed value because
    registration has no way to fail. Evaluation does, and must use it: a guard
    written as a bare string, or a list with a non-string in it, would otherwise
    protect nothing while reading exactly like a guarded run. Declaring a guard
    and having it silently not apply is the one outcome worse than not
    declaring one.
    """
    if "guard_paths" not in predicate:
        return None
    guards = predicate.get("guard_paths")
    if not isinstance(guards, list):
        return "guard_paths must be a list of paths"
    if not guards:
        return "guard_paths is empty; remove it or name the paths to guard"
    if not all(isinstance(item, str) and item for item in guards):
        return "guard_paths must contain only non-empty path strings"
    return None


def _guard_violations(
    predicate: Mapping[str, Any], *, cwd: Path, baseline: object
) -> list[str]:
    """Report guarded paths whose content moved between registration and now.

    The literature on coding agents is consistent that the most common way a
    "tests pass" claim is satisfied dishonestly is by changing the tests --
    rewriting a case, mocking the thing under test, weakening an assertion.
    ``cmd_succeeds`` reads an exit code and cannot see any of that.

    It does not need to. Registration already hashes whatever the claim
    declares, so the check is a hash comparison and no model is involved: if
    the command passed but a file the claim named as a guard is not the file
    that was there when the claim was made, the run is not a clean pass and
    the report says which path and how it moved.
    """
    guards = baseline_paths(predicate)
    if not guards:
        return []
    baseline = baseline if isinstance(baseline, Mapping) else {}
    files = baseline.get("files", {})
    files = files if isinstance(files, Mapping) else {}
    violations: list[str] = []
    for path_value in guards:
        before = files.get(path_value)
        if not isinstance(before, Mapping):
            violations.append(
                f"{path_value}: no baseline recorded, cannot be proven unchanged"
            )
            continue
        current = snapshot(resolve_target(cwd, path_value))
        if before.get("exists") and not current.get("exists"):
            violations.append(f"{path_value}: deleted since the claim was registered")
        elif not before.get("exists") and current.get("exists"):
            violations.append(f"{path_value}: created since the claim was registered")
        elif current.get("sha256") != before.get("sha256"):
            violations.append(
                f"{path_value}: content changed since the claim was registered"
            )
    return violations


def _split_command(value: str) -> list[str]:
    """Split a command string the way the running platform's own launcher does.

    shlex implements POSIX word splitting, where a backslash escapes the next
    character. Windows paths are full of backslashes and the interpreter lives
    at a path with a space in it, so shlex turns
    `C:\\Program Files\\Python311\\python.exe -c pass` into fragments that name
    no real executable. CommandLineToArgvW is the function Windows itself uses
    to build argv, so it agrees with what would actually run.
    """
    if sys.platform != "win32":
        return shlex.split(value)

    import ctypes
    from ctypes import wintypes

    count = ctypes.c_int(0)
    parser = ctypes.windll.shell32.CommandLineToArgvW
    parser.restype = ctypes.POINTER(wintypes.LPWSTR)
    pointer = parser(value, ctypes.byref(count))
    if not pointer:
        raise ValueError("command could not be parsed as a Windows command line")
    try:
        return [pointer[index] for index in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(pointer)


def _command_argv(value: Any) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        argv = list(value)
    elif isinstance(value, str) and value and not UNSAFE_COMMAND.search(value):
        argv = _split_command(value)
    else:
        raise ValueError(
            "command must be an argv list or a simple command without shell metacharacters"
        )
    if not argv:
        raise ValueError("command is empty")
    executable = Path(argv[0]).name
    if sys.platform == "win32":
        stem, suffix = os.path.splitext(executable)
        if suffix.lower() == _WINDOWS_EXECUTABLE_SUFFIX:
            executable = stem
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
        guard_error = guard_paths_error(predicate)
        if guard_error is not None:
            return _failed(
                predicate_type,
                "guard_paths is a non-empty list of path strings",
                guard_error,
                cmd=predicate.get("cmd"),
            )
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
                # Without these, text mode decodes with the locale encoding --
                # cp1252 on a Western Windows install -- and any non-Latin-1
                # byte in the command's output raises UnicodeDecodeError inside
                # subprocess' reader thread. The verdict here is the exit code,
                # so output we cannot decode must never decide a predicate.
                encoding="utf-8",
                errors="replace",
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
        evidence: dict[str, Any] = {
            "type": predicate_type,
            "cmd": command,
            "passed": passed,
            "expected": "fresh command exits 0",
            "actual": actual,
        }
        # Only meaningful once the command itself passed: a failing command is
        # already a failure, and saying "and also the tests moved" on top of it
        # buries the thing the reader has to fix.
        if passed:
            violations = _guard_violations(predicate, cwd=cwd, baseline=baseline)
            if violations:
                evidence["passed"] = False
                evidence["expected"] = (
                    "fresh command exits 0 and guarded paths unchanged since registration"
                )
                evidence["actual"] = f"{actual}, but guarded paths moved: " + "; ".join(
                    violations
                )
                evidence["guard_violations"] = violations
        return evidence

    return _failed(
        predicate_type,
        "one supported v1 predicate type",
        "unsupported or malformed predicate",
    )
