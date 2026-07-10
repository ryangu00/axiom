#!/usr/bin/env python3
"""Verify declared completion predicates and immediate Write/Edit read-backs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Any, Mapping, Sequence

import axiom_common as common


RULE = "write-verify"
UNSAFE_COMMAND = re.compile(r"[;|&$`<>\n\r]")
COMPLETION_DECLARATION = re.compile(
    r"\b(?:done|complete(?:d)?|implemented|fixed|tests? pass(?:ed|ing)?)\b",
    re.IGNORECASE,
)
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


def _cwd(payload: Mapping[str, Any]) -> Path:
    value = payload.get("cwd")
    return (
        Path(value).resolve()
        if isinstance(value, str) and value
        else Path.cwd().resolve()
    )


def _target(cwd: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (cwd / path).resolve()


def _snapshot(path: Path) -> dict[str, Any]:
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
    if executable not in ALLOWED_EXECUTABLES and not executable.startswith("python"):
        raise ValueError(f"executable is not allowlisted: {executable}")
    return argv


def _evidence(
    predicate: Mapping[str, Any], claim: Mapping[str, Any], *, cwd: Path
) -> dict[str, Any]:
    predicate_type = predicate.get("type")
    path_value = predicate.get("path")
    target = _target(cwd, path_value)

    if predicate_type == "file_exists":
        exists = bool(target and target.exists())
        return {
            "type": predicate_type,
            "path": path_value,
            "passed": exists,
            "expected": "file exists",
            "actual": "exists" if exists else "missing",
        }

    if predicate_type == "file_contains":
        pattern = predicate.get("pattern")
        actual = "missing"
        passed = False
        if target is not None and isinstance(pattern, str):
            try:
                passed = (
                    re.search(pattern, target.read_text(encoding="utf-8")) is not None
                )
                actual = "pattern found" if passed else "pattern absent"
            except re.error as error:
                actual = f"invalid pattern: {error}"
            except (OSError, UnicodeError):
                actual = "unreadable or missing"
        return {
            "type": predicate_type,
            "path": path_value,
            "passed": passed,
            "expected": f"contains {pattern!r}",
            "actual": actual,
        }

    if predicate_type == "file_changed":
        baseline = claim.get("baseline", {})
        baseline = baseline if isinstance(baseline, Mapping) else {}
        files = baseline.get("files", {})
        files = files if isinstance(files, Mapping) else {}
        before = files.get(path_value, {})
        before = before if isinstance(before, Mapping) else {}
        current = (
            _snapshot(target)
            if target is not None
            else {"exists": False, "sha256": None}
        )
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
            "actual": "changed" if passed else "unchanged",
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

    return {
        "type": predicate_type,
        "passed": False,
        "expected": "one supported v1 predicate type",
        "actual": "unsupported or malformed predicate",
    }


def evaluate_claim(claim: Mapping[str, Any], *, cwd: Path) -> dict[str, Any]:
    """Evaluate every declared predicate through an independent evidence channel."""
    predicates = claim.get("predicates", [])
    predicates = predicates if isinstance(predicates, list) else []
    evidence: list[dict[str, Any]] = []
    for item in predicates:
        if isinstance(item, Mapping):
            evidence.append(_evidence(item, claim, cwd=cwd))
        else:
            # A malformed (non-mapping) predicate is failed evidence, never
            # silently dropped: a declared predicate that cannot be evaluated
            # must not let the claim pass on a sibling.
            evidence.append(
                {
                    "type": None,
                    "passed": False,
                    "expected": "well-formed predicate object",
                    "actual": f"malformed entry ({type(item).__name__})",
                }
            )
    return {
        "passed": bool(evidence) and all(item["passed"] for item in evidence),
        "evidence": evidence,
    }


def _has_completion_declaration(payload: Mapping[str, Any]) -> bool:
    path_value = payload.get("transcript_path")
    if not isinstance(path_value, str) or not path_value:
        return False
    try:
        with Path(path_value).open("rb") as transcript:
            transcript.seek(0, 2)
            transcript.seek(max(0, transcript.tell() - 8192))
            tail = transcript.read().decode("utf-8", errors="replace")
    except OSError:
        return False
    return bool(COMPLETION_DECLARATION.search(tail))


def _failure_reason(failed: Sequence[Mapping[str, Any]]) -> str:
    details = "; ".join(
        f"{item.get('type')} {item.get('path') or item.get('cmd')}: "
        f"expected {item.get('expected')}, actual {item.get('actual')}"
        for item in failed
    )
    return (
        f"AXIOM write verification failed: {details}. "
        "Fix the declared artifact or verification command, then stop again. "
        "Escape hatch: /axiom:enforce off write-verify"
    )


def process_stop(
    payload: Mapping[str, Any], *, root: Path | str | None = None
) -> dict[str, Any] | None:
    """Apply the five-step Stop decision flow, failing open on verifier errors."""
    cwd = _cwd(payload)
    paths = common.ensure_layout(root=root, cwd=cwd)
    try:
        claim = common.read_active_claim(root=root, cwd=cwd)
        if claim is None:
            common.append_ledger(
                paths["ledger"],
                {
                    "event": "unverified_completion",
                    "hook": "write_verify",
                    "rule": RULE,
                    "completion_declaration": _has_completion_declaration(payload),
                },
            )
            return None

        evaluated_token = (claim.get("baseline") or {}).get("registered_at")
        result = evaluate_claim(claim, cwd=cwd)
        if result["passed"]:
            # Compare-and-clear: only clear the exact claim we evaluated. If
            # another session registered a new claim between our read and here,
            # its registration token differs and we leave it for its own Stop.
            cleared = common.clear_active_claim(
                root=root, cwd=cwd, expected_registered_at=evaluated_token
            )
            common.append_ledger(
                paths["ledger"],
                {
                    "event": "verified",
                    "hook": "write_verify",
                    "rule": RULE,
                    "claim": claim,
                    "predicates": claim.get("predicates", []),
                    "evidence": result["evidence"],
                    "cleared": cleared,
                },
            )
            return None

        failed = [item for item in result["evidence"] if not item.get("passed")]
        mode = common.rule_mode(common.read_config(paths["config"]), RULE)
        if mode == "observe":
            common.append_ledger(
                paths["ledger"],
                {
                    "event": "would_have_blocked",
                    "hook": "write_verify",
                    "rule": RULE,
                    "claim": claim,
                    "failed": failed,
                },
            )
            return None

        if bool(payload.get("stop_hook_active")):
            common.append_ledger(
                paths["ledger"],
                {
                    "event": "escalation",
                    "hook": "write_verify",
                    "rule": RULE,
                    "claim": claim,
                    "failed": failed,
                    "action": "fail_open_after_reentry",
                },
            )
            return None
        return {"decision": "block", "reason": _failure_reason(failed)}
    except Exception as error:
        try:
            common.append_ledger(
                paths["ledger"],
                {
                    "event": "error",
                    "hook": "write_verify",
                    "rule": RULE,
                    "error": f"{type(error).__name__}: {error}",
                    "action": "fail_open",
                },
            )
        except Exception:
            pass
        return None


def process_posttooluse(
    payload: Mapping[str, Any], *, root: Path | str | None = None
) -> None:
    """Record an immediate stat-based read-back for successful Write/Edit events."""
    cwd = _cwd(payload)
    paths = common.ensure_layout(root=root, cwd=cwd)
    tool_input = payload.get("tool_input", {})
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    path_value = tool_input.get("file_path")
    target = _target(cwd, path_value)
    record: dict[str, Any] = {
        "event": "write_readback",
        "hook": "write_verify",
        "rule": RULE,
        "tool": payload.get("tool_name", ""),
        "path": path_value,
        "verified": False,
    }
    try:
        if target is not None:
            stat = target.stat()
            record["verified"] = True
            record["stat"] = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        else:
            record["error"] = "missing file_path"
    except OSError as error:
        record["error"] = f"{type(error).__name__}: {error}"
    common.append_ledger(paths["ledger"], record)
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        payload = payload if isinstance(payload, Mapping) else {}
        event = payload.get("hook_event_name")
        if event == "Stop":
            response = process_stop(payload)
        elif event == "PostToolUse" and payload.get("tool_name") in {"Write", "Edit"}:
            response = process_posttooluse(payload)
        else:
            response = None
        if response:
            print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
