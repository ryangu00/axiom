# GOAL: windows-support (worked example)

<!-- This is a completed goal file kept as an example. It lives under
     docs/examples/ with a name that does NOT match `*.goal.md` on purpose:
     SessionStart registers the first `*.goal.md` it finds in the project
     directory, and a goal file at the repository root would re-register a
     claim that runs the full test suite at every Stop of every session that
     opens this repo -- including in observe mode. Copy it next to your own
     project as `<name>.goal.md` when you want it live. -->

This worked goal records the Windows support work completed on 2026-08-25.
Its predicates passed on the author's machine while the CI matrix still
failed on three Python versions. A passing local predicate is not
target-platform acceptance: the suite must run on Windows to verify Windows
support. Predicates constrain what was declared; they do not replace running
the code where it will actually be used.

To register a claim from a goal file by hand (the CLI reads one JSON object
on stdin; goal-file discovery runs when the request carries only `cwd`):

```sh
python3 -c "import json, os; print(json.dumps({'cwd': os.getcwd()}))" | python3 scripts/axiom_cli.py register
python3 -c "import json, os; print(json.dumps({'cwd': os.getcwd()}))" | python3 scripts/axiom_cli.py verify
```

On Windows, use `python` for both the JSON producer and the CLI:

```powershell
python -c "import json, os; print(json.dumps({'cwd': os.getcwd()}))" | python scripts/axiom_cli.py register
python -c "import json, os; print(json.dumps({'cwd': os.getcwd()}))" | python scripts/axiom_cli.py verify
```

`json.dumps` escapes quotes and backslashes in the working directory.

## why each predicate exists

- **tests green** — the Windows port broke 9 tests initially; this checks the
  suite on the machine running the claim. A pass on another platform does not
  establish that the suite passes on Windows.
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
