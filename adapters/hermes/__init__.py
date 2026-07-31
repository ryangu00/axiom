"""hermes-agent plugin: Axiom completion-claim verification.

hermes exposes the verify loop through the ``pre_verify`` hook, whose return is
consumed by ``get_pre_verify_continue_message`` — verified in-process against
that real function (see the p2 probe). hermes natively accepts both
``{"action": "continue", "message": ...}`` and the Claude-Code Stop shape, so
this adapter is a thin translation over the shared ``axiom-adapter-cli/v1``:

    on_session_start  -> `axiom register`   (goal-file discovery)
    pre_verify        -> `axiom verify`     (failed -> continue+message, else finish)

Per CONTRACTS.md §5 the CLI returns facts only; the host mapping lives here.
Everything fails open: hermes wraps each hook in try/except, and every path
below returns ``None`` (let the turn finish) on any error. hermes payloads
carry no cwd (process-global ``TERMINAL_CWD``), so the shim resolves it there.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Dev fallback only: valid when this file runs from inside the repo tree. A
# copy-install into ~/.hermes/plugins detaches it, so installed use must set
# AXIOM_CLI (or have `axiom` on PATH). See README.
_DEV_CLI = Path(__file__).resolve().parents[2] / "scripts" / "axiom_cli.py"


def _cli_path() -> Path:
    override = os.environ.get("AXIOM_CLI")
    if override:
        return Path(override)
    found = shutil.which("axiom")
    if found:
        return Path(found)
    return _DEV_CLI


def _cwd() -> str:
    return os.environ.get("TERMINAL_CWD") or os.getcwd()


def _call_cli(verb: str) -> dict | None:
    """Run one CLI verb against the shared primitive; None on any failure."""
    cli = _cli_path()
    if not cli.exists():
        print(f"axiom hermes adapter: CLI not found at {cli}", file=sys.stderr)
        return None
    try:
        completed = subprocess.run(
            [sys.executable, str(cli), verb],
            input=json.dumps({"cwd": _cwd()}),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        # Fail open, but never silently: an unobservable fail-open is
        # indistinguishable from a verifier that was never installed.
        print(f"axiom hermes adapter: {verb} failed open ({error})", file=sys.stderr)
        return None
    # §5: fail open on any nonzero exit, independent of stdout.
    if completed.returncode != 0:
        print(
            f"axiom hermes adapter: {verb} failed open "
            f"(CLI exit {completed.returncode})",
            file=sys.stderr,
        )
        return None
    try:
        response = json.loads(completed.stdout)
    except (json.JSONDecodeError, ValueError) as error:
        print(
            f"axiom hermes adapter: {verb} failed open (bad JSON: {error})",
            file=sys.stderr,
        )
        return None
    return response if isinstance(response, dict) else None


def _on_session_start(**kwargs: object) -> None:
    """Register the goal-file claim at session start; never affect the turn."""
    _call_cli("register")
    return None


def _pre_verify(**kwargs: object) -> dict | None:
    """Gate the verify loop: keep the turn going only on a failed claim."""
    try:
        attempt = int(kwargs.get("attempt", 0))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        attempt = 0
    # Re-entry cap (§5): nudge at most once per turn; then fail open with an
    # observable event. Coerce attempt so a str/float/None cannot bypass the cap.
    if attempt >= 1:
        print("axiom hermes adapter event: verify_reentry_capped", file=sys.stderr)
        return None
    response = _call_cli("verify")
    if response is None:
        return None  # fail open
    # Observe-by-default: the CLI records the finding; only an enforced rule
    # may keep the turn going (`enforced` is authoritative, CONTRACTS §5).
    if response.get("outcome") == "failed" and response.get("enforced"):
        reason = response.get("reason") or "Axiom verification failed."
        return {"action": "continue", "message": reason}
    return None  # observe-failed / passed / no_active_claim / error -> finish


def register(ctx: object) -> None:
    ctx.register_hook("on_session_start", _on_session_start)  # type: ignore[attr-defined]
    ctx.register_hook("pre_verify", _pre_verify)  # type: ignore[attr-defined]
