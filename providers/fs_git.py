"""Filesystem and command write-verification predicates."""

from __future__ import annotations

import hashlib
import shlex
import subprocess
from pathlib import Path
from typing import Any

from . import VerifyResult, WriteVerifier

_SHELL_METACHARACTERS = frozenset(";|&$`")


class FsGitWriteVerifier(WriteVerifier):
    """Verify filesystem state and freshly re-run commands."""

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        self.config = dict(config or {})

    def verify(self, predicate: dict[str, Any]) -> VerifyResult:
        predicate_type = str(predicate.get("type", predicate.get("kind", "")))
        if predicate_type == "file_exists":
            path = Path(str(predicate.get("path", "")))
            actual = path.exists()
            return VerifyResult(actual, True, actual)
        if predicate_type == "file_contains":
            path = Path(str(predicate.get("path", "")))
            expected = str(predicate.get("text", predicate.get("contains", "")))
            try:
                actual = expected in path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as error:
                return VerifyResult(False, expected, f"read failed: {error}")
            return VerifyResult(actual, expected, actual)
        if predicate_type == "file_changed":
            path = Path(str(predicate.get("path", "")))
            expected = str(
                predicate.get("baseline_hash", predicate.get("baseline", ""))
            )
            try:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as error:
                return VerifyResult(
                    False, f"hash different from {expected}", f"read failed: {error}"
                )
            return VerifyResult(
                actual != expected, f"hash different from {expected}", actual
            )
        if predicate_type == "cmd_succeeds":
            return self._verify_command(predicate)
        return VerifyResult(
            False,
            "file_exists, file_contains, file_changed, or cmd_succeeds",
            f"unsupported predicate type: {predicate_type}",
        )

    def _verify_command(self, predicate: dict[str, Any]) -> VerifyResult:
        raw_command = predicate.get("argv", predicate.get("command"))
        if isinstance(raw_command, str):
            if any(character in raw_command for character in _SHELL_METACHARACTERS):
                return VerifyResult(
                    False, "exit code 0", "rejected shell metacharacter"
                )
            try:
                argv = shlex.split(raw_command)
            except ValueError as error:
                return VerifyResult(
                    False, "exit code 0", f"rejected invalid command: {error}"
                )
        elif isinstance(raw_command, (list, tuple)) and all(
            isinstance(argument, str) for argument in raw_command
        ):
            argv = list(raw_command)
            if any(
                character in argument
                for argument in argv
                for character in _SHELL_METACHARACTERS
            ):
                return VerifyResult(
                    False, "exit code 0", "rejected shell metacharacter"
                )
        else:
            return VerifyResult(
                False, "exit code 0", "rejected command: expected string or argv"
            )
        if not argv:
            return VerifyResult(False, "exit code 0", "rejected empty command")

        timeout = predicate.get("timeout", self.config.get("timeout", 120))
        cwd = predicate.get("cwd", self.config.get("cwd"))
        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=float(timeout),
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired, ValueError) as error:
            return VerifyResult(False, "exit code 0", f"command failed: {error}")
        actual = {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        return VerifyResult(completed.returncode == 0, "exit code 0", actual)
