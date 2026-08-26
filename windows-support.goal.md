# GOAL: windows-support

Evidence that Axiom now runs on Windows with full test coverage. This goal
verifies the platform support work completed in 2026-08-25.

Register this claim before pushing the Windows support changes:

    python scripts/axiom_cli.py register < windows-support.goal.md
    python scripts/axiom_cli.py verify

## why each predicate exists

- **tests green** — the Windows port broke 9 tests initially; this confirms
  all platform-specific fixes are in place and the suite passes on Windows.
- **Windows locking code** — the `msvcrt.locking` branch is the core of the
  Windows port; without it, the hooks fail at import on Windows.
- **Windows command parsing** — `CommandLineToArgvW` is required because
  `shlex.split` cannot handle Windows paths with spaces and backslashes.
- **CI includes Windows** — the windows-latest runner prevents future regressions.
- **docs updated** — KNOWN-LIMITATIONS.md and README must reflect the new
  platform support; stale documentation is a form of technical debt.

## acceptance
```json
[
  {"type": "cmd_succeeds", "cmd": ["python", "-m", "unittest", "discover", "-s", "tests", "-v"], "timeout": 300},
  {"type": "file_contains", "path": "hooks/axiom_common.py", "pattern": "msvcrt.locking"},
  {"type": "file_contains", "path": "hooks/predicate_evaluator.py", "pattern": "CommandLineToArgvW"},
  {"type": "file_contains", "path": ".github/workflows/ci.yml", "pattern": "windows-latest"},
  {"type": "file_contains", "path": "docs/KNOWN-LIMITATIONS.md", "pattern": "Windows-specific notes"},
  {"type": "file_contains", "path": "README.md", "pattern": "Linux, macOS, and Windows"}
]
```
