#!/usr/bin/env python3
"""Codex host shim for the Axiom adapter CLI (`axiom-adapter-cli/v1`).

Codex CLI (>= 0.144.1) fires Claude-Code-shaped lifecycle hooks and honors the
same decision protocol (verified by the p1 Stop-block probe: a `Stop` hook that
returns `{"decision":"block","reason"}` re-enters the turn, and `{}` lets it
stop). So the Codex adapter is a thin translation layer, not a new engine:

    SessionStart  -> `axiom register`   (goal-file discovery; never blocks)
    Stop          -> `axiom verify`     (map CLI outcome -> Codex decision)

Per CONTRACTS.md §5 the CLI returns *facts only* (`passed|failed|...`); host
semantics (`decision:block`) live here in the shim. Everything fails open: any
error, missing CLI, bad JSON, or nonzero exit lets the agent proceed. A
verifier that can wedge its host is worse than no verifier.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Dev fallback only: valid when the shim runs from inside the repo tree. Codex
# COPIES the plugin into its cache on install, detaching it from the repo, so
# installed use must set AXIOM_CLI (or have `axiom` on PATH). See README.
_DEV_CLI = Path(__file__).resolve().parents[3] / "scripts" / "axiom_cli.py"


def _cli_path() -> Path:
    override = os.environ.get("AXIOM_CLI")
    if override:
        return Path(override)
    found = shutil.which("axiom")
    if found:
        return Path(found)
    return _DEV_CLI


def _read_payload() -> dict:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _cwd(payload: dict) -> str:
    value = payload.get("cwd")
    if isinstance(value, str) and value:
        return str(Path(value).expanduser())
    return os.getcwd()


def _fail_open(reason: str) -> None:
    """Emit the neutral 'do nothing' hook response and log why to stderr."""
    print(f"axiom codex adapter: {reason}", file=sys.stderr)
    print("{}")


def _observe_event(name: str, **fields: object) -> None:
    """Best-effort observable event: stderr always, data-root jsonl if present."""
    record = {"event": name, "ts": datetime.now(timezone.utc).isoformat(), **fields}
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    print(f"axiom codex adapter event: {line}", file=sys.stderr)
    data_root = os.environ.get("CLAUDE_PLUGIN_DATA")
    if not data_root:
        return
    try:
        path = Path(data_root)
        path.mkdir(parents=True, exist_ok=True)
        with (path / "adapter-events.jsonl").open("a", encoding="utf-8") as log:
            log.write(line + "\n")
    except OSError:
        pass  # ponytail: observability is best-effort, never a failure path


def _call_cli(verb: str, cwd: str) -> dict | None:
    """Run one CLI verb; return parsed response or None on any failure."""
    cli = _cli_path()
    if not cli.exists():
        _observe_event("adapter_cli_missing", verb=verb, cli=str(cli))
        return None
    request = json.dumps({"cwd": cwd})
    try:
        completed = subprocess.run(
            [sys.executable, str(cli), verb],
            input=request,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        _observe_event("adapter_cli_exec_error", verb=verb, error=str(error))
        return None
    try:
        response = json.loads(completed.stdout)
    except (json.JSONDecodeError, ValueError):
        _observe_event("adapter_cli_bad_json", verb=verb, exit=completed.returncode)
        return None
    if not isinstance(response, dict):
        _observe_event("adapter_cli_bad_json", verb=verb, exit=completed.returncode)
        return None
    return response


def do_register() -> int:
    """SessionStart: register the goal-file claim, never influence the turn."""
    payload = _read_payload()
    response = _call_cli("register", _cwd(payload))
    if response is not None and response.get("outcome") == "error":
        _observe_event("register_error", reason=response.get("reason"))
    # SessionStart returns no decision; registration outcome is advisory only.
    print("{}")
    return 0


def do_verify() -> int:
    """Stop: verify the active claim and map the outcome to a Codex decision."""
    payload = _read_payload()

    # Re-entry cap (§5): a failed verify may drive at most one block cycle per
    # attempt. Codex re-enters with stop_hook_active=true (p1). On re-entry we
    # fail open so a still-failing claim cannot loop the agent forever.
    if payload.get("stop_hook_active") is True:
        _observe_event("verify_reentry_capped")
        print("{}")
        return 0

    response = _call_cli("verify", _cwd(payload))
    if response is None:
        _fail_open("verify unavailable; proceeding")
        return 0

    outcome = response.get("outcome")
    if outcome == "failed":
        reason = response.get("reason") or "Axiom verification failed."
        print(json.dumps({"decision": "block", "reason": reason}))
        return 0
    if outcome in ("passed", "no_active_claim"):
        print("{}")  # pass silently
        return 0
    # outcome == "error" or anything unexpected -> fail open
    _fail_open(f"verify outcome {outcome!r}; proceeding")
    return 0


def main(argv: list[str]) -> int:
    verb = argv[1] if len(argv) > 1 else ""
    if verb == "register":
        return do_register()
    if verb == "verify":
        return do_verify()
    # Unknown invocation must never wedge the host.
    _fail_open(f"unknown verb {verb!r}; expected register|verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
