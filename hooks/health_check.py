#!/usr/bin/env python3
"""SessionStart health check that reports visible, fail-open warnings."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Iterable

from axiom_common import data_root


def health_issues(
    root: Path,
    *,
    interpreter: Path,
    executables: Iterable[Path] = (),
) -> list[str]:
    """Return interpreter, hook executable, and state-root health failures."""
    issues: list[str] = []
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        issues.append(f"Python interpreter is not executable: {interpreter}")

    for executable in executables:
        if not executable.is_file() or not os.access(executable, os.X_OK):
            issues.append(f"Hook is missing or not executable: {executable.name}")

    probe_path: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        descriptor, probe_name = tempfile.mkstemp(prefix=".axiom-health-", dir=root)
        probe_path = Path(probe_name)
        with os.fdopen(descriptor, "w", encoding="utf-8") as probe:
            probe.write("ok\n")
            probe.flush()
            os.fsync(probe.fileno())
    except OSError as error:
        issues.append(f"Data root is not writable: {root} ({error})")
    finally:
        if probe_path is not None:
            try:
                probe_path.unlink(missing_ok=True)
            except OSError as error:
                issues.append(f"Data-root probe cleanup failed: {error}")
    return issues


def warning_response(issues: list[str]) -> dict[str, object]:
    """Build a SessionStart additionalContext warning without blocking startup."""
    details = "\n".join(f"- {issue}" for issue in issues)
    message = (
        "AXIOM HEALTH WARNING: startup checks failed; Axiom hooks remain fail-open.\n"
        f"{details}\n"
        "Treat enforcement results as unavailable until these issues are fixed."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": message,
        }
    }


def main() -> int:
    hooks_dir = Path(__file__).resolve().parent
    hook_files = [
        hooks_dir / name
        for name in (
            "health_check.py",
            "write_verify.py",
            "schema_guard.py",
            "stuck_search.py",
            "preflight.py",
        )
    ]
    issues = health_issues(
        data_root(),
        interpreter=Path(sys.executable),
        executables=hook_files,
    )
    if issues:
        print(json.dumps(warning_response(issues), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
