#!/usr/bin/env python3
"""Axiom command-line interface (stdlib only).

Subcommands:
    report              Print a human-readable findings and coverage summary.
    modes               Show the current observe/enforce mode of every rule.
    enforce RULE on|off Set a single rule to enforce (on) or observe (off).
    persist-lessons JSON Append approved lessons (timestamp + source required).
    uninstall           Remove axiom-managed state files.

Every subcommand degrades to a helpful message when a dependency function is
missing, because parallel work on the shared library is in flight.
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
PROVIDERS_DIR = REPO_ROOT / "providers"


def _import_common() -> Any:
    """Import axiom_common by adding the hooks dir to sys.path."""
    if str(HOOKS_DIR) not in sys.path:
        sys.path.insert(0, str(HOOKS_DIR))
    try:
        import axiom_common  # type: ignore
    except Exception as error:  # pragma: no cover - import-time failure path
        return error
    return axiom_common


def _import_lessons_provider() -> Any:
    """Import the lessons_md provider if it is available."""
    if str(PROVIDERS_DIR) not in sys.path:
        sys.path.insert(0, str(PROVIDERS_DIR))
    try:
        import lessons_md  # type: ignore
    except Exception:
        return None
    return lessons_md


def _missing(what: str) -> int:
    print(f"axiom: {what} is not available yet (report-data layer pending).")
    print("Parallel work on axiom_common is in flight; retry once it lands.")
    return 0


# --------------------------------------------------------------------------- report


def _ledger_path(common: Any) -> Path | None:
    if hasattr(common, "state_paths"):
        try:
            return common.state_paths()["ledger"]
        except Exception:
            return None
    if hasattr(common, "data_root") and hasattr(common, "project_id"):
        root = common.data_root()
        return (
            root
            / common.SCHEMA_VERSION
            / "projects"
            / common.project_id()
            / "ledger.jsonl"
        )
    return None


def cmd_report(args: argparse.Namespace) -> int:
    common = _import_common()
    if isinstance(common, Exception):
        return _missing("axiom_common")
    getter = getattr(common, "get_report_data", None)
    if getter is None:
        print("axiom: report-data layer pending.")
        print("axiom_common.get_report_data() is not implemented yet, so no")
        print("findings or coverage can be summarized. Re-run once the report")
        print("data layer lands.")
        return 0
    ledger = _ledger_path(common)
    if ledger is None:
        print("axiom: cannot resolve the ledger path (state helpers missing).")
        return 1
    if hasattr(common, "ensure_layout"):
        with contextlib.suppress(Exception):
            common.ensure_layout()
    try:
        data = getter(ledger)
    except TypeError:
        # Getter expects a different signature than the documented single-arg form.
        try:
            data = getter()
        except Exception as error:
            print(
                f"axiom: report-data layer pending — get_report_data unusable: {error}"
            )
            return 1
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


def _config_paths(common: Any):
    """Resolve the project config path via whichever helpers are present."""
    if hasattr(common, "state_paths"):
        paths = common.state_paths()
        return paths, paths["config"]
    if hasattr(common, "data_root") and hasattr(common, "project_id"):
        root = common.data_root()
        project = root / common.SCHEMA_VERSION / "projects" / common.project_id()
        return project, project / "config.json"
    return None, None


def cmd_modes(args: argparse.Namespace) -> int:
    common = _import_common()
    if isinstance(common, Exception):
        return _missing("axiom_common")
    _, config_path = _config_paths(common)
    if config_path is None:
        print("axiom: cannot locate the project config (data_root/project_id missing).")
        return 1
    config = _read_config(common, config_path)
    rules = config.get("rules", {})
    rules = rules if isinstance(rules, Mapping) else {}
    if not rules:
        print("axiom: no rules are configured. Every rule defaults to observe.")
        return 0
    print("Current rule modes:")
    for rule in rules:
        mode = _rule_mode(common, config, rule)
        print(f"  {rule}: {mode}")
    return 0


def _read_config(common: Any, path: Path) -> dict[str, Any]:
    reader = getattr(common, "read_config", None)
    if reader is not None:
        return reader(path)
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_config(common: Any, path: Path, config: Mapping[str, Any]) -> bool:
    writer = getattr(common, "write_config", None)
    if writer is not None:
        writer(path, config)
        return True
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return True
    except OSError as error:
        print(f"axiom: cannot write config: {error}")
        return False


def _rule_mode(common: Any, config: Mapping[str, Any], rule: str) -> str:
    mode_fn = getattr(common, "rule_mode", None)
    if mode_fn is not None:
        return mode_fn(config, rule)
    rules = config.get("rules", {})
    rules = rules if isinstance(rules, Mapping) else {}
    rule_config = rules.get(rule, {})
    rule_config = rule_config if isinstance(rule_config, Mapping) else {}
    mode = rule_config.get("mode", "observe")
    return mode if mode in {"observe", "enforce"} else "observe"


# --------------------------------------------------------------------------- enforce


def cmd_enforce(args: argparse.Namespace) -> int:
    common = _import_common()
    if isinstance(common, Exception):
        return _missing("axiom_common")
    _, config_path = _config_paths(common)
    if config_path is None:
        print("axiom: cannot locate the project config (data_root/project_id missing).")
        return 1
    if hasattr(common, "ensure_layout"):
        common.ensure_layout()
    config = _read_config(common, config_path)
    rules = config.setdefault("rules", {})
    if not isinstance(rules, dict):
        rules = {}
        config["rules"] = rules
    rule_config = rules.setdefault(args.rule, {})
    if not isinstance(rule_config, dict):
        rule_config = {}
        rules[args.rule] = rule_config
    new_mode = "enforce" if args.on else "observe"
    rule_config["mode"] = new_mode
    if not _write_config(common, config_path, config):
        return 1
    print(f"axiom: rule '{args.rule}' is now {new_mode}.")
    if new_mode == "enforce":
        print(
            "enforce blocks the tool outright; observe only records what would have blocked."
        )
    else:
        print("observe records what would have blocked without stopping the tool.")
    return 0


# --------------------------------------------------------------------------- persist-lessons


def cmd_persist_lessons(args: argparse.Namespace) -> int:
    common = _import_common()
    if isinstance(common, Exception):
        return _missing("axiom_common")
    lessons_path = _lessons_path(common)
    if lessons_path is None:
        print("axiom: cannot locate the lessons file (state helpers missing).")
        return 1
    entries = _load_lessons_json(args.json_path)
    if entries is None:
        return 1

    provider = _import_lessons_provider()
    if provider is not None and hasattr(provider, "append_lessons"):
        try:
            provider.append_lessons(lessons_path, entries)
        except Exception as error:
            print(
                f"axiom: lessons_md provider failed: {error}; falling back to direct append."
            )
        else:
            print(f"axiom: persisted {len(entries)} lesson(s) via lessons_md.")
            return 0

    if _append_lessons_direct(lessons_path, entries):
        print(f"axiom: persisted {len(entries)} lesson(s) directly to lessons.md.")
        return 0
    return 1


def _lessons_path(common: Any) -> Path | None:
    if hasattr(common, "state_paths"):
        return common.state_paths()["lessons"]
    if hasattr(common, "data_root") and hasattr(common, "project_id"):
        root = common.data_root()
        return (
            root
            / common.SCHEMA_VERSION
            / "projects"
            / common.project_id()
            / "lessons.md"
        )
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
                handle.write(
                    f"- [{entry['timestamp']}] {entry['text']} (source: {entry['source']})\n"
                )
        return True
    except OSError as error:
        print(f"axiom: cannot append to lessons.md: {error}")
        return False


# --------------------------------------------------------------------------- uninstall


def _managed_paths(common: Any) -> list[Path] | None:
    manifest_fn = getattr(common, "manifest", None)
    if manifest_fn is None:
        return None
    try:
        result = manifest_fn()
    except Exception as error:
        print(f"axiom: cannot enumerate managed files: {error}")
        return None
    paths = result.get("managed_paths", []) if isinstance(result, Mapping) else []
    return [Path(p) for p in paths]


def _goal_paths(common: Any) -> list[Path]:
    """Best-effort enumeration of goal files under the project root."""
    if hasattr(common, "state_paths"):
        project_root = common.state_paths()["project_root"]
    elif hasattr(common, "data_root") and hasattr(common, "project_id"):
        project_root = (
            common.data_root()
            / common.SCHEMA_VERSION
            / "projects"
            / common.project_id()
        )
    else:
        return []
    found: list[Path] = []
    if project_root.is_dir():
        for candidate in project_root.rglob("*"):
            if candidate.is_file() and "goal" in candidate.name.lower():
                found.append(candidate)
    return found


def cmd_uninstall(args: argparse.Namespace) -> int:
    common = _import_common()
    if isinstance(common, Exception):
        return _missing("axiom_common")
    managed = _managed_paths(common)
    if managed is None:
        print(
            "axiom: axiom_common.manifest() is not available; cannot enumerate managed files."
        )
        print("Uninstall is unavailable until the manifest helper lands.")
        return 1

    existing = [p for p in managed if p.exists()]
    goals = _goal_paths(common)

    print("axiom-managed files:")
    if existing:
        for path in existing:
            print(f"  {path}")
    else:
        print("  (none present)")
    if goals:
        print("goal files detected:")
        for path in goals:
            print(f"  {path}")

    if args.dry_run:
        print("\n(dry-run: nothing deleted)")
        if args.keep_goals:
            print(
                "keep-goals requested: goal files will be preserved on real uninstall."
            )
        return 0

    if not args.confirm:
        print(
            "\naxiom: refusing to delete without --confirm. Re-run with --confirm to proceed."
        )
        return 1

    if not args.keep_goals and goals:
        existing.extend(goals)

    guard_root = None
    if hasattr(common, "data_root"):
        try:
            guard_root = common.data_root().resolve()
        except Exception:
            guard_root = None
    deleted = 0
    skipped = 0
    for path in existing:
        # Containment guard (MED 3.7): never delete outside the axiom data_root,
        # even if a manifest entry or symlink resolves elsewhere. Goal files are
        # exempt — they live in the user's project and are deleted only on
        # explicit confirmation.
        if guard_root is not None and "goal" not in path.name.lower():
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
    tail = f" skipped {skipped} outside data_root." if skipped else ""
    print(f"axiom: deleted {deleted} file(s).{tail}")

    remaining = [p for p in existing if p.exists()]
    if remaining:
        print("axiom: WARNING — these files still exist after deletion:")
        for path in remaining:
            print(f"  {path}")
        return 1

    still_managed = [p for p in _managed_paths(common) or [] if p.exists()]
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

    sub.add_parser("report", help="Print findings and coverage summary.")

    sub.add_parser("modes", help="Show current observe/enforce mode of every rule.")

    enforce = sub.add_parser(
        "enforce", help="Set a rule to enforce (on) or observe (off)."
    )
    enforce.add_argument("rule", help="Rule name to toggle.")
    enforce.add_argument(
        "mode",
        choices=("on", "off"),
        help="'on' selects enforce (blocks); 'off' selects observe (records only).",
    )

    persist = sub.add_parser(
        "persist-lessons", help="Append approved lessons from a JSON file."
    )
    persist.add_argument("json_path", help="Path to a JSON file of approved lessons.")

    uninstall = sub.add_parser("uninstall", help="Remove axiom-managed state files.")
    uninstall.add_argument(
        "--dry-run", action="store_true", help="List files without deleting."
    )
    uninstall.add_argument(
        "--confirm", action="store_true", help="Actually delete managed files."
    )
    uninstall.add_argument(
        "--keep-goals",
        action="store_true",
        help="Preserve goal files even when deleting other managed state.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

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
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
