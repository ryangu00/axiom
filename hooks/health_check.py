#!/usr/bin/env python3
"""SessionStart health check that reports visible, fail-open warnings."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Iterable
from pathlib import Path

from axiom_common import data_root


def health_issues(
    root: Path,
    *,
    interpreter: Path,
    executables: Iterable[Path] = (),
    ledger: Path | None = None,
) -> list[str]:
    """Return interpreter, hook executable, and state-root health failures."""
    issues: list[str] = []
    # A ledger that exists but cannot be appended means observe-mode findings
    # are being dropped while everything else looks healthy — the worst
    # possible silent state for a tool whose default mode is "record".
    if ledger is not None and ledger.is_file() and not os.access(ledger, os.W_OK):
        issues.append(f"Ledger exists but is not writable: {ledger}")
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        issues.append(f"Python interpreter is not executable: {interpreter}")

    # hooks.json invokes every hook as `python3 <file>`, so the runtime
    # requirement is readability, not an exec bit (which zip distribution or
    # a wide umask can strip without breaking anything).
    for executable in executables:
        if not executable.is_file() or not os.access(executable, os.R_OK):
            issues.append(f"Hook is missing or not readable: {executable.name}")

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
            # Load-bearing non-entry modules: axiom_common is also the
            # registered SessionStart hook, predicate_evaluator is the single
            # source of predicate semantics.
            "axiom_common.py",
            "predicate_evaluator.py",
        )
    ]
    # SessionStart delivers the project cwd on stdin; use it to probe the
    # project's own ledger, not just the shared data root.
    ledger: Path | None = None
    try:
        payload = json.load(sys.stdin)
        cwd_value = payload.get("cwd") if isinstance(payload, dict) else None
        if isinstance(cwd_value, str) and cwd_value:
            from axiom_common import state_paths

            ledger = state_paths(cwd=Path(cwd_value))["ledger"]
    except Exception:
        ledger = None  # health checking must never crash the session
    issues = health_issues(
        data_root(),
        interpreter=Path(sys.executable),
        executables=hook_files,
        ledger=ledger,
    )
    if issues:
        print(json.dumps(warning_response(issues), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
