#!/usr/bin/env python3
"""Verify declared completion predicates and immediate Write/Edit read-backs."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import axiom_common as common
import predicate_evaluator

RULE = "write-verify"
COMPLETION_DECLARATION = re.compile(
    r"\b(?:done|complete(?:d)?|implemented|fixed|tests? pass(?:ed|ing)?)\b",
    re.IGNORECASE,
)


def _cwd(payload: Mapping[str, Any]) -> Path:
    value = payload.get("cwd")
    return (
        Path(value).resolve()
        if isinstance(value, str) and value
        else Path.cwd().resolve()
    )


def evaluate_claim(claim: Mapping[str, Any], *, cwd: Path) -> dict[str, Any]:
    """Evaluate every declared predicate through an independent evidence channel."""
    predicates = claim.get("predicates", [])
    predicates = predicates if isinstance(predicates, list) else []
    evidence: list[dict[str, Any]] = []
    baseline = claim.get("baseline", {})
    for item in predicates:
        evidence.append(
            predicate_evaluator.evaluate_predicate(item, cwd=cwd, baseline=baseline)
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


def failure_reason(
    failed: Sequence[Mapping[str, Any]], *, escape_hatch: bool = True
) -> str:
    """Render the block reason; the hatch is a Claude Code slash command.

    Adapter hosts pass `escape_hatch=False`: the command means nothing there,
    and quoting a bypass to the agent being blocked is a hint for the operator,
    not for the model.
    """
    details = "; ".join(
        f"{item.get('type')} {item.get('path') or item.get('cmd')}: "
        f"expected {item.get('expected')}, actual {item.get('actual')}"
        for item in failed
    )
    reason = (
        f"AXIOM write verification failed: {details}. "
        "Fix the declared artifact or verification command, then stop again."
    )
    if escape_hatch:
        reason += f" Escape hatch: {common.escape_hatch(RULE)}"
    return reason


def _corrupt_slot_decision(
    payload: Mapping[str, Any],
    paths: Mapping[str, Path],
    corrupt: common.ClaimSlotCorrupt,
) -> dict[str, Any] | None:
    """Decide a Stop whose claim slot exists but is not a claim.

    Observe mode records it and lets the turn end, like any other finding.
    Enforce mode blocks once -- a slot nobody can read is not "no claim", and
    an agent that could escape verification by scribbling over the slot would
    have a cheaper bypass than deleting it -- but honors the same re-entry cap
    as a failed claim, so a corrupt slot costs one extra cycle, never a wedge.
    """
    record: dict[str, Any] = {
        "event": "claim_unreadable",
        "hook": "write_verify",
        "rule": RULE,
        "status": corrupt.status,
        "reason": corrupt.reason,
        "path": str(corrupt.path),
        "finding_id": common.finding_id(RULE, "slot", str(corrupt.path)),
    }
    config = common.hook_config(paths, payload, "write_verify").data
    if common.rule_mode(config, RULE) == "observe":
        common.append_ledger(paths["ledger"], {**record, "action": "observed"})
        return None
    if bool(payload.get("stop_hook_active")):
        common.append_ledger(
            paths["ledger"], {**record, "action": "fail_open_after_reentry"}
        )
        return None
    # The reason reaches the model. It must not hand over the one step that
    # turns this block into a free pass ("remove that file" -> next Stop reads
    # an empty slot -> `unverified_completion`): the bytes are evidence, the
    # next session start quarantines them and re-registers from the goal file.
    reason = (
        f"AXIOM write verification cannot run: the active claim slot is "
        f"{corrupt.status} ({corrupt.reason}) at {corrupt.path}. Those bytes "
        "are evidence, not a claim: do not delete or rewrite them. This stop "
        "is refused once; the next session start quarantines the slot and "
        "re-registers the goal file. "
        f"Escape hatch: {common.escape_hatch(RULE)}"
    )
    common.record_best_effort(
        paths["ledger"], {**record, "action": "blocked", "reason": reason}
    )
    return {"decision": "block", "reason": reason}


def process_stop(
    payload: Mapping[str, Any], *, root: Path | str | None = None
) -> dict[str, Any] | None:
    """Apply the five-step Stop decision flow, failing open on verifier errors."""
    cwd = _cwd(payload)
    paths: dict[str, Path] | None = None
    try:
        paths = common.ensure_layout(root=root, cwd=cwd)
        try:
            claim = common.read_active_claim(root=root, cwd=cwd)
        except common.ClaimSlotCorrupt as corrupt:
            return _corrupt_slot_decision(payload, paths, corrupt)
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

        finding = common.claim_finding_id(RULE, claim)
        evaluated_claim_id = claim.get("claim_id")
        evaluated_legacy_token = None
        if not isinstance(evaluated_claim_id, str) or not evaluated_claim_id:
            baseline = claim.get("baseline")
            baseline = baseline if isinstance(baseline, Mapping) else {}
            evaluated_legacy_token = baseline.get("registered_at")
        result = evaluate_claim(claim, cwd=cwd)
        if result["passed"]:
            # Compare-and-clear: only clear the exact claim we evaluated. If
            # another session registered a new claim between our read and here,
            # its registration token differs and we leave it for its own Stop.
            cleared = common.clear_active_claim(
                root=root,
                cwd=cwd,
                expected_claim_id=evaluated_claim_id,
                expected_legacy_registered_at=evaluated_legacy_token,
            )
            common.append_ledger(
                paths["ledger"],
                {
                    "event": "verified",
                    "hook": "write_verify",
                    "rule": RULE,
                    "finding_id": finding,
                    "claim": claim,
                    "predicates": claim.get("predicates", []),
                    "evidence": result["evidence"],
                    "cleared": cleared,
                },
            )
            return None

        failed = [item for item in result["evidence"] if not item.get("passed")]
        config = common.hook_config(paths, payload, "write_verify").data
        mode = common.rule_mode(config, RULE)
        if mode == "observe":
            common.append_ledger(
                paths["ledger"],
                {
                    "event": "would_have_blocked",
                    "hook": "write_verify",
                    "rule": RULE,
                    "finding_id": finding,
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
                    "finding_id": finding,
                    "claim": claim,
                    "failed": failed,
                    "action": "fail_open_after_reentry",
                },
            )
            return None
        reason = failure_reason(failed)
        # The enforced decision is recorded like the observed one, or the
        # ledger goes blind the moment a rule is enforced and no precision can
        # ever be measured for it. Best-effort on purpose: a failed append
        # must not turn this block into a fail-open.
        common.record_best_effort(
            paths["ledger"],
            {
                "event": "blocked",
                "hook": "write_verify",
                "rule": RULE,
                "finding_id": finding,
                "claim": claim,
                "failed": failed,
                "reason": reason,
            },
        )
        return {"decision": "block", "reason": reason}
    except Exception as error:
        return common.fail_open(
            "write_verify",
            error,
            ledger=paths["ledger"] if paths is not None else None,
            rule=RULE,
        )


def process_posttooluse(
    payload: Mapping[str, Any], *, root: Path | str | None = None
) -> None:
    """Record an immediate stat-based read-back for successful Write/Edit events."""
    cwd = _cwd(payload)
    paths = common.ensure_layout(root=root, cwd=cwd)
    tool_input = payload.get("tool_input", {})
    tool_input = tool_input if isinstance(tool_input, Mapping) else {}
    path_value = tool_input.get("file_path")
    target = predicate_evaluator.resolve_target(cwd, path_value)
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
    payload: Any = {}
    try:
        payload = json.load(sys.stdin)
        payload = payload if isinstance(payload, Mapping) else {}
        event = payload.get("hook_event_name")
        if event == "Stop":
            response = process_stop(payload)
        elif event == "PostToolUse" and payload.get("tool_name") in {"Write", "Edit"}:
            process_posttooluse(payload)
            response = None
        else:
            response = None
        if response:
            print(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    except Exception as error:
        # Fail open, but never silently (an invisible fail-open is
        # indistinguishable from a hook that was never installed).
        return common.fail_open_main("write_verify", RULE, error, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
