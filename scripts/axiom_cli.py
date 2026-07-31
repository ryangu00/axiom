#!/usr/bin/env python3
"""Axiom command-line interface (stdlib only).

Subcommands:
    register            Register an adapter claim from one stdin JSON object.
    verify              Verify the active claim from one stdin JSON object.
    report              Print a human-readable findings and coverage summary.
    modes               Show the current observe/enforce mode of every rule.
    enforce RULE on|off Set a single rule to enforce (on) or observe (off).
    persist-lessons JSON Append approved lessons (timestamp + source required).
    uninstall           Remove axiom-managed state files.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks"
ADAPTER_PROTOCOL = "axiom-adapter-cli/v1"


def _import_common() -> Any:
    """Import axiom_common by adding the hooks dir to sys.path."""
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    try:
        import axiom_common
    except Exception as error:  # pragma: no cover - import-time failure path
        return error
    return axiom_common


def _missing(what: str) -> int:
    print(f"axiom: could not import {what} from {HOOKS_DIR}.", file=sys.stderr)
    print("Verify the plugin is installed intact, then retry.", file=sys.stderr)
    return 2


def _import_write_verify() -> Any:
    """Import the runtime verifier used by the Stop hook."""
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    try:
        import write_verify
    except Exception as error:  # pragma: no cover - import-time failure path
        return error
    return write_verify


# ------------------------------------------------------------- adapter protocol


def _adapter_response(response: Mapping[str, Any]) -> None:
    payload = {"protocol": ADAPTER_PROTOCOL, **response}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _adapter_error(reason: str, *, error_kind: str, exit_code: int) -> int:
    print(f"axiom adapter CLI: {reason}", file=sys.stderr)
    _adapter_response({"outcome": "error", "error_kind": error_kind, "reason": reason})
    return exit_code


def _read_adapter_request() -> tuple[dict[str, Any] | None, int | None]:
    try:
        request = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as error:
        return None, _adapter_error(
            f"invalid JSON request: {error}",
            error_kind="malformed_request",
            exit_code=2,
        )
    if not isinstance(request, dict):
        return None, _adapter_error(
            "request must be one JSON object",
            error_kind="malformed_request",
            exit_code=2,
        )
    cwd = request.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None, _adapter_error(
            "request is missing required cwd",
            error_kind="malformed_request",
            exit_code=2,
        )
    if not Path(cwd).is_absolute():
        return None, _adapter_error(
            "request cwd must be an absolute path",
            error_kind="malformed_request",
            exit_code=2,
        )
    return request, None


def _claim_id(claim: Mapping[str, Any]) -> str | None:
    value = claim.get("claim_id")
    return value if isinstance(value, str) and value else None


def cmd_register(args: argparse.Namespace) -> int:
    request, error_code = _read_adapter_request()
    if request is None:
        return error_code if error_code is not None else 2
    claim = request.get("claim")
    if claim is not None and not isinstance(claim, Mapping):
        return _adapter_error(
            "claim must be a JSON object",
            error_kind="malformed_request",
            exit_code=2,
        )
    common = _import_common()
    if isinstance(common, Exception):
        return _adapter_error(
            f"could not import axiom_common: {common}",
            error_kind="internal",
            exit_code=3,
        )
    cwd = Path(request["cwd"])
    try:
        registration = (
            common.register_claim_if_absent(claim, cwd=cwd)
            if claim is not None
            else common.register_goal_claim(cwd=cwd)
        )
        if registration is None:
            _adapter_response(
                {
                    "outcome": "no_goal_found",
                    "registered": False,
                    "claim_id": None,
                    "reason": None,
                }
            )
            return 0
        outcome = "registered" if registration.registered else "already_active"
        _adapter_response(
            {
                "outcome": outcome,
                "registered": registration.registered,
                "claim_id": _claim_id(registration.claim),
                "reason": None,
            }
        )
        return 0
    except Exception as error:
        return _adapter_error(
            f"{type(error).__name__}: {error}",
            error_kind="internal",
            exit_code=3,
        )


def cmd_verify(args: argparse.Namespace) -> int:
    request, error_code = _read_adapter_request()
    if request is None:
        return error_code if error_code is not None else 2
    common = _import_common()
    verifier = _import_write_verify()
    import_error = common if isinstance(common, Exception) else verifier
    if isinstance(import_error, Exception):
        return _adapter_error(
            f"could not import verifier core: {import_error}",
            error_kind="internal",
            exit_code=3,
        )
    cwd = Path(request["cwd"])
    try:
        claim = common.read_active_claim(cwd=cwd)
        if claim is None:
            _adapter_response(
                {
                    "outcome": "no_active_claim",
                    "claim_id": None,
                    "cleared": False,
                    "evidence": [],
                    "reason": None,
                }
            )
            return 0

        claim_id = _claim_id(claim)
        legacy_token = None
        if claim_id is None:
            baseline = claim.get("baseline")
            baseline = baseline if isinstance(baseline, Mapping) else {}
            value = baseline.get("registered_at")
            legacy_token = value if isinstance(value, str) else None
        result = verifier.evaluate_claim(claim, cwd=cwd)
        evidence = result["evidence"]
        if result["passed"]:
            cleared = common.clear_active_claim(
                cwd=cwd,
                expected_claim_id=claim_id,
                expected_legacy_registered_at=legacy_token,
            )
            _adapter_response(
                {
                    "outcome": "passed",
                    "claim_id": claim_id,
                    "cleared": cleared,
                    "evidence": evidence,
                    "reason": None,
                }
            )
            return 0

        failed = [item for item in evidence if not item.get("passed")]
        # Observe-by-default holds on EVERY runtime, not only the Claude lane:
        # the CLI reads the same rule mode the Stop hook reads. Unless the
        # operator enforced the rule, a failure is recorded here (the same
        # would_have_blocked event the report reads) and the host must stay
        # silent — `enforced` is the authoritative signal for host action.
        paths = common.ensure_layout(cwd=cwd)
        config = common.load_hook_config(
            paths["config"], ledger=paths["ledger"], hook="cli_verify"
        ).data
        enforced = common.rule_mode(config, "write-verify") == "enforce"
        recorded = True
        if not enforced:
            # Observe mode's whole value is the record. If the append fails
            # (read-only ledger, full disk) the host still proceeds — but
            # swallowing that silently would be a missed finding dressed up as
            # a clean observe run, which is the exact failure this tool exists
            # to catch. Say so on stderr and tell the caller in `recorded`.
            try:
                common.append_ledger(
                    paths["ledger"],
                    {
                        "event": "would_have_blocked",
                        "hook": "adapter_cli",
                        "rule": "write-verify",
                        "claim": claim,
                        "failed": failed,
                    },
                )
            except Exception as error:
                recorded = False
                # State the failure, not a guessed cause: this catches every
                # exception, so "the ledger is unwritable" would misattribute a
                # serialization bug to file permissions.
                print(
                    "axiom adapter CLI: observe finding NOT recorded — "
                    f"appending to {paths['ledger']} raised "
                    f"{type(error).__name__}: {error}",
                    file=sys.stderr,
                )
        _adapter_response(
            {
                "outcome": "failed",
                "claim_id": claim_id,
                "cleared": False,
                "enforced": enforced,
                "recorded": recorded,
                "evidence": evidence,
                "reason": verifier.failure_reason(failed),
            }
        )
        return 0
    except Exception as error:
        return _adapter_error(
            f"{type(error).__name__}: {error}",
            error_kind="internal",
            exit_code=3,
        )


# --------------------------------------------------------------------------- report


def _project_cwd(args: argparse.Namespace) -> Path:
    """Resolve the project directory a project-scoped subcommand targets.

    CONTRACTS §1: cwd is always explicit. `--cwd` defaults to the process cwd
    for compatibility, but the resolved project is printed by destructive
    commands so a wrong-directory run is visible instead of silently clean.
    """
    value = getattr(args, "cwd", None)
    if isinstance(value, str) and value:
        return Path(value).expanduser().resolve()
    return Path.cwd()


def _ledger_path(common: Any, cwd: Path) -> Path | None:
    try:
        return common.state_paths(cwd=cwd)["ledger"]
    except Exception:
        return None


def cmd_report(args: argparse.Namespace) -> int:
    common = _import_common()
    if isinstance(common, Exception):
        return _missing("axiom_common")
    cwd = _project_cwd(args)
    ledger = _ledger_path(common, cwd)
    if ledger is None:
        print("axiom: cannot resolve the ledger path (state helpers unavailable).")
        return 1
    with contextlib.suppress(Exception):
        common.ensure_layout(cwd=cwd)
    try:
        data = common.get_report_data(ledger)
    except Exception as error:
        print(f"axiom: failed to gather report data: {error}")
        return 1
    return _render_report(data)


def _scene_line(rule: str, scene: Mapping[str, Any]) -> str:
    """One-line summary of a recent incident: <label> | <failed predicate> | <time>."""
    label = scene.get("claim_label") or scene.get("label") or rule
    failed = scene.get("failed")
    if isinstance(failed, list):
        predicate = ", ".join(str(item) for item in failed) if failed else ""
    else:
        predicate = scene.get("predicate") or scene.get("basis") or ""
    when = scene.get("timestamp") or scene.get("time") or ""
    return f"{label} | {predicate} | {when}"


def _render_report(data: Mapping[str, Any]) -> int:
    rules = data.get("rules", {}) if isinstance(data, Mapping) else {}
    coverage = data.get("coverage", {}) if isinstance(data, Mapping) else {}
    degraded = data.get("config_degraded", {}) if isinstance(data, Mapping) else {}
    lock_degraded = data.get("lock_degraded", {}) if isinstance(data, Mapping) else {}

    events_total = 0
    heartbeats_total = 0

    if rules and isinstance(rules, Mapping):
        print("== Findings by rule ==")
        for rule, info in rules.items():
            info = info if isinstance(info, Mapping) else {}
            count = info.get("would_have_blocked", 0)
            count = count if isinstance(count, int) else 0
            events_total += count
            print(f"\n[{rule}] would-have-blocked: {count}")
            incidents = info.get("recent") if isinstance(info, Mapping) else None
            if not isinstance(incidents, list):
                incidents = (
                    info.get("incidents", []) if isinstance(info, Mapping) else []
                )
            recent = incidents[-3:]
            if recent:
                print("  last incidents:")
                for incident in recent:
                    scene = incident if isinstance(incident, Mapping) else {}
                    print(f"    {_scene_line(rule, scene)}")
            else:
                print("  (no incidents recorded)")

    if isinstance(degraded, Mapping) and degraded.get("count", 0):
        print("\n== Degraded config ==")
        print(f"episodes: {degraded.get('count', 0)}")
        print(f"latest status: {degraded.get('latest_status', '')}")
        print(f"latest path: {degraded.get('latest_path', '')}")
        print(f"latest hook: {degraded.get('latest_hook', '')}")
        print(f"latest reason: {degraded.get('latest_reason', '')}")

    if isinstance(lock_degraded, Mapping) and lock_degraded.get("count", 0):
        print("\n== Lock degraded ==")
        print("WARNING: claim mutual exclusion is degraded; proceeding lock-free.")
        print(f"episodes: {lock_degraded.get('count', 0)}")
        print(f"latest lock path: {lock_degraded.get('latest_lock_path', '')}")
        print(f"latest reason: {lock_degraded.get('latest_reason', '')}")

    if coverage and isinstance(coverage, Mapping):
        print("\n== Coverage ==")
        heartbeat_days = coverage.get("heartbeat_days")
        if heartbeat_days is not None:
            print(f"heartbeat days: {heartbeat_days}")
            heartbeats_total = heartbeat_days if isinstance(heartbeat_days, int) else 0
        events_value = coverage.get("event_count", coverage.get("events_total"))
        if events_value is not None:
            print(f"events total: {events_value}")
            if isinstance(events_value, int):
                events_total = events_value
        last_active = coverage.get("hook_last_active", coverage.get("per_hook", {}))
        if isinstance(last_active, Mapping) and last_active:
            print("per-hook last-active:")
            for hook, last in last_active.items():
                print(f"  {hook}: {last}")
        elif isinstance(last_active, Mapping):
            print("per-hook last-active: (none)")

    if events_total == 0 and heartbeats_total == 0:
        print("\nWARNING: zero events and zero heartbeats reported.")
        print("The report layer has nothing to summarize — the axiom hooks may")
        print("not be loaded. Verify the plugin hooks are registered and active.")

    hot = [
        rule
        for rule, info in (rules if isinstance(rules, Mapping) else {}).items()
        if isinstance(info, Mapping)
        and isinstance(info.get("would_have_blocked"), int)
        and info.get("would_have_blocked", 0) >= 3
    ]
    if hot:
        print("\nSuggestion: run /axiom:enforce to switch these rules to enforce:")
        for rule in hot:
            print(f"  /axiom:enforce {rule} on")
    return 0


# --------------------------------------------------------------------------- modes


def _config_path(common: Any, cwd: Path) -> Path | None:
    """Resolve the project config path via the shared state helpers."""
    try:
        return common.state_paths(cwd=cwd)["config"]
    except Exception:
        return None


def cmd_modes(args: argparse.Namespace) -> int:
    common = _import_common()
    if isinstance(common, Exception):
        return _missing("axiom_common")
    cwd = _project_cwd(args)
    config_path = _config_path(common, cwd)
    if config_path is None:
        print("axiom: cannot locate the project config (state helpers unavailable).")
        return 1
    paths = common.state_paths(cwd=cwd)
    config = common.load_hook_config(
        config_path, ledger=paths["ledger"], hook="cli_modes"
    ).data
    rules = config.get("rules", {})
    rules = rules if isinstance(rules, Mapping) else {}
    if not rules:
        print("axiom: no rules are configured. Every rule defaults to observe.")
        return 0
    print("Current rule modes:")
    for rule in rules:
        print(f"  {rule}: {common.rule_mode(config, rule)}")
    return 0


# --------------------------------------------------------------------------- enforce


KNOWN_RULES = ("preflight", "schema-guard", "stuck-search", "write-verify")


def cmd_enforce(args: argparse.Namespace) -> int:
    common = _import_common()
    if isinstance(common, Exception):
        return _missing("axiom_common")
    if args.rule not in KNOWN_RULES:
        # Warn but stay fail-open: a typo should be visible, not fatal.
        print(
            f"axiom: warning — '{args.rule}' is not a known rule "
            f"({', '.join(KNOWN_RULES)}); no hook reads it, writing anyway.",
            file=sys.stderr,
        )
    cwd = _project_cwd(args)
    config_path = _config_path(common, cwd)
    if config_path is None:
        print("axiom: cannot locate the project config (state helpers unavailable).")
        return 1
    common.ensure_layout(cwd=cwd)
    paths = common.state_paths(cwd=cwd)
    config = common.load_hook_config(
        config_path, ledger=paths["ledger"], hook="cli_enforce"
    ).data
    rules = config.setdefault("rules", {})
    if not isinstance(rules, dict):
        rules = {}
        config["rules"] = rules
    rule_config = rules.setdefault(args.rule, {})
    if not isinstance(rule_config, dict):
        rule_config = {}
        rules[args.rule] = rule_config
    previous_mode = common.rule_mode(config, args.rule)
    new_mode = "enforce" if args.on else "observe"
    rule_config["mode"] = new_mode
    try:
        common.write_config(config_path, config)
    except OSError as error:
        print(f"axiom: cannot write config: {error}")
        return 1
    # The human's decision belongs in the same ledger as the machine's: a
    # governance trail that records only what the tool did, and not what its
    # operator chose, audits the wrong half.
    with contextlib.suppress(Exception):
        common.append_ledger(
            paths["ledger"],
            {
                "event": "mode_changed",
                "hook": "cli_enforce",
                "rule": args.rule,
                "from": previous_mode,
                "to": new_mode,
                "decided_by": "human",
            },
        )
    print(f"axiom: rule '{args.rule}' is now {new_mode}.")
    if new_mode == "enforce":
        # Be precise: only two rules can actually stop the host. preflight and
        # stuck-search stay advisory in either mode — claiming otherwise would
        # leave the operator believing in a gate that does not exist.
        if args.rule in ("write-verify", "schema-guard"):
            print(
                "enforce blocks the action outright; observe only records what "
                "would have blocked."
            )
        else:
            print(
                f"note: '{args.rule}' is advisory in both modes — it injects "
                "guidance and records findings, it never blocks. enforce only "
                "changes whether that guidance is surfaced to the agent."
            )
    else:
        print("observe records what would have blocked without stopping the tool.")
    return 0


# --------------------------------------------------------------------------- persist-lessons


def cmd_persist_lessons(args: argparse.Namespace) -> int:
    common = _import_common()
    if isinstance(common, Exception):
        return _missing("axiom_common")
    lessons_path = _lessons_path(common, _project_cwd(args))
    if lessons_path is None:
        print("axiom: cannot locate the lessons file (state helpers unavailable).")
        return 1
    entries = _load_lessons_json(args.json_path)
    if entries is None:
        return 1
    if _append_lessons_direct(lessons_path, entries):
        print(f"axiom: persisted {len(entries)} lesson(s) to lessons.md.")
        return 0
    return 1


def _lessons_path(common: Any, cwd: Path) -> Path | None:
    try:
        return common.state_paths(cwd=cwd)["lessons"]
    except Exception:
        return None


def _load_lessons_json(path: str) -> list[dict[str, Any]] | None:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"axiom: lessons file not found: {path}")
        return None
    except (json.JSONDecodeError, OSError) as error:
        print(f"axiom: cannot read lessons JSON: {error}")
        return None
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, Mapping) and isinstance(raw.get("lessons"), list):
        entries = raw["lessons"]
    else:
        print(
            "axiom: lessons JSON must be a list of entries or an object with a 'lessons' list."
        )
        return None
    validated: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        source = entry.get("source")
        timestamp = entry.get("timestamp")
        text = entry.get("text") or entry.get("lesson")
        if not source or not timestamp or not text:
            print(
                "axiom: skipping entry missing required 'source', 'timestamp', or 'text'."
            )
            continue
        validated.append({"text": text, "source": source, "timestamp": timestamp})
    return validated


def _append_lessons_direct(path: Path, entries: list[dict[str, Any]]) -> bool:
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with Path(path).open("a", encoding="utf-8") as handle:
            for entry in entries:
                # Must match providers/_util.py serialize_lesson / parse_lesson
                # (`- [ts] [source] text #tags`), or recall silently skips it.
                timestamp = " ".join(str(entry["timestamp"]).split())
                source = " ".join(str(entry["source"]).split())
                text = " ".join(str(entry["text"]).split())
                tags = "".join(
                    f" #{str(tag).lstrip('#')}"
                    for tag in entry.get("tags", [])
                    if str(tag).lstrip("#")
                )
                handle.write(f"- [{timestamp}] [{source}] {text}{tags}\n")
        return True
    except OSError as error:
        print(f"axiom: cannot append to lessons.md: {error}")
        return False


# --------------------------------------------------------------------------- uninstall


def _managed_paths(common: Any, cwd: Path) -> list[Path] | None:
    try:
        result = common.manifest(cwd=cwd)
    except Exception as error:
        print(f"axiom: cannot enumerate managed files: {error}")
        return None
    paths = result.get("managed_paths", []) if isinstance(result, Mapping) else []
    return [Path(p) for p in paths]


def _sweep_empty_dirs(root: Path) -> int:
    """Remove now-empty directories under (and including) the data root."""
    removed = 0
    try:
        directories = sorted(
            (p for p in root.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        )
    except OSError:
        return 0
    for directory in [*directories, root]:
        with contextlib.suppress(OSError):
            directory.rmdir()  # only succeeds when empty — exactly what we want
            removed += 1
    return removed


def cmd_uninstall(args: argparse.Namespace) -> int:
    common = _import_common()
    if isinstance(common, Exception):
        return _missing("axiom_common")
    cwd = _project_cwd(args)
    managed = _managed_paths(common, cwd)
    if managed is None:
        return 1

    existing = [p for p in managed if p.exists()]

    # Make the resolved target visible so a wrong-directory run reads as
    # "wrong project", never as a silently clean uninstall.
    with contextlib.suppress(Exception):
        print(f"axiom project: {cwd}")
        print(f"axiom state root: {common.state_paths(cwd=cwd)['project_root']}")
    print("axiom-managed files:")
    if existing:
        for path in existing:
            print(f"  {path}")
    else:
        print("  (none present)")
    # Goal files are the user's: they live in the project repo, and this
    # command never touches the project directory at all.
    print("(your *.goal.md files live in your project and are never touched)")

    if args.dry_run:
        print("\n(dry-run: nothing deleted)")
        return 0

    if not args.confirm:
        print(
            "\naxiom: refusing to delete without --confirm. Re-run with --confirm to proceed."
        )
        return 1

    try:
        guard_root = common.data_root().resolve()
    except Exception:
        guard_root = None
    deleted = 0
    skipped = 0
    for path in existing:
        # Containment guard (MED 3.7): never delete outside the axiom
        # data_root, even if a manifest entry or symlink resolves elsewhere.
        if guard_root is not None:
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if not (guard_root == resolved or guard_root in resolved.parents):
                print(
                    f"axiom: skipping {path} — resolves outside data_root, refusing to delete."
                )
                skipped += 1
                continue
        try:
            path.unlink(missing_ok=True)
            deleted += 1
        except OSError as error:
            print(f"axiom: could not delete {path}: {error}")
    directories_removed = _sweep_empty_dirs(guard_root) if guard_root else 0
    tail = f" skipped {skipped} outside data_root." if skipped else ""
    print(
        f"axiom: deleted {deleted} file(s), removed {directories_removed} "
        f"empty directorie(s).{tail}"
    )

    remaining = [p for p in existing if p.exists()]
    if remaining:
        print("axiom: WARNING — these files still exist after deletion:")
        for path in remaining:
            print(f"  {path}")
        return 1

    still_managed = [p for p in _managed_paths(common, cwd) or [] if p.exists()]
    print("\nPost-uninstall dry-run (proving empty):")
    if still_managed:
        print("axiom: managed files remain:")
        for path in still_managed:
            print(f"  {path}")
        return 1
    print("  (no axiom-managed files remain)")
    print("\naxiom state removed. To uninstall the plugin itself, run:")
    print("  claude plugin uninstall axiom")
    print("Note: the host keeps plugin cache copies, which Claude Code manages —")
    print("this command only removed the state this plugin wrote.")
    return 0


# --------------------------------------------------------------------------- entry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="axiom_cli",
        description="Axiom command-line interface (stdlib only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # Project-scoped subcommands take an explicit --cwd (CONTRACTS §1); the
    # adapter verbs receive cwd in their stdin JSON instead.
    project_opts = argparse.ArgumentParser(add_help=False)
    project_opts.add_argument(
        "--cwd",
        default=None,
        help="Project directory whose Axiom state to target (default: current directory).",
    )

    sub.add_parser("register", help="Register an adapter claim from stdin JSON.")

    sub.add_parser("verify", help="Verify the active adapter claim from stdin JSON.")

    sub.add_parser(
        "report", parents=[project_opts], help="Print findings and coverage summary."
    )

    sub.add_parser(
        "modes",
        parents=[project_opts],
        help="Show current observe/enforce mode of every rule.",
    )

    enforce = sub.add_parser(
        "enforce",
        parents=[project_opts],
        help="Set a rule to enforce (on) or observe (off).",
    )
    enforce.add_argument("rule", help="Rule name to toggle.")
    enforce.add_argument(
        "mode",
        choices=("on", "off"),
        help="'on' selects enforce (blocks); 'off' selects observe (records only).",
    )

    persist = sub.add_parser(
        "persist-lessons",
        parents=[project_opts],
        help="Append approved lessons from a JSON file.",
    )
    persist.add_argument("json_path", help="Path to a JSON file of approved lessons.")

    uninstall = sub.add_parser(
        "uninstall",
        parents=[project_opts],
        help="Remove axiom-managed state files.",
    )
    uninstall.add_argument(
        "--dry-run", action="store_true", help="List files without deleting."
    )
    uninstall.add_argument(
        "--confirm", action="store_true", help="Actually delete managed files."
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "register":
        return cmd_register(args)
    if args.command == "verify":
        return cmd_verify(args)
    if args.command == "report":
        return cmd_report(args)
    if args.command == "modes":
        return cmd_modes(args)
    if args.command == "enforce":
        args.on = args.mode == "on"
        return cmd_enforce(args)
    if args.command == "persist-lessons":
        return cmd_persist_lessons(args)
    if args.command == "uninstall":
        return cmd_uninstall(args)
    raise AssertionError("argparse required=True guarantees a handled command")


if __name__ == "__main__":
    raise SystemExit(main())
