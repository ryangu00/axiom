#!/usr/bin/env python3
"""Self-tests for the Axiom shared state library."""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import importlib.util
import io
import json
import multiprocessing
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import axiom_common as common  # noqa: E402
import health_check  # noqa: E402
import preflight  # noqa: E402
import schema_guard  # noqa: E402
import stuck_search  # noqa: E402
import write_verify  # noqa: E402

CLI_SPEC = importlib.util.spec_from_file_location(
    "axiom_cli", REPO_ROOT / "scripts" / "axiom_cli.py"
)
assert CLI_SPEC is not None and CLI_SPEC.loader is not None
axiom_cli = importlib.util.module_from_spec(CLI_SPEC)
CLI_SPEC.loader.exec_module(axiom_cli)

# A chmod-based fixture only tests anything where the OS enforces it against the
# current user. POSIX root bypasses the mode bits, and on Windows a 000 file
# stays readable by its owner, so the "unreadable" setup silently succeeds and
# the assertion under test never gets exercised.
PERMISSIONS_UNENFORCEABLE = sys.platform == "win32" or (
    hasattr(os, "geteuid") and os.geteuid() == 0
)


def _append_worker(ledger_path: str, index: int) -> None:
    common.append_ledger(Path(ledger_path), {"index": index})


def _register_worker(
    root: str,
    cwd: str,
    label: str,
    barrier: multiprocessing.synchronize.Barrier,
    results: multiprocessing.queues.Queue,
) -> None:
    barrier.wait(timeout=10)
    result = common.register_claim_if_absent(
        {
            "label": label,
            "predicates": [{"type": "file_exists", "path": f"{label}.txt"}],
        },
        root=root,
        cwd=cwd,
    )
    results.put((result.registered, result.claim))


class AxiomCommonTests(unittest.TestCase):
    def test_corrupt_claim_slot_raises_and_registration_quarantines_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            slot = paths["active_claim"]
            slot.parent.mkdir(parents=True, exist_ok=True)
            slot.write_text("[1, 2", encoding="utf-8")
            with self.assertRaises(common.ClaimSlotCorrupt) as caught:
                common.read_active_claim(root=root, cwd=cwd)
            self.assertEqual(caught.exception.status, "invalid")
            self.assertEqual(caught.exception.path, slot)

            registration = common.register_claim_if_absent(
                {
                    "label": "fresh",
                    "predicates": [{"type": "file_exists", "path": "x"}],
                },
                root=root,
                cwd=cwd,
            )
            self.assertTrue(registration.registered)
            self.assertEqual(
                common.read_active_claim(root=root, cwd=cwd)["label"], "fresh"
            )
            quarantined = sorted(slot.parent.glob("active.json.corrupt-*"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "[1, 2")
            record = common.read_ledger(paths["ledger"])[-1]
            self.assertEqual(record["event"], "claim_unreadable")
            self.assertEqual(record["action"], "quarantined")
            self.assertEqual(record["moved_to"], str(quarantined[0]))
            # The quarantined copy is plugin-managed state: uninstall must see it.
            managed = common.manifest(root=root, cwd=cwd)["managed_paths"]
            self.assertIn(str(quarantined[0]), managed)

    def test_finding_id_is_stable_per_claim_and_blind_to_evidence(self) -> None:
        claim = {
            "claim_id": "abc",
            "label": "x",
            "predicates": [{"type": "file_exists"}],
        }
        first = common.claim_finding_id("write-verify", claim)
        second = common.claim_finding_id(
            "write-verify", {**claim, "label": "renamed", "predicates": []}
        )
        self.assertEqual(first, second)
        self.assertEqual(len(first), 16)
        self.assertNotEqual(first, common.claim_finding_id("schema-guard", claim))
        legacy = {"baseline": {"registered_at": "2026-01-01T00:00:00+00:00"}}
        self.assertEqual(
            common.claim_finding_id("write-verify", legacy),
            common.finding_id("write-verify", "2026-01-01T00:00:00+00:00"),
        )

    def test_fail_open_reports_on_every_channel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "ledger.jsonl"
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                response = common.fail_open(
                    "preflight", ValueError("bad"), ledger=ledger, rule="preflight"
                )
            self.assertEqual(
                response,
                {"systemMessage": "axiom preflight: fail-open (ValueError: bad)"},
            )
            self.assertIn("fail-open (ValueError: bad)", stderr.getvalue())
            record = common.read_ledger(ledger)[-1]
            self.assertEqual(
                (record["event"], record["action"], record["hook"], record["rule"]),
                ("error", "fail_open", "preflight", "preflight"),
            )
            # A ledger that cannot be written must not take the message with it.
            with contextlib.redirect_stderr(io.StringIO()):
                still = common.fail_open(
                    "preflight", ValueError("bad"), ledger=Path(temporary), rule=None
                )
            self.assertIn("systemMessage", still)
            report = common.get_report_data(ledger)
            self.assertEqual(report["fail_open"]["count"], 1)
            self.assertEqual(report["fail_open"]["latest_hook"], "preflight")

    def test_report_renders_blocked_counts_and_fail_open_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "ledger.jsonl"
            claim = {"label": "demo", "claim_id": "c1"}
            common.append_ledger(
                ledger,
                {
                    "event": "would_have_blocked",
                    "hook": "write_verify",
                    "rule": "write-verify",
                    "claim": claim,
                    "failed": [],
                },
            )
            common.append_ledger(
                ledger,
                {
                    "event": "blocked",
                    "hook": "write_verify",
                    "rule": "write-verify",
                    "claim": claim,
                    "failed": [],
                    "reason": "r",
                },
            )
            common.append_ledger(
                ledger,
                {
                    "event": "error",
                    "hook": "stuck_search",
                    "rule": "stuck-search",
                    "error": "KeyError: 'x'",
                    "action": "fail_open",
                },
            )
            common.append_ledger(
                ledger,
                {
                    "event": "claim_unreadable",
                    "hook": "register_claim",
                    "rule": "write-verify",
                    "action": "quarantined",
                },
            )
            output = io.StringIO()
            with redirect_stdout(output):
                axiom_cli._render_report(common.get_report_data(ledger))
            text = output.getvalue()
            self.assertIn("[write-verify] would-have-blocked: 1", text)
            self.assertIn("  blocked: 1", text)
            self.assertIn("  claim_unreadable: 1", text)
            # Both incidents are listed; only the enforced one is marked, so
            # what the rule did is never read as what it would have done.
            incidents = [
                line for line in text.splitlines() if line.startswith("    demo |")
            ]
            self.assertEqual(len(incidents), 2)
            self.assertEqual(
                [line.endswith(" | blocked") for line in incidents], [False, True]
            )
            self.assertIn("== Fail-open ==", text)
            self.assertIn("latest hook: stuck_search", text)
            self.assertIn("latest error: KeyError: 'x'", text)

    def test_read_ledger_keeps_records_containing_unicode_line_breaks(self) -> None:
        # json.dumps leaves U+2028/U+2029/NEL unescaped inside strings, and
        # str.splitlines() breaks on all three: a label carrying one used to
        # split its record in two and lose both halves as invalid JSON.
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "ledger.jsonl"
            labels = ["line sep", "para sep", "next\x85line", "plain"]
            for label in labels:
                common.append_ledger(ledger, {"event": "verified", "label": label})
            records = common.read_ledger(ledger)
            self.assertEqual(len(records), 4)
            self.assertEqual([record["label"] for record in records], labels)

    def test_load_config_reports_absent_valid_and_invalid_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"

            absent = common.load_config(config_path)
            self.assertEqual(absent.status, "absent")
            self.assertEqual(absent.data, {})
            self.assertIsNone(absent.reason)

            config_path.write_text('{"rules": {}}', encoding="utf-8")
            valid = common.load_config(config_path)
            self.assertEqual(valid.status, "valid")
            self.assertEqual(valid.data, {"rules": {}})
            self.assertIsNone(valid.reason)

            config_path.write_text("not json", encoding="utf-8")
            invalid_json = common.load_config(config_path)
            self.assertEqual(invalid_json.status, "invalid")
            self.assertEqual(invalid_json.data, {})
            self.assertIn("JSONDecodeError", invalid_json.reason)

            config_path.write_text("[]", encoding="utf-8")
            non_object = common.load_config(config_path)
            self.assertEqual(non_object.status, "invalid")
            self.assertEqual(non_object.data, {})
            self.assertIn("object", non_object.reason)

    @unittest.skipIf(
        PERMISSIONS_UNENFORCEABLE, "cannot make the config unreadable here"
    )
    def test_load_config_reports_real_unreadable_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config_path.write_text('{"rules": {}}', encoding="utf-8")
            config_path.chmod(0)
            try:
                result = common.load_config(config_path)
            finally:
                config_path.chmod(0o600)

            self.assertEqual(result.status, "unreadable")
            self.assertEqual(result.data, {})
            self.assertIn("PermissionError", result.reason)

    def test_hook_config_degraded_event_is_deduplicated_and_absent_is_quiet(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config_path = base / "config.json"
            ledger = base / "ledger.jsonl"
            invocation_paths: set[str] = set()

            absent = common.load_hook_config(
                config_path,
                ledger=ledger,
                hook="schema_guard",
                degraded_paths=invocation_paths,
            )
            self.assertEqual(absent.status, "absent")
            self.assertFalse(ledger.exists())

            config_path.write_text("broken", encoding="utf-8")
            first = common.load_hook_config(
                config_path,
                ledger=ledger,
                hook="schema_guard",
                degraded_paths=invocation_paths,
            )
            second = common.load_hook_config(
                config_path,
                ledger=ledger,
                hook="schema_guard",
                degraded_paths=invocation_paths,
            )

            self.assertEqual(first.status, "invalid")
            self.assertEqual(second.status, "invalid")
            records = common.read_ledger(ledger)
            self.assertEqual(len(records), 1)
            self.assertEqual(
                {key: records[0][key] for key in ("event", "status", "path", "hook")},
                {
                    "event": "config_degraded",
                    "status": "invalid",
                    "path": str(config_path),
                    "hook": "schema_guard",
                },
            )
            self.assertIn("JSONDecodeError", records[0]["reason"])

    def test_hook_config_records_a_broken_state_once_per_session(self) -> None:
        # Every hook is its own process, so per-process dedup wrote one record
        # per tool call for as long as the config stayed broken (CONTRACTS §3).
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = common.ensure_layout(root=base / "state", cwd=base)

            def load(session: str, hook: str = "preflight") -> str:
                return common.hook_config(paths, {"session_id": session}, hook).status

            def count() -> int:
                return sum(
                    r["event"] == "config_degraded"
                    for r in common.read_ledger(paths["ledger"])
                )

            paths["config"].write_text("broken", encoding="utf-8")
            for hook in ("preflight", "schema_guard", "stuck_search", "preflight"):
                self.assertEqual(load("a", hook), "invalid")
            self.assertEqual(count(), 1)
            load("b")
            self.assertEqual(count(), 2)
            paths["config"].write_text("broken differently", encoding="utf-8")
            load("b")
            load("b")
            self.assertEqual(count(), 3)
            # A repair is forgotten, so the same break afterwards is news.
            paths["config"].write_text("{}", encoding="utf-8")
            self.assertEqual(load("b"), "valid")
            self.assertFalse(paths["config_degraded"].exists())
            paths["config"].write_text("broken differently", encoding="utf-8")
            load("b")
            self.assertEqual(count(), 4)
            # The marker holds a fingerprint, never the config's content.
            self.assertNotIn(
                "broken", paths["config_degraded"].read_text(encoding="utf-8")
            )

    def test_interleaved_sessions_each_record_a_broken_state_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = common.ensure_layout(root=base / "state", cwd=base)
            paths["config"].write_text("broken", encoding="utf-8")
            for session in ("a", "b", "a", "b", "a"):
                common.hook_config(paths, {"session_id": session}, "preflight")
            self.assertEqual(
                [r["event"] for r in common.read_ledger(paths["ledger"])],
                ["config_degraded"] * 2,
            )

    def test_non_utf8_state_and_config_never_raise(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = common.ensure_layout(root=base / "state", cwd=base)
            paths["config_degraded"].write_bytes(b"\xff\xfe{")
            paths["config"].write_text("{}", encoding="utf-8")
            self.assertEqual(
                common.hook_config(paths, {"session_id": "a"}, "preflight").status,
                "valid",
            )
            paths["config"].write_bytes(b"\xff\xfe")
            result = common.hook_config(paths, {"session_id": "a"}, "preflight")
            self.assertEqual(result.status, "invalid")
            self.assertIn("UnicodeDecodeError", result.reason or "")

    def test_config_with_an_oversized_integer_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.json"
            config.write_text('{"x": ' + "1" * 5000 + "}", encoding="utf-8")
            self.assertEqual(common.load_config(config).status, "invalid")
            config.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
            self.assertEqual(common.load_config(config).status, "invalid")

    def test_unwritable_marker_costs_a_duplicate_never_the_record(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            paths = common.ensure_layout(root=base / "state", cwd=base)
            paths["config"].write_text("broken", encoding="utf-8")
            paths["config_degraded"].mkdir()
            for _ in range(2):
                result = common.hook_config(paths, {"session_id": "a"}, "preflight")
                self.assertEqual(result.status, "invalid")
            self.assertEqual(
                [r["event"] for r in common.read_ledger(paths["ledger"])],
                ["config_degraded"] * 2,
            )

    def test_data_root_prefers_argument_then_environment_then_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            argv_root = base / "argv"
            env_root = base / "env"
            home = base / "home"
            environment = {"CLAUDE_PLUGIN_DATA": str(env_root)}

            self.assertEqual(
                common.data_root(
                    ["--data-root", str(argv_root)],
                    environ=environment,
                    home=home,
                ),
                argv_root.resolve(),
            )
            self.assertEqual(
                common.data_root([], environ=environment, home=home),
                env_root.resolve(),
            )
            self.assertEqual(
                common.data_root([], environ={}, home=home),
                (home / ".axiom").resolve(),
            )

    def test_data_root_empty_argument_is_treated_as_unset(self) -> None:
        # hooks.json passes --data-root "${CLAUDE_PLUGIN_DATA}"; with the env
        # var unset the shell yields an empty argv value, which must fall
        # through to the fallbacks instead of resolving to the process cwd.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            env_root = base / "env"
            home = base / "home"
            for argv in (["--data-root", ""], ["--data-root="]):
                self.assertEqual(
                    common.data_root(
                        argv,
                        environ={"CLAUDE_PLUGIN_DATA": str(env_root)},
                        home=home,
                    ),
                    env_root.resolve(),
                )
                self.assertEqual(
                    common.data_root(argv, environ={}, home=home),
                    (home / ".axiom").resolve(),
                )
            self.assertNotEqual(
                common.data_root(["--data-root", ""], environ={}, home=home),
                Path.cwd().resolve(),
            )

    def test_register_and_verify_resolve_tilde_paths_identically(self) -> None:
        # Regression: the registration-side baseline snapshot must resolve
        # "~/x" exactly like the evaluator does. Before the fix, register
        # snapshotted cwd/~/x (exists=False) while verify resolved the real
        # home file, so an untouched file counted as "changed" — a false PASS.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            home = base / "home"
            home.mkdir()
            cwd = base / "project"
            cwd.mkdir()
            probe = home / "probe.txt"
            probe.write_text("original", encoding="utf-8")
            env_patch = {"HOME": str(home)}
            if sys.platform == "win32":
                env_patch["USERPROFILE"] = str(home)
            with mock.patch.dict(os.environ, env_patch):
                registration = common.register_claim_if_absent(
                    {
                        "label": "tilde",
                        "predicates": [{"type": "file_changed", "path": "~/probe.txt"}],
                    },
                    root=base / "state",
                    cwd=cwd,
                )
                baseline = registration.claim["baseline"]["files"]["~/probe.txt"]
                self.assertTrue(baseline["exists"])
                self.assertIsNotNone(baseline["sha256"])

                unchanged = write_verify.evaluate_claim(registration.claim, cwd=cwd)
                self.assertFalse(unchanged["passed"])

                probe.write_text("modified", encoding="utf-8")
                changed = write_verify.evaluate_claim(registration.claim, cwd=cwd)
                self.assertTrue(changed["passed"])

    def test_project_id_hashes_non_git_canonical_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary).resolve()
            expected = hashlib.sha256(str(cwd).encode("utf-8")).hexdigest()[:12]
            self.assertEqual(common.project_id(cwd), expected)

    def test_project_id_distinguishes_linked_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            repo = base / "source"
            linked = base / "linked worktree"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=repo, check=True)
            identity = "axiom" + "@" + "invalid.invalid"
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Axiom Test",
                    "-c",
                    f"user.email={identity}",
                    "commit",
                    "-qm",
                    "seed",
                ],
                cwd=repo,
                check=True,
            )
            subprocess.run(
                ["git", "worktree", "add", "-q", "-b", "linked-test", str(linked)],
                cwd=repo,
                check=True,
            )

            common_dir = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                cwd=repo,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            canonical = (repo / common_dir).resolve()
            base_id = hashlib.sha256(str(canonical).encode("utf-8")).hexdigest()[:12]

            self.assertEqual(common.project_id(repo), base_id)
            self.assertEqual(
                common.project_id(linked),
                f"{base_id}-wt-linked-worktree",
            )

    def test_concurrent_ledger_appends_are_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "ledger.jsonl"
            processes = [
                multiprocessing.Process(
                    target=_append_worker, args=(str(ledger), index)
                )
                for index in range(50)
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)

            lines = ledger.read_text(encoding="utf-8").splitlines()
            records = [json.loads(line) for line in lines]
            self.assertEqual(len(records), 50)
            self.assertEqual({record["index"] for record in records}, set(range(50)))

    def test_config_replacement_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            common.write_config(config_path, {"revision": 0, "payload": "a" * 4096})
            failures: list[str] = []
            stop = threading.Event()

            def reader() -> None:
                while not stop.is_set():
                    try:
                        payload = json.loads(config_path.read_text(encoding="utf-8"))
                        if "revision" not in payload or "payload" not in payload:
                            failures.append("partial object")
                            return
                    except (json.JSONDecodeError, FileNotFoundError) as error:
                        failures.append(str(error))
                        return

            thread = threading.Thread(target=reader)
            thread.start()
            try:
                for revision in range(1, 51):
                    common.write_config(
                        config_path,
                        {"revision": revision, "payload": str(revision) * 4096},
                    )
            finally:
                stop.set()
                thread.join(timeout=5)

            self.assertEqual(failures, [])
            loaded = common.load_config(config_path)
            self.assertEqual(loaded.status, "valid")
            self.assertEqual(loaded.data["revision"], 50)
            self.assertEqual(
                list(config_path.parent.glob(f".{config_path.name}.*.tmp")), []
            )

    def test_payload_parser_tolerates_missing_fields(self) -> None:
        self.assertEqual(
            common.parse_payload(
                {"hook_event_name": "PostToolUseFailure", "error": "bad"}
            ),
            {"event": "PostToolUseFailure", "error": "bad", "stdout": "", "stderr": ""},
        )
        self.assertEqual(
            common.parse_payload(
                {
                    "hook_event_name": "PostToolUse",
                    "tool_response": {"stdout": "ok", "stderr": "warn"},
                }
            ),
            {"event": "PostToolUse", "error": "", "stdout": "ok", "stderr": "warn"},
        )
        self.assertEqual(
            common.parse_payload({}),
            {"event": "", "error": "", "stdout": "", "stderr": ""},
        )

    def test_rule_mode_defaults_to_observe(self) -> None:
        self.assertEqual(common.rule_mode({}, "write_verify"), "observe")
        self.assertEqual(
            common.rule_mode(
                {"rules": {"write_verify": {"mode": "enforce"}}}, "write_verify"
            ),
            "enforce",
        )

    def test_manifest_enumerates_all_managed_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(os.path.realpath(Path(temporary) / "data"))
            cwd = Path(temporary) / "project"
            cwd.mkdir()
            manifest = common.manifest(root=root, cwd=cwd)
            project = root / "v1" / "projects" / common.project_id(cwd)
            self.assertEqual(manifest["schema_version"], "v1")
            self.assertEqual(
                set(manifest["managed_paths"]),
                {
                    str(root / "v1" / "global.json"),
                    str(root / "adapter-events.jsonl"),
                    str(project / "ledger.jsonl"),
                    str(project / "config.json"),
                    str(project / "lessons.md"),
                    str(project / "claims" / "active.json"),
                    str(project / "claims" / "claim.lock"),
                    str(project / "stuck-search.json"),
                    str(project / "preflight.json"),
                    str(project / "config-degraded.json"),
                },
            )

    def test_claim_registration_captures_file_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "state"
            cwd = base / "project"
            cwd.mkdir()
            target = cwd / "result.txt"
            target.write_text("before\n", encoding="utf-8")
            claim = common.register_claim_if_absent(
                {
                    "label": "change result",
                    "predicates": [{"type": "file_changed", "path": "result.txt"}],
                },
                root=root,
                cwd=cwd,
            ).claim
            loaded = common.read_active_claim(root=root, cwd=cwd)
            self.assertEqual(loaded, claim)
            self.assertRegex(
                claim["claim_id"],
                r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            )
            self.assertIn("registered_at", claim)
            self.assertNotIn("registered_at", claim["baseline"])
            self.assertTrue(claim["baseline"]["files"]["result.txt"]["exists"])
            self.assertRegex(
                claim["baseline"]["files"]["result.txt"]["sha256"], r"^[0-9a-f]{64}$"
            )

    def test_report_contract_and_calibration_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "ledger.jsonl"
            start = datetime.now(timezone.utc) - timedelta(days=7)
            common.append_ledger(
                ledger,
                {
                    "event": "heartbeat",
                    "hook": "session_start",
                    "timestamp": start.isoformat(),
                },
            )
            common.append_ledger(
                ledger,
                {
                    "event": "heartbeat",
                    "hook": "session_start",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
            )
            for index in range(3):
                common.append_ledger(
                    ledger,
                    {
                        "event": "would_have_blocked",
                        "hook": "write_verify",
                        "rule": "write-verify",
                        "claim": {"label": f"claim-{index}"},
                        "failed": [{"type": "file_exists", "actual": "missing"}],
                    },
                )
            report = common.get_report_data(ledger)
            self.assertEqual(report["rules"]["write-verify"]["would_have_blocked"], 3)
            self.assertEqual(len(report["rules"]["write-verify"]["recent"]), 3)
            self.assertGreaterEqual(report["coverage"]["heartbeat_days"], 7)
            self.assertEqual(report["coverage"]["event_count"], 5)
            notice = common.calibration_notice(report)
            self.assertIn("3", notice)
            self.assertIn("/axiom:report", notice)

    def test_report_aggregates_and_renders_config_degraded_episodes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "ledger.jsonl"
            for reason in ("JSONDecodeError: first", "PermissionError: latest"):
                common.append_ledger(
                    ledger,
                    {
                        "event": "config_degraded",
                        "status": "unreadable",
                        "path": "/project/config.json",
                        "reason": reason,
                        "hook": "schema_guard",
                    },
                )

            report = common.get_report_data(ledger)
            degraded = report["config_degraded"]
            self.assertEqual(degraded["count"], 2)
            self.assertEqual(degraded["latest_reason"], "PermissionError: latest")
            self.assertEqual(degraded["latest_status"], "unreadable")
            self.assertEqual(degraded["latest_path"], "/project/config.json")

            output = io.StringIO()
            with redirect_stdout(output):
                axiom_cli._render_report(report)
            rendered = output.getvalue()
            self.assertIn("Degraded config", rendered)
            self.assertIn("episodes: 2", rendered)
            self.assertIn("PermissionError: latest", rendered)

    def test_claim_lock_degradation_is_observable_and_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()

            # Patch the locking seam rather than fcntl.flock directly: the
            # module only imports fcntl on POSIX, and what this test cares
            # about is "the lock could not be taken", which is the same
            # degradation on either platform.
            with (
                mock.patch.object(common, "_lock_degraded_emitted", False),
                mock.patch.object(
                    common,
                    "_lock_exclusive",
                    side_effect=OSError("flock unsupported"),
                ),
            ):
                first = common.register_claim_if_absent(
                    {"label": "first", "predicates": []}, root=root, cwd=cwd
                )
                self.assertTrue(first.registered)
                self.assertTrue(
                    common.clear_active_claim(
                        root=root,
                        cwd=cwd,
                        expected_claim_id=first.claim["claim_id"],
                    )
                )
                second = common.register_claim_if_absent(
                    {"label": "second", "predicates": []}, root=root, cwd=cwd
                )
                self.assertTrue(second.registered)

            ledger = common.state_paths(root=root, cwd=cwd)["ledger"]
            degraded_events = [
                record
                for record in common.read_ledger(ledger)
                if record.get("event") == "lock_degraded"
            ]
            self.assertEqual(len(degraded_events), 1)
            self.assertIn("flock unsupported", degraded_events[0]["reason"])

            report = common.get_report_data(ledger)
            self.assertEqual(report["lock_degraded"]["count"], 1)
            self.assertIn("flock unsupported", report["lock_degraded"]["latest_reason"])

            output = io.StringIO()
            with redirect_stdout(output):
                axiom_cli._render_report(report)
            rendered = output.getvalue()
            self.assertIn("Lock degraded", rendered)
            self.assertIn("mutual exclusion is degraded", rendered)

    def test_goal_acceptance_json_registers_on_session_start_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            (cwd / "release.goal.md").write_text(
                "# Release\n\n## acceptance\n\n"
                "```json\n"
                '{"predicates":[{"type":"file_exists","path":"dist/app.js"}]}\n'
                "```\n",
                encoding="utf-8",
            )
            result = common.register_goal_claim(root=root, cwd=cwd)
            assert result is not None
            self.assertTrue(result.registered)
            self.assertEqual(result.claim["label"], "release")
            self.assertEqual(
                common.read_active_claim(root=root, cwd=cwd)["predicates"][0]["type"],
                "file_exists",
            )

    def test_register_claim_if_absent_returns_typed_winner_and_loser(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, cwd = Path(temporary) / "state", Path(temporary) / "project"
            cwd.mkdir()

            winner = common.register_claim_if_absent(
                {
                    "label": "winner",
                    "predicates": [{"type": "file_exists", "path": "winner.txt"}],
                },
                root=root,
                cwd=cwd,
            )
            loser = common.register_claim_if_absent(
                {
                    "label": "loser",
                    "predicates": [{"type": "file_exists", "path": "loser.txt"}],
                },
                root=root,
                cwd=cwd,
            )

            self.assertIsInstance(winner, common.ClaimRegistration)
            self.assertTrue(winner.registered)
            self.assertFalse(loser.registered)
            self.assertEqual(loser.claim, winner.claim)
            self.assertEqual(common.read_active_claim(root=root, cwd=cwd), winner.claim)

    def test_two_process_register_race_has_exactly_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, cwd = Path(temporary) / "state", Path(temporary) / "project"
            cwd.mkdir()
            barrier = multiprocessing.Barrier(2)
            results: multiprocessing.Queue = multiprocessing.Queue()
            processes = [
                multiprocessing.Process(
                    target=_register_worker,
                    args=(str(root), str(cwd), label, barrier, results),
                )
                for label in ("A", "B")
            ]

            for process in processes:
                process.start()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)

            outcomes = [results.get(timeout=2) for _ in processes]
            self.assertEqual([registered for registered, _ in outcomes].count(True), 1)
            self.assertEqual([registered for registered, _ in outcomes].count(False), 1)
            winner = next(claim for registered, claim in outcomes if registered)
            loser_observation = next(
                claim for registered, claim in outcomes if not registered
            )
            self.assertEqual(loser_observation, winner)
            self.assertEqual(common.read_active_claim(root=root, cwd=cwd), winner)

    def test_register_goal_claim_routes_only_through_atomic_primitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, cwd = Path(temporary) / "state", Path(temporary) / "project"
            cwd.mkdir()
            (cwd / "release.goal.md").write_text(
                "# Release\n\n## acceptance\n\n"
                "```json\n"
                '{"predicates":[{"type":"file_exists","path":"dist/app.js"}]}\n'
                "```\n",
                encoding="utf-8",
            )
            existing = {"claim_id": "existing", "predicates": []}
            loser = common.ClaimRegistration(registered=False, claim=existing)

            with (
                mock.patch.object(
                    common,
                    "read_active_claim",
                    side_effect=AssertionError("external check-then-act"),
                ),
                mock.patch.object(
                    common, "register_claim_if_absent", return_value=loser
                ) as register,
            ):
                result = common.register_goal_claim(root=root, cwd=cwd)

            self.assertEqual(result, loser)
            register.assert_called_once()


def _init_repo(path: Path) -> None:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Axiom Test"], cwd=path, check=True)
    identity = "axiom" + "@" + "invalid.invalid"
    subprocess.run(["git", "config", "user.email", identity], cwd=path, check=True)


def _commit_all(path: Path, message: str = "seed") -> None:
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=path, check=True)


class WriteVerifyCounterexampleTests(unittest.TestCase):
    def test_a4_1_stale_file_fails_file_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            (cwd / "artifact.txt").write_text("stale\n", encoding="utf-8")
            claim = common.register_claim_if_absent(
                {
                    "label": "stale file",
                    "predicates": [
                        {"type": "file_exists", "path": "artifact.txt"},
                        {"type": "file_changed", "path": "artifact.txt"},
                    ],
                },
                root=root,
                cwd=cwd,
            ).claim
            result = write_verify.evaluate_claim(claim, cwd=cwd)
            self.assertFalse(result["passed"])
            failed = [item for item in result["evidence"] if not item["passed"]]
            self.assertEqual(failed[0]["type"], "file_changed")
            print("A4-1 stale file: observed FAIL (file_changed=false)")

    def test_a4_2_unrelated_dirty_diff_does_not_satisfy_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "repo"
            _init_repo(cwd)
            (cwd / "target.txt").write_text("target\n", encoding="utf-8")
            (cwd / "other.txt").write_text("before\n", encoding="utf-8")
            _commit_all(cwd)
            claim = common.register_claim_if_absent(
                {
                    "label": "target-only",
                    "predicates": [{"type": "file_changed", "path": "target.txt"}],
                },
                root=root,
                cwd=cwd,
            ).claim
            (cwd / "other.txt").write_text("dirty\n", encoding="utf-8")
            result = write_verify.evaluate_claim(claim, cwd=cwd)
            self.assertFalse(result["passed"])
            self.assertEqual(result["evidence"][0]["actual"], "unchanged")
            print(
                "A4-2 unrelated dirty diff: observed FAIL (declared target unchanged)"
            )

    def test_a4_3_tests_not_run_is_decided_by_fresh_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary)
            command = subprocess.list2cmdline(
                [sys.executable, "-c", "raise SystemExit(7)"]
            )
            claim = {
                "label": "fresh command",
                "predicates": [{"type": "cmd_succeeds", "cmd": command, "timeout": 10}],
                "baseline": {},
            }
            result = write_verify.evaluate_claim(claim, cwd=cwd)
            self.assertFalse(result["passed"])
            self.assertEqual(result["evidence"][0]["actual"], "exit 7")
            print("A4-3 tests not run: observed FAIL (fresh command exit=7)")

    def test_a4_4_old_transcript_success_is_not_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            transcript = base / "transcript.jsonl"
            transcript.write_text('{"tool_result":"tests passed"}\n', encoding="utf-8")
            common.register_claim_if_absent(
                {
                    "label": "ignore transcript",
                    "predicates": [{"type": "file_exists", "path": "missing.txt"}],
                },
                root=root,
                cwd=cwd,
            )
            common.write_config(
                common.state_paths(root=root, cwd=cwd)["config"],
                {"rules": {"write-verify": {"mode": "enforce"}}},
            )
            response = write_verify.process_stop(
                {"cwd": str(cwd), "transcript_path": str(transcript)}, root=root
            )
            self.assertEqual(response["decision"], "block")
            self.assertIn("missing.txt", response["reason"])
            print(
                "A4-4 old log: observed FAIL (missing file blocked despite old success text)"
            )

    def test_a4_5_wrong_worktree_has_no_foreign_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, source, linked = base / "state", base / "source", base / "linked"
            _init_repo(source)
            (source / "seed.txt").write_text("seed\n", encoding="utf-8")
            _commit_all(source)
            subprocess.run(
                ["git", "worktree", "add", "-q", "-b", "linked", str(linked)],
                cwd=source,
                check=True,
            )
            common.register_claim_if_absent(
                {
                    "label": "source only",
                    "predicates": [{"type": "file_changed", "path": "seed.txt"}],
                },
                root=root,
                cwd=source,
            )
            response = write_verify.process_stop({"cwd": str(linked)}, root=root)
            self.assertIsNone(response)
            self.assertIsNotNone(common.read_active_claim(root=root, cwd=source))
            records = common.read_ledger(
                common.state_paths(root=root, cwd=linked)["ledger"]
            )
            self.assertEqual(records[-1]["event"], "unverified_completion")
            print(
                "A4-5 wrong worktree: observed FAIL/no-claim (foreign claim untouched)"
            )


class WriteVerifyTests(unittest.TestCase):
    def test_posttooluse_records_stat_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            target = cwd / "written.txt"
            target.write_text("content\n", encoding="utf-8")
            response = write_verify.process_posttooluse(
                {
                    "hook_event_name": "PostToolUse",
                    "tool_name": "Write",
                    "tool_input": {"file_path": "written.txt"},
                    "cwd": str(cwd),
                },
                root=root,
            )
            self.assertIsNone(response)
            record = common.read_ledger(
                common.state_paths(root=root, cwd=cwd)["ledger"]
            )[-1]
            self.assertEqual(record["event"], "write_readback")
            self.assertTrue(record["verified"])
            self.assertGreater(record["stat"]["size"], 0)

    def test_reentrant_second_failure_escalates_and_allows_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            common.register_claim_if_absent(
                {
                    "label": "missing output",
                    "predicates": [{"type": "file_exists", "path": "missing.txt"}],
                },
                root=root,
                cwd=cwd,
            )
            common.write_config(
                common.state_paths(root=root, cwd=cwd)["config"],
                {"rules": {"write-verify": {"mode": "enforce"}}},
            )
            response = write_verify.process_stop(
                {"cwd": str(cwd), "stop_hook_active": True}, root=root
            )
            self.assertIsNone(response)
            records = common.read_ledger(
                common.state_paths(root=root, cwd=cwd)["ledger"]
            )
            self.assertEqual(records[-1]["event"], "escalation")

    def test_observe_default_records_would_have_blocked_and_keeps_claim(self) -> None:
        # The product DEFAULT: no config written -> observe mode. A failing
        # claim must let the turn stop (None), record would_have_blocked with
        # the failed predicates, and leave the claim active for the next stop.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            common.register_claim_if_absent(
                {
                    "label": "missing output",
                    "predicates": [{"type": "file_exists", "path": "missing.txt"}],
                },
                root=root,
                cwd=cwd,
            )
            response = write_verify.process_stop(
                {"cwd": str(cwd), "stop_hook_active": False}, root=root
            )
            self.assertIsNone(response)
            records = common.read_ledger(
                common.state_paths(root=root, cwd=cwd)["ledger"]
            )
            self.assertEqual(records[-1]["event"], "would_have_blocked")
            self.assertTrue(records[-1]["failed"])
            self.assertIsNotNone(common.read_active_claim(root=root, cwd=cwd))

    def test_successful_predicates_clear_active_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            target = cwd / "output.txt"
            target.write_text("before\n", encoding="utf-8")
            common.register_claim_if_absent(
                {
                    "label": "complete output",
                    "predicates": [
                        {"type": "file_exists", "path": "output.txt"},
                        {
                            "type": "file_contains",
                            "path": "output.txt",
                            "pattern": "aft.r",
                        },
                        {"type": "file_changed", "path": "output.txt"},
                    ],
                },
                root=root,
                cwd=cwd,
            )
            target.write_text("after\n", encoding="utf-8")
            self.assertIsNone(write_verify.process_stop({"cwd": str(cwd)}, root=root))
            self.assertIsNone(common.read_active_claim(root=root, cwd=cwd))
            record = common.read_ledger(
                common.state_paths(root=root, cwd=cwd)["ledger"]
            )[-1]
            self.assertEqual(record["event"], "verified")

    def test_cmd_succeeds_rejects_shell_injection_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary)
            marker = cwd / "injected"
            claim = {
                "predicates": [
                    {
                        "type": "cmd_succeeds",
                        "cmd": f"python3 -c pass; touch {marker}",
                    }
                ],
                "baseline": {},
            }
            result = write_verify.evaluate_claim(claim, cwd=cwd)
            self.assertFalse(result["passed"])
            self.assertIn("rejected", result["evidence"][0]["actual"])
            self.assertFalse(marker.exists())

    def test_verifier_exception_records_error_and_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            with mock.patch.object(
                common, "read_active_claim", side_effect=RuntimeError("boom")
            ):
                response = write_verify.process_stop({"cwd": str(cwd)}, root=root)
            # Fail-open means no decision -- the turn may end -- but the host
            # is told, so the operator can see that nothing was checked.
            assert response is not None
            self.assertNotIn("decision", response)
            self.assertIn("fail-open (RuntimeError: boom)", response["systemMessage"])
            record = common.read_ledger(
                common.state_paths(root=root, cwd=cwd)["ledger"]
            )[-1]
            self.assertEqual(record["event"], "error")
            self.assertEqual(record["action"], "fail_open")
            self.assertEqual(record["rule"], "write-verify")

    def test_enforced_block_is_recorded_and_shares_its_finding_id(self) -> None:
        # Once a rule is enforced the observe record stops; without a `blocked`
        # record the ledger would go blind exactly when it starts to matter.
        # The block, and the `verified` that later closes it, carry one id.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            common.register_claim_if_absent(
                {
                    "label": "needs output",
                    "predicates": [{"type": "file_exists", "path": "out.txt"}],
                },
                root=root,
                cwd=cwd,
            )
            common.write_config(
                common.state_paths(root=root, cwd=cwd)["config"],
                {"rules": {"write-verify": {"mode": "enforce"}}},
            )
            blocked = write_verify.process_stop(
                {"cwd": str(cwd), "stop_hook_active": False}, root=root
            )
            assert blocked is not None
            self.assertEqual(blocked["decision"], "block")
            ledger = common.state_paths(root=root, cwd=cwd)["ledger"]
            record = common.read_ledger(ledger)[-1]
            self.assertEqual(record["event"], "blocked")
            self.assertEqual(record["reason"], blocked["reason"])
            self.assertEqual(record["failed"][0]["type"], "file_exists")
            self.assertEqual(len(record["finding_id"]), 16)

            (cwd / "out.txt").write_text("done\n", encoding="utf-8")
            self.assertIsNone(
                write_verify.process_stop(
                    {"cwd": str(cwd), "stop_hook_active": False}, root=root
                )
            )
            verified = common.read_ledger(ledger)[-1]
            self.assertEqual(verified["event"], "verified")
            self.assertEqual(verified["finding_id"], record["finding_id"])

    def test_failed_block_record_does_not_turn_the_block_into_a_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            common.register_claim_if_absent(
                {"label": "x", "predicates": [{"type": "file_exists", "path": "no"}]},
                root=root,
                cwd=cwd,
            )
            common.write_config(
                common.state_paths(root=root, cwd=cwd)["config"],
                {"rules": {"write-verify": {"mode": "enforce"}}},
            )
            with mock.patch.object(
                common, "append_ledger", side_effect=OSError("disk full")
            ):
                response = write_verify.process_stop(
                    {"cwd": str(cwd), "stop_hook_active": False}, root=root
                )
            assert response is not None
            self.assertEqual(response["decision"], "block")

    def _corrupt_slot(self, root: Path, cwd: Path) -> Path:
        slot = common.state_paths(root=root, cwd=cwd)["active_claim"]
        slot.parent.mkdir(parents=True, exist_ok=True)
        slot.write_text("{not json", encoding="utf-8")
        return slot

    def test_corrupt_slot_is_recorded_in_observe_and_never_read_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            slot = self._corrupt_slot(root, cwd)
            self.assertIsNone(
                write_verify.process_stop(
                    {"cwd": str(cwd), "stop_hook_active": False}, root=root
                )
            )
            records = common.read_ledger(
                common.state_paths(root=root, cwd=cwd)["ledger"]
            )
            events = [record["event"] for record in records]
            self.assertNotIn("unverified_completion", events)
            self.assertEqual(records[-1]["event"], "claim_unreadable")
            self.assertEqual(records[-1]["status"], "invalid")
            self.assertEqual(records[-1]["action"], "observed")
            self.assertEqual(slot.read_text(encoding="utf-8"), "{not json")

    def test_corrupt_slot_blocks_once_in_enforce_then_fails_open_on_reentry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            self._corrupt_slot(root, cwd)
            common.write_config(
                common.state_paths(root=root, cwd=cwd)["config"],
                {"rules": {"write-verify": {"mode": "enforce"}}},
            )
            first = write_verify.process_stop(
                {"cwd": str(cwd), "stop_hook_active": False}, root=root
            )
            assert first is not None
            self.assertEqual(first["decision"], "block")
            self.assertIn("active claim slot is invalid", first["reason"])
            self.assertIn(common.escape_hatch("write-verify"), first["reason"])
            # The reason reaches the model: it must not hand over the one step
            # that turns the block into a free pass (delete the slot -> next
            # Stop reads "no claim" -> unverified_completion).
            self.assertIn("do not delete", first["reason"])
            self.assertNotIn("remove", first["reason"])
            second = write_verify.process_stop(
                {"cwd": str(cwd), "stop_hook_active": True}, root=root
            )
            self.assertIsNone(second)
            records = common.read_ledger(
                common.state_paths(root=root, cwd=cwd)["ledger"]
            )
            self.assertEqual(
                [(r["event"], r["action"]) for r in records[-2:]],
                [
                    ("claim_unreadable", "blocked"),
                    ("claim_unreadable", "fail_open_after_reentry"),
                ],
            )

    def test_corrupt_slot_block_survives_an_unwritable_ledger(self) -> None:
        # Same best-effort guarantee as the claim block (CONTRACTS §6): a
        # ledger that cannot take the record must not turn the block into a
        # pass, and the loss is said out loud.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            self._corrupt_slot(root, cwd)
            common.write_config(
                common.state_paths(root=root, cwd=cwd)["config"],
                {"rules": {"write-verify": {"mode": "enforce"}}},
            )
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    common, "append_ledger", side_effect=OSError("disk full")
                ),
                contextlib.redirect_stderr(stderr),
            ):
                response = write_verify.process_stop(
                    {"cwd": str(cwd), "stop_hook_active": False}, root=root
                )
            assert response is not None
            self.assertEqual(response["decision"], "block")
            self.assertIn("claim_unreadable record NOT confirmed", stderr.getvalue())

    def test_record_best_effort_reports_and_returns_false(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "ledger.jsonl"
            self.assertTrue(common.record_best_effort(ledger, {"event": "blocked"}))
            self.assertEqual(common.read_ledger(ledger)[0]["event"], "blocked")
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    common, "append_ledger", side_effect=OSError("disk full")
                ),
                contextlib.redirect_stderr(stderr),
            ):
                confirmed = common.record_best_effort(ledger, {"event": "blocked"})
            self.assertFalse(confirmed)
            self.assertIn("blocked record NOT confirmed", stderr.getvalue())
            self.assertIn("disk full", stderr.getvalue())

    def test_quarantine_rename_failure_leaves_the_slot_and_raises(self) -> None:
        # A rename that fails must not be papered over: no new claim is
        # written over the corrupt bytes, the bytes stay where they were, and
        # the error reaches the caller's fail-open handler (which records it).
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            slot = self._corrupt_slot(root, cwd)
            with (
                mock.patch.object(
                    common, "_atomic_replace", side_effect=OSError("rename refused")
                ),
                self.assertRaises(OSError),
            ):
                common.register_claim_if_absent(
                    {"label": "fresh", "predicates": []}, root=root, cwd=cwd
                )
            self.assertEqual(slot.read_text(encoding="utf-8"), "{not json")
            self.assertEqual(list(slot.parent.glob("active.json.corrupt-*")), [])
            with self.assertRaises(common.ClaimSlotCorrupt):
                common.read_active_claim(root=root, cwd=cwd)


class TempRootsTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep the simulated project outside the OS temp roots so an explicit
        # /tmp or native macOS root cannot mask the gettempdir regression.
        temporary = tempfile.TemporaryDirectory(dir=REPO_ROOT)
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.cwd = self.base / "project"
        self.cwd.mkdir()
        previous = Path.cwd()
        os.chdir(self.cwd)
        self.addCleanup(os.chdir, previous)

    def test_gettempdir_cwd_and_ancestors_are_excluded(self) -> None:
        for value in (
            str(self.cwd),
            ".",
            str(self.cwd / ".." / "project"),
            str(self.base),
            self.cwd.anchor,
        ):
            with (
                self.subTest(value=value),
                mock.patch.object(tempfile, "gettempdir", return_value=value),
            ):
                self.assertNotIn(
                    Path(value).resolve(), common.temp_roots([], environ={})
                )

    def test_gettempdir_other_roots_remain(self) -> None:
        for path in (self.base / "project-temp", self.cwd / "cache"):
            with (
                self.subTest(path=path),
                mock.patch.object(tempfile, "gettempdir", return_value=str(path)),
            ):
                self.assertIn(path, common.temp_roots([], environ={}))

    def test_explicit_roots_keep_cwd_and_ancestors(self) -> None:
        with mock.patch.object(tempfile, "gettempdir", return_value=str(self.cwd)):
            for path in (self.cwd, self.base):
                with self.subTest(configured=path):
                    self.assertIn(path, common.temp_roots([str(path)], environ={}))
                for variable in ("TMPDIR", "TEMP", "TMP"):
                    with self.subTest(path=path, variable=variable):
                        self.assertIn(
                            path, common.temp_roots([], environ={variable: str(path)})
                        )

    def test_gettempdir_errors_keep_explicit_roots(self) -> None:
        with mock.patch.object(tempfile, "gettempdir", side_effect=OSError):
            roots = common.temp_roots(
                [str(self.base)], environ={"TMPDIR": str(self.cwd)}
            )
        self.assertIn(self.base, roots)
        self.assertIn(self.cwd, roots)

    def test_cwd_fallback_keeps_consumer_decisions_non_temporary(self) -> None:
        # The fix must not change either consumer's decision: a gettempdir()
        # that falls back to the cwd (or an ancestor) has to behave exactly
        # like one that points somewhere unrelated. Comparing the two keeps the
        # test independent of whether the test directory itself sits under a
        # temp root, which it does on Linux CI.
        root = self.base / "state"
        paths = common.state_paths(root=root, cwd=self.cwd)
        common.write_config(
            paths["config"],
            {
                "rules": {
                    "schema-guard": {"mode": "enforce", "tmp_paths": []},
                    "preflight": {"mode": "enforce", "cooldown_minutes": 0},
                }
            },
        )
        target = shlex.quote((self.cwd / "build").as_posix())

        def decisions() -> tuple[object, object]:
            guard = schema_guard.process(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": "config.json"},
                    "cwd": str(self.cwd),
                },
                root=root,
                environ={},
            )
            flight = preflight.process(
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": f"rm -rf {target}"},
                    "cwd": str(self.cwd),
                },
                root=root,
                environ={},
            )
            return guard, flight

        unrelated = str(self.base / "elsewhere-not-a-temp-root")
        with mock.patch.object(tempfile, "gettempdir", return_value=unrelated):
            baseline = decisions()
        for value in (str(self.cwd), ".", str(self.base)):
            with (
                self.subTest(value=value),
                mock.patch.object(tempfile, "gettempdir", return_value=value),
            ):
                self.assertNotIn(
                    Path(value).resolve(), common.temp_roots([], environ={})
                )
                self.assertEqual(decisions(), baseline)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS libc and getconf")
    def test_darwin_root_without_tmpdir_matches_getconf(self) -> None:
        environment = dict(os.environ)
        for variable in ("TMPDIR", "TEMP", "TMP"):
            environment.pop(variable, None)
        result = subprocess.run(
            ["getconf", "DARWIN_USER_TEMP_DIR"],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
        if result.returncode == 71:
            self.skipTest(
                "system temporary-directory probe unavailable (getconf exit 71)"
            )
        result.check_returncode()
        self.assertTrue(result.stdout.strip())
        expected = Path(result.stdout.strip()).resolve()
        root = self.base / "state"
        common.write_config(
            common.state_paths(root=root, cwd=self.cwd)["config"],
            {"rules": {"schema-guard": {"mode": "enforce", "tmp_paths": []}}},
        )
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(tempfile, "tempdir", None),
            mock.patch.object(
                subprocess, "run", side_effect=AssertionError("hook subprocess")
            ) as run,
        ):
            # gettempdir alone does not discover the per-user directory.
            self.assertNotEqual(Path(tempfile.gettempdir()).resolve(), expected)
            self.assertIn(expected, common.temp_roots([], environ={}))
            run.assert_not_called()

        # Exercise both consumers with the same missing-environment conditions.
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(tempfile, "tempdir", None),
        ):
            response = schema_guard.process(
                {
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(expected / "config.json")},
                    "cwd": str(self.cwd),
                },
                root=root,
                environ={},
            )
            self.assertEqual(
                response["hookSpecificOutput"]["permissionDecision"], "deny"
            )
            target = shlex.quote(str(expected / "build"))
            self.assertIsNone(preflight.detect_pattern(f"rm -rf {target}", environ={}))

    def test_darwin_discovery_failures_keep_other_roots(self) -> None:
        for failure in (OSError, AttributeError, RuntimeError):
            with (
                self.subTest(failure=failure),
                mock.patch.object(sys, "platform", "darwin"),
                mock.patch.object(ctypes, "CDLL", side_effect=failure),
                mock.patch.object(tempfile, "gettempdir", return_value=str(self.cwd)),
            ):
                self.assertEqual(
                    common.temp_roots([str(self.base)], environ={}), [self.base]
                )

    def test_darwin_confstr_invalid_results_are_ignored(self) -> None:
        for results in ([0], [16, 0], [16, 17], [16, OSError()]):
            with (
                self.subTest(results=results),
                mock.patch.object(sys, "platform", "darwin"),
                mock.patch.object(ctypes, "CDLL") as libc,
                mock.patch.object(tempfile, "gettempdir", return_value=str(self.cwd)),
            ):
                libc.return_value.confstr.side_effect = results
                self.assertEqual(
                    common.temp_roots([str(self.base)], environ={}), [self.base]
                )

    def test_other_platforms_do_not_load_libc(self) -> None:
        for platform in ("linux", "win32"):
            with (
                self.subTest(platform=platform),
                mock.patch.object(sys, "platform", platform),
                mock.patch.object(ctypes, "CDLL") as libc,
                mock.patch.object(tempfile, "gettempdir", return_value=str(self.cwd)),
            ):
                self.assertEqual(common.temp_roots([], environ={}), [])
                libc.assert_not_called()


class SchemaGuardTests(unittest.TestCase):
    def test_shell_targets_are_advisory_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd, tmp = base / "data", base / "project", base / "temp space"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            target = (tmp / "ledger.jsonl").as_posix()
            cases = [
                (f'echo x > "{target}"', True),
                (f'echo x >> "{target}"', True),
                (f'echo 1 > "{target}"', True),
                (f'cmd 2>"{tmp.as_posix()}/state.log"', False),
                (f'cmd 2>>"{target}"', False),
                (f'cmd &>"{target}"', False),
                ("cmd >&2", False),
                (f'echo "see {target}"', False),
                (f'printf x | tee -a "{tmp.as_posix()}/history.jsonl"', True),
                (f'sqlite3 "{tmp.as_posix()}/state.db"', True),
                (f'sqlite3 -readonly "{tmp.as_posix()}/state.db"', True),
                (f'sqlite3 "{cwd.as_posix()}/data.db" "{target}"', False),
                (f'echo x > "{target}', False),
                (f'echo x > "{cwd.as_posix()}/ledger.jsonl"', False),
            ]
            for mode in ("observe", "enforce"):
                common.write_config(
                    paths["config"], {"rules": {"schema-guard": {"mode": mode}}}
                )
                for command, finding in cases:
                    with self.subTest(mode=mode, command=command):
                        before = len(common.read_ledger(paths["ledger"]))
                        with mock.patch.object(
                            common, "temp_roots", return_value=[tmp.resolve()]
                        ):
                            response = schema_guard.process(
                                {
                                    "hook_event_name": "PreToolUse",
                                    "tool_name": "Bash",
                                    "tool_input": {"command": command},
                                    "cwd": str(cwd),
                                },
                                root=root,
                                environ={"TMPDIR": str(tmp)},
                            )
                        records = common.read_ledger(paths["ledger"])[before:]
                        if mode == "observe" and finding:
                            self.assertIsNone(response)
                            self.assertEqual(len(records), 1)
                            self.assertEqual(records[0]["event"], "would_have_blocked")
                            self.assertEqual(
                                records[0]["basis"],
                                "shell write target in temporary storage",
                            )
                        elif finding:
                            output = response["hookSpecificOutput"]
                            self.assertIn("additionalContext", output)
                            self.assertNotIn("permissionDecision", output)
                            # The advisory is recorded as what it is, under
                            # the id the observe record for this target
                            # carried in the previous pass.
                            self.assertEqual(len(records), 1)
                            self.assertEqual(records[0]["event"], "advice_injected")
                            self.assertEqual(
                                records[0]["basis"],
                                "shell write target in temporary storage",
                            )
                            observed = [
                                r
                                for r in common.read_ledger(paths["ledger"])
                                if r["event"] == "would_have_blocked"
                                and r["basis"] == records[0]["basis"]
                            ]
                            self.assertIn(
                                records[0]["finding_id"],
                                {r["finding_id"] for r in observed},
                            )
                        else:
                            self.assertIsNone(response)
                            self.assertEqual(records, [])

    def test_one_unparsable_redirect_keeps_the_other_targets(self) -> None:
        targets = schema_guard._bash_write_targets(
            'echo x > /scratch/ledger.jsonl; echo y > "/scratch/unclosed'
        )
        self.assertEqual(targets, ["/scratch/ledger.jsonl"])

    def test_shell_targets_use_shared_roots_and_configured_patterns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd, tmp = base / "data", base / "project", base / "temp"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            common.write_config(
                paths["config"],
                {
                    "rules": {
                        "schema-guard": {
                            "tmp_paths": [],
                            "persist_patterns": [r"^archive$"],
                        }
                    }
                },
            )
            with mock.patch.dict(
                os.environ, {"HOME": str(tmp), "USERPROFILE": str(tmp)}
            ):
                for target, expected in (("~/archive", 1), ("~/ledger.jsonl", 0)):
                    before = len(common.read_ledger(paths["ledger"]))
                    schema_guard.process(
                        {
                            "tool_name": "Bash",
                            "cwd": str(cwd),
                            "tool_input": {"command": f"echo x > {target}"},
                        },
                        root=root,
                        environ={"TMPDIR": str(tmp)},
                    )
                    self.assertEqual(
                        len(common.read_ledger(paths["ledger"])) - before, expected
                    )

    def test_scratch_exemptions_use_resolved_directory_prefixes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd, tmp = base / "data", base / "project", base / "temp"
            cwd.mkdir()
            scratch = tmp / "claude-501"
            extra = tmp / "extra"
            paths = common.ensure_layout(root=root, cwd=cwd)
            common.write_config(
                paths["config"],
                {
                    "rules": {
                        "schema-guard": {
                            "mode": "enforce",
                            "exempt_paths": [str(extra)],
                        }
                    }
                },
            )
            cases = [
                (scratch / "proj/session/scratchpad/state.json", False),
                (extra / "state.json", False),
                (tmp / "claude-ledger.jsonl", True),
                (tmp / "x/claude-state.json", True),
                (tmp / "my-scratchpad-ledger.jsonl", True),
                (tmp / "claude-501-evil/state.json", True),
                (scratch / "../other/state.json", True),
            ]
            with mock.patch.object(os, "getuid", return_value=501, create=True):
                for candidate, finding in cases:
                    for tool in ("Write", "Bash"):
                        with self.subTest(candidate=candidate, tool=tool):
                            tool_input = (
                                {"file_path": str(candidate)}
                                if tool == "Write"
                                else {"command": f'echo x > "{candidate.as_posix()}"'}
                            )
                            response = schema_guard.process(
                                {
                                    "tool_name": tool,
                                    "tool_input": tool_input,
                                    "cwd": str(cwd),
                                },
                                root=root,
                                environ={"TMPDIR": str(tmp)},
                            )
                            self.assertEqual(response is not None, finding)
                            if finding and tool == "Write":
                                self.assertEqual(
                                    response["hookSpecificOutput"][
                                        "permissionDecision"
                                    ],
                                    "deny",
                                )

    @unittest.skipIf(sys.platform == "win32", "symlink fixture requires POSIX")
    def test_scratch_symlink_escape_is_not_exempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd, tmp = base / "data", base / "project", base / "temp"
            cwd.mkdir()
            scratch, other = tmp / "claude-501", tmp / "other"
            scratch.mkdir(parents=True)
            other.mkdir()
            (scratch / "link").symlink_to(other, target_is_directory=True)
            paths = common.ensure_layout(root=root, cwd=cwd)
            with mock.patch.object(os, "getuid", return_value=501):
                schema_guard.process(
                    {
                        "tool_name": "Write",
                        "cwd": str(cwd),
                        "tool_input": {"file_path": str(scratch / "link/state.json")},
                    },
                    root=root,
                    environ={"TMPDIR": str(tmp)},
                )
            self.assertEqual(len(common.read_ledger(paths["ledger"])), 1)

    def test_invalid_config_fails_open_and_records_degraded_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd, tmp = base / "state", base / "project", base / "tmp"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            paths["config"].write_text("not json", encoding="utf-8")

            response = schema_guard.process(
                {
                    "tool_name": "Edit",
                    "tool_input": {"file_path": str(tmp / "state.db")},
                    "cwd": str(cwd),
                },
                root=root,
                environ={"TMPDIR": str(tmp)},
            )

            self.assertIsNone(response)
            records = common.read_ledger(paths["ledger"])
            self.assertEqual(
                [record["event"] for record in records],
                ["config_degraded", "would_have_blocked"],
            )
            self.assertEqual(records[0]["hook"], "schema_guard")
            self.assertEqual(records[0]["status"], "invalid")

    def test_observe_records_persistent_file_in_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd, tmp = base / "state", base / "project", base / "tmp"
            cwd.mkdir()
            response = schema_guard.process(
                {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Write",
                    "tool_input": {"file_path": str(tmp / "history.jsonl")},
                    "cwd": str(cwd),
                },
                root=root,
                environ={"TMPDIR": str(tmp)},
            )
            self.assertIsNone(response)
            record = common.read_ledger(
                common.state_paths(root=root, cwd=cwd)["ledger"]
            )[-1]
            self.assertEqual(record["event"], "would_have_blocked")
            self.assertEqual(record["rule"], "schema-guard")

    def test_enforce_denies_with_escape_hatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd, tmp = base / "state", base / "project", base / "tmp"
            cwd.mkdir()
            common.write_config(
                common.state_paths(root=root, cwd=cwd)["config"],
                {"rules": {"schema-guard": {"mode": "enforce"}}},
            )
            response = schema_guard.process(
                {
                    "tool_name": "Edit",
                    "tool_input": {"file_path": str(tmp / "state.db")},
                    "cwd": str(cwd),
                },
                root=root,
                environ={"TMPDIR": str(tmp)},
            )
            output = response["hookSpecificOutput"]
            self.assertEqual(output["permissionDecision"], "deny")
            self.assertIn("expected", output["permissionDecisionReason"])
            self.assertIn(
                "/axiom:enforce schema-guard off", output["permissionDecisionReason"]
            )
            # The deny is recorded as what it is, with the same finding id the
            # observe record for this path would carry.
            record = common.read_ledger(
                common.state_paths(root=root, cwd=cwd)["ledger"]
            )[-1]
            self.assertEqual(record["event"], "blocked")
            self.assertEqual(record["rule"], "schema-guard")
            # The hook hashes the resolved path (macOS /var -> /private/var,
            # Windows short names), so derive the expectation the same way.
            self.assertEqual(
                record["finding_id"],
                common.finding_id("schema-guard", str((tmp / "state.db").resolve())),
            )

    def test_deny_survives_an_unwritable_ledger(self) -> None:
        # CONTRACTS §6: a failed `blocked` append never converts a deny into
        # an allow. The record is reported lost on stderr; the decision holds.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd, tmp = base / "state", base / "project", base / "tmp"
            cwd.mkdir()
            common.write_config(
                common.state_paths(root=root, cwd=cwd)["config"],
                {"rules": {"schema-guard": {"mode": "enforce"}}},
            )
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    common, "append_ledger", side_effect=OSError("disk full")
                ),
                contextlib.redirect_stderr(stderr),
            ):
                response = schema_guard.process(
                    {
                        "tool_name": "Edit",
                        "tool_input": {"file_path": str(tmp / "state.db")},
                        "cwd": str(cwd),
                    },
                    root=root,
                    environ={"TMPDIR": str(tmp)},
                )
            assert response is not None
            self.assertEqual(
                response["hookSpecificOutput"]["permissionDecision"], "deny"
            )
            self.assertIn("blocked record NOT confirmed", stderr.getvalue())

    def test_fires_under_gettempdir_without_any_temp_variable(self) -> None:
        # Every other test injects TMPDIR. On Windows (and on macOS when the
        # variable does not reach the hook environment) nothing does, so the
        # platform's own notion of the temp directory must be enough.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd, tmp = base / "state", base / "project", base / "platform-tmp"
            cwd.mkdir()
            tmp.mkdir()
            common.write_config(
                common.state_paths(root=root, cwd=cwd)["config"],
                {"rules": {"schema-guard": {"tmp_paths": []}}},
            )
            with mock.patch.object(tempfile, "tempdir", str(tmp)):
                response = schema_guard.process(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Write",
                        "tool_input": {"file_path": str(tmp / "ledger.jsonl")},
                        "cwd": str(cwd),
                    },
                    root=root,
                    environ={},
                )
            self.assertIsNone(response)
            record = common.read_ledger(
                common.state_paths(root=root, cwd=cwd)["ledger"]
            )[-1]
            self.assertEqual(record["event"], "would_have_blocked")
            self.assertEqual(record["rule"], "schema-guard")

    def test_windows_temp_variables_count_as_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd, tmp = base / "state", base / "project", base / "win-temp"
            cwd.mkdir()
            tmp.mkdir()
            common.write_config(
                common.state_paths(root=root, cwd=cwd)["config"],
                {"rules": {"schema-guard": {"tmp_paths": []}}},
            )
            with mock.patch.object(tempfile, "tempdir", str(base / "elsewhere")):
                response = schema_guard.process(
                    {
                        "hook_event_name": "PreToolUse",
                        "tool_name": "Write",
                        "tool_input": {"file_path": str(tmp / "config.json")},
                        "cwd": str(cwd),
                    },
                    root=root,
                    environ={"TEMP": str(tmp)},
                )
            self.assertIsNone(response)
            record = common.read_ledger(
                common.state_paths(root=root, cwd=cwd)["ledger"]
            )[-1]
            self.assertEqual(record["event"], "would_have_blocked")


class EscapeHatchTests(unittest.TestCase):
    RULES = ("write-verify", "schema-guard", "stuck-search", "preflight")

    def test_escape_hatch_strings_parse_through_the_real_cli_parser(self) -> None:
        # The hatch quoted in every block reason must be a command the CLI
        # accepts. The previous wording had the arguments reversed and the
        # tests had locked the reversed order in.
        parser = axiom_cli.build_parser()
        for rule in self.RULES:
            hatch = common.escape_hatch(rule)
            self.assertTrue(hatch.startswith("/axiom:enforce "), hatch)
            args = parser.parse_args(hatch.removeprefix("/axiom:").split())
            self.assertEqual(
                (args.command, args.rule, args.mode), ("enforce", rule, "off")
            )

    def test_write_verify_reason_carries_hatch_only_for_the_claude_host(self) -> None:
        failed = [
            {
                "type": "file_exists",
                "path": "x",
                "expected": "exists",
                "actual": "missing",
            }
        ]
        with_hatch = write_verify.failure_reason(failed)
        self.assertIn(common.escape_hatch("write-verify"), with_hatch)
        without = write_verify.failure_reason(failed, escape_hatch=False)
        self.assertNotIn("/axiom:", without)
        self.assertIn("file_exists x: expected exists, actual missing", without)


class RepositoryHygieneTests(unittest.TestCase):
    def test_repo_root_has_no_live_goal_file(self) -> None:
        # SessionStart registers the first `*.goal.md` in the project
        # directory and the Stop hook then runs its predicates -- in observe
        # mode too. A live goal file at the repository root is code execution
        # for everyone who opens the repo; examples live under docs/examples/
        # with a name that does not match the glob.
        self.assertEqual(sorted(REPO_ROOT.glob("*.goal.md")), [])


class CliConfigTests(unittest.TestCase):
    def test_modes_and_enforce_use_typed_loader_and_record_degraded_config(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config_path = base / "config.json"
            ledger = base / "ledger.jsonl"
            paths = {"config": config_path, "ledger": ledger}
            config_path.write_text("broken", encoding="utf-8")

            with (
                mock.patch.object(axiom_cli, "_import_common", return_value=common),
                mock.patch.object(axiom_cli, "_config_path", return_value=config_path),
                mock.patch.object(common, "state_paths", return_value=paths),
                mock.patch.object(common, "ensure_layout", return_value=paths),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(axiom_cli.cmd_modes(mock.Mock()), 0)
                self.assertEqual(
                    axiom_cli.cmd_enforce(mock.Mock(rule="schema-guard", on=True)),
                    0,
                )

            records = common.read_ledger(ledger)
            self.assertEqual(
                [(record["event"], record["hook"]) for record in records],
                [
                    ("config_degraded", "cli_modes"),
                    ("config_degraded", "cli_enforce"),
                    # The operator's own decision lands in the same ledger as
                    # the machine's findings — auditing only one half would
                    # audit the wrong half.
                    ("mode_changed", "cli_enforce"),
                ],
            )
            decision = records[-1]
            self.assertEqual(decision["rule"], "schema-guard")
            self.assertEqual(decision["from"], "observe")
            self.assertEqual(decision["to"], "enforce")
            # No `--by` was given: the ledger must not credit a human for a
            # command any agent can run.
            self.assertEqual(decision["decided_by"], "unattested")
            repaired = common.load_config(config_path)
            self.assertEqual(repaired.status, "valid")
            self.assertEqual(repaired.data["rules"]["schema-guard"]["mode"], "enforce")

    def test_enforce_records_the_asserted_decider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            config_path = base / "config.json"
            ledger = base / "ledger.jsonl"
            paths = {"config": config_path, "ledger": ledger}
            with (
                mock.patch.object(axiom_cli, "_import_common", return_value=common),
                mock.patch.object(axiom_cli, "_config_path", return_value=config_path),
                mock.patch.object(common, "state_paths", return_value=paths),
                mock.patch.object(common, "ensure_layout", return_value=paths),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    axiom_cli.main(
                        [
                            "enforce",
                            "write-verify",
                            "off",
                            "--by",
                            "human",
                            "--cwd",
                            str(base),
                        ]
                    ),
                    0,
                )
            decision = common.read_ledger(ledger)[-1]
            self.assertEqual(decision["event"], "mode_changed")
            self.assertEqual(decision["to"], "observe")
            self.assertEqual(decision["decided_by"], "human")


class StuckSearchTests(unittest.TestCase):
    def test_interrupts_leave_clusters_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "data", base / "project"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            common.write_config(
                paths["config"], {"rules": {"stuck-search": {"mode": "enforce"}}}
            )
            payload = self._payload(
                cwd, "PostToolUseFailure", "npm test unit", "failed"
            )
            interrupted = {**payload, "is_interrupt": True}
            for _ in range(3):
                self.assertIsNone(stuck_search.process(interrupted, root=root))
            self.assertFalse(paths["stuck_search"].exists())
            for _ in range(2):
                self.assertIsNone(stuck_search.process(payload, root=root))
            before = paths["stuck_search"].read_bytes()
            self.assertIsNone(stuck_search.process(interrupted, root=root))
            self.assertEqual(paths["stuck_search"].read_bytes(), before)
            response = stuck_search.process(payload, root=root)
            self.assertIn("additionalContext", response["hookSpecificOutput"])
            self.assertEqual(
                common.read_json(paths["stuck_search"])["clusters"][0]["count"], 3
            )

    def test_polling_defaults_and_sleep_prefix_opt_in(self) -> None:
        cases = [
            ("until ready; do sleep 1; done", None, False),
            ("while true; do ready; sleep 1; done", None, False),
            ("tail -f output.log", None, False),
            ("watch status", None, False),
            ("sleep 1 && broken_command", None, True),
            ("echo until ready do", None, True),
            ("sleep 1 && broken_command", [r"^sleep\s+\d+\s*&&"], False),
            ("until ready; do sleep 1; done", [], True),
        ]
        for command, patterns, counted in cases:
            with (
                self.subTest(command=command, patterns=patterns),
                tempfile.TemporaryDirectory() as temporary,
            ):
                base = Path(temporary)
                root, cwd = base / "data", base / "project"
                cwd.mkdir()
                paths = common.ensure_layout(root=root, cwd=cwd)
                settings: dict[str, object] = {"mode": "enforce"}
                if patterns is not None:
                    settings["polling_patterns"] = patterns
                common.write_config(
                    paths["config"], {"rules": {"stuck-search": settings}}
                )
                responses = [
                    stuck_search.process(
                        self._payload(cwd, "PostToolUseFailure", command, "failed"),
                        root=root,
                    )
                    for _ in range(4)
                ]
                if counted:
                    self.assertIn(
                        "additionalContext", responses[2]["hookSpecificOutput"]
                    )
                    self.assertEqual(
                        common.read_json(paths["stuck_search"])["clusters"][0]["count"],
                        4,
                    )
                else:
                    self.assertEqual(responses, [None] * 4)
                    self.assertFalse(paths["stuck_search"].exists())

    def test_advice_ledger_and_report_in_both_modes(self) -> None:
        for mode in ("enforce", "observe"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root, cwd = base / "data", base / "project"
                cwd.mkdir()
                paths = common.ensure_layout(root=root, cwd=cwd)
                common.write_config(
                    paths["config"], {"rules": {"stuck-search": {"mode": mode}}}
                )
                for _ in range(3):
                    stuck_search.process(
                        self._payload(
                            cwd, "PostToolUseFailure", "npm test unit", "failed"
                        ),
                        root=root,
                    )
                records = common.read_ledger(paths["ledger"])
                event = "advice_injected" if mode == "enforce" else "would_have_blocked"
                self.assertEqual([record["event"] for record in records], [event])
                self.assertEqual(records[0]["rule"], "stuck-search")
                self.assertEqual(records[0]["hook"], "stuck_search")
                self.assertEqual(len(records[0]["finding_id"]), 16)
                report = common.get_report_data(paths["ledger"])
                self.assertEqual(report["rules"]["stuck-search"][event], 1)
                if mode == "enforce":
                    self.assertEqual(records[0]["count"], 3)
                    self.assertEqual(report["rules"]["stuck-search"]["recent"], [])
                    self.assertEqual(common.calibration_notice(report), "")
                    output = io.StringIO()
                    with redirect_stdout(output):
                        axiom_cli._render_report(report)
                    self.assertIn("advice_injected: 1", output.getvalue())

    def test_cooldown_suppresses_injection_but_not_observe_records(self) -> None:
        for mode in ("enforce", "observe"):
            for cooldown in (10, 0):
                with (
                    self.subTest(mode=mode, cooldown=cooldown),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    base = Path(temporary)
                    root, cwd = base / "data", base / "project"
                    cwd.mkdir()
                    paths = common.ensure_layout(root=root, cwd=cwd)
                    common.write_config(
                        paths["config"],
                        {
                            "rules": {
                                "stuck-search": {
                                    "mode": mode,
                                    "cooldown_minutes": cooldown,
                                }
                            }
                        },
                    )
                    payload = self._payload(
                        cwd, "PostToolUseFailure", "npm test unit", "failed"
                    )
                    now = datetime.now(timezone.utc)
                    with mock.patch.object(stuck_search, "datetime") as clock:
                        clock.now.return_value = now
                        responses = [
                            stuck_search.process(payload, root=root) for _ in range(5)
                        ]
                        self.assertEqual(
                            sum(item is not None for item in responses),
                            (1 if cooldown else 3) if mode == "enforce" else 0,
                        )
                        clock.now.return_value = now + timedelta(minutes=10)
                        response = stuck_search.process(payload, root=root)
                    self.assertEqual(response is not None, mode == "enforce")
                    self.assertEqual(
                        len(common.read_ledger(paths["ledger"])),
                        2 if mode == "enforce" and cooldown else 4,
                    )

    def test_advice_is_injected_even_when_its_record_cannot_be_written(self) -> None:
        # CONTRACTS §6: the enforce-mode record is best-effort. The cooldown
        # is spent the moment the advice is decided, so a failed append that
        # raised into fail-open would cost the agent the advice AND suppress
        # it for the whole cooldown window -- the worst of both.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "data", base / "project"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            common.write_config(
                paths["config"], {"rules": {"stuck-search": {"mode": "enforce"}}}
            )
            payload = self._payload(
                cwd, "PostToolUseFailure", "npm test unit", "failed"
            )
            stderr = io.StringIO()
            with (
                mock.patch.object(
                    common, "append_ledger", side_effect=OSError("disk full")
                ),
                contextlib.redirect_stderr(stderr),
            ):
                responses = [stuck_search.process(payload, root=root) for _ in range(3)]
            self.assertIsNotNone(responses[-1])
            self.assertIn(
                "AXIOM stuck-search",
                responses[-1]["hookSpecificOutput"]["additionalContext"],
            )
            self.assertIn("advice_injected record NOT confirmed", stderr.getvalue())
            state = common.read_json(paths["stuck_search"])
            self.assertIn("last_advice_at", state["clusters"][0])
            self.assertEqual(common.read_ledger(paths["ledger"]), [])

    def test_search_after_advice_records_once_with_lag(self) -> None:
        for tool in (
            "WebSearch",
            "WebFetch",
            "mcp__provider__search",
            "mcp__provider__fetch",
        ):
            with self.subTest(tool=tool), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                root, cwd = base / "data", base / "project"
                cwd.mkdir()
                paths = common.ensure_layout(root=root, cwd=cwd)
                common.write_config(
                    paths["config"], {"rules": {"stuck-search": {"mode": "enforce"}}}
                )
                search = {
                    "hook_event_name": "PostToolUse",
                    "tool_name": tool,
                    "cwd": str(cwd),
                    "session_id": "a",
                }
                stuck_search.process_search_event(search, root=root)
                self.assertEqual(common.read_ledger(paths["ledger"]), [])
                now = datetime.now(timezone.utc)
                with mock.patch.object(stuck_search, "datetime") as clock:
                    clock.now.return_value = now
                    failure = {
                        **self._payload(
                            cwd, "PostToolUseFailure", "npm test unit", "failed"
                        ),
                        "session_id": "a",
                    }
                    for _ in range(3):
                        stuck_search.process(failure, root=root)
                    # A Bash success decays the cluster, never the pending measurement.
                    stuck_search.process(
                        self._payload(cwd, "PostToolUse", "npm test unit"), root=root
                    )
                    clock.now.return_value = now + timedelta(seconds=90)
                    stuck_search.process_search_event(
                        {**search, "session_id": "b"}, root=root
                    )
                    stuck_search.process_search_event(
                        {**search, "tool_name": "mcp__provider__write"}, root=root
                    )
                    stuck_search.process_search_event(
                        {**search, "hook_event_name": "PostToolUseFailure"}, root=root
                    )
                    stuck_search.process_search_event(search, root=root)
                    stuck_search.process_search_event(search, root=root)
                records = common.read_ledger(paths["ledger"])
                self.assertEqual(
                    [item["event"] for item in records],
                    ["advice_injected", "search_after_trigger"],
                )
                self.assertEqual(records[-1]["lag_seconds"], 90)
                self.assertEqual(records[-1]["rule"], "stuck-search")
                # The search is joined to the advice it followed (CONTRACTS §6).
                self.assertEqual(records[-1]["finding_id"], records[0]["finding_id"])
                output = io.StringIO()
                with redirect_stdout(output):
                    axiom_cli._render_report(common.get_report_data(paths["ledger"]))
                self.assertIn("search_after_trigger: 1", output.getvalue())

    def test_search_window_and_configurable_tool_names(self) -> None:
        for mode, minutes, patterns, expected in (
            ("enforce", 30, ["Lookup"], 1),
            ("enforce", 31, ["Lookup"], 0),
            ("enforce", -1, ["Lookup"], 0),
            ("enforce", 1, [], 0),
            ("observe", 1, ["Lookup"], 0),
        ):
            with (
                self.subTest(mode=mode, minutes=minutes, patterns=patterns),
                tempfile.TemporaryDirectory() as temporary,
            ):
                base = Path(temporary)
                root, cwd = base / "data", base / "project"
                cwd.mkdir()
                paths = common.ensure_layout(root=root, cwd=cwd)
                common.write_config(
                    paths["config"],
                    {
                        "rules": {
                            "stuck-search": {"mode": mode, "search_tools": patterns}
                        }
                    },
                )
                now = datetime.now(timezone.utc)
                with mock.patch.object(stuck_search, "datetime") as clock:
                    clock.now.return_value = now
                    for _ in range(3):
                        stuck_search.process(
                            self._payload(
                                cwd, "PostToolUseFailure", "npm test unit", "failed"
                            ),
                            root=root,
                        )
                    clock.now.return_value = now + timedelta(minutes=minutes)
                    stuck_search.process_search_event(
                        {
                            "hook_event_name": "PostToolUse",
                            "tool_name": "Lookup",
                            "cwd": str(cwd),
                        },
                        root=root,
                    )
                records = common.read_ledger(paths["ledger"])
                self.assertEqual(
                    sum(item["event"] == "search_after_trigger" for item in records),
                    expected,
                )

    def _payload(
        self, cwd: Path, event: str, command: str, error: str = ""
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "hook_event_name": event,
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(cwd),
        }
        if error:
            payload["error"] = error
        else:
            payload["tool_response"] = {"stdout": "ok", "stderr": ""}
        return payload

    def test_two_successes_and_one_failure_do_not_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            for event in ("PostToolUse", "PostToolUse", "PostToolUseFailure"):
                response = stuck_search.process(
                    self._payload(
                        cwd,
                        event,
                        "npm test unit",
                        "failed" if "Failure" in event else "",
                    ),
                    root=root,
                )
                self.assertIsNone(response)
            records = common.read_ledger(
                common.state_paths(root=root, cwd=cwd)["ledger"]
            )
            self.assertFalse(
                any(item.get("event") == "would_have_blocked" for item in records)
            )

    def test_different_commands_with_same_root_tokens_cluster(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            common.write_config(
                common.state_paths(root=root, cwd=cwd)["config"],
                {"rules": {"stuck-search": {"mode": "enforce"}}},
            )
            commands = ["npm test alpha", "npm test beta", "npm test gamma"]
            responses = [
                stuck_search.process(
                    self._payload(
                        cwd, "PostToolUseFailure", command, "same root cause"
                    ),
                    root=root,
                )
                for command in commands
            ]
            self.assertIsNone(responses[0])
            self.assertIsNone(responses[1])
            self.assertIn("additionalContext", responses[2]["hookSpecificOutput"])
            state = common.read_json(
                common.state_paths(root=root, cwd=cwd)["stuck_search"]
            )
            self.assertEqual(state["clusters"][0]["count"], 3)

    def test_success_decays_instead_of_clearing(self) -> None:
        # One passing neighbour is evidence, not proof: it takes one failure
        # off the cluster, and the cluster goes only when its count reaches 0.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            state_path = common.state_paths(root=root, cwd=cwd)["stuck_search"]
            for command in ("npm test alpha", "npm test beta"):
                stuck_search.process(
                    self._payload(cwd, "PostToolUseFailure", command, "failure"),
                    root=root,
                )
            stuck_search.process(
                self._payload(cwd, "PostToolUse", "npm test gamma"), root=root
            )
            self.assertEqual(common.read_json(state_path)["clusters"][0]["count"], 1)
            stuck_search.process(
                self._payload(cwd, "PostToolUse", "npm test gamma"), root=root
            )
            self.assertEqual(common.read_json(state_path)["clusters"], [])

    def test_varied_short_commands_stay_in_one_cluster(self) -> None:
        # Matching against the union of every absorbed command drifted: by the
        # fourth `npm test <x>` the union held five tokens and the new command
        # scored 2/6 < 0.4, opening a second cluster. Seed and latest do not.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            common.write_config(
                paths["config"],
                {"rules": {"stuck-search": {"failure_threshold": 5}}},
            )
            for name in ("alpha", "beta", "gamma", "delta", "epsilon"):
                stuck_search.process(
                    self._payload(cwd, "PostToolUseFailure", f"npm test {name}", "x"),
                    root=root,
                )
            clusters = common.read_json(paths["stuck_search"])["clusters"]
            self.assertEqual([item["count"] for item in clusters], [5])
            self.assertEqual(
                [r["event"] for r in common.read_ledger(paths["ledger"])],
                ["would_have_blocked"],
            )

    def test_exit0_masked_failure_counts_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            common.write_config(
                paths["config"], {"rules": {"stuck-search": {"mode": "enforce"}}}
            )
            payload = {
                "hook_event_name": "PostToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "python -m pytest tests | tail -5"},
                "tool_response": {
                    "stdout": "FAILED tests/test_a.py::test_x - AssertionError\n"
                    "==== 1 failed, 4 passed in 0.12s ====\n",
                    "stderr": "",
                },
                "cwd": str(cwd),
            }
            responses = [stuck_search.process(payload, root=root) for _ in range(3)]
            self.assertEqual(responses[:2], [None, None])
            output = responses[2]["hookSpecificOutput"]
            self.assertEqual(output["hookEventName"], "PostToolUse")
            self.assertIn("exited 0", output["additionalContext"])
            record = common.read_ledger(paths["ledger"])[-1]
            self.assertEqual(record["event"], "advice_injected")
            self.assertIs(record["masked_failure"], True)
            self.assertEqual(record["distinct_errors"], 1)

    def test_masked_failure_signatures_and_readers(self) -> None:
        for command, stdout, counted in (
            ("make test", "Traceback (most recent call last):\n  boom\n", True),
            ("cargo test 2>&1 | tail", "test result: FAILED. 1 passed\n", True),
            ("go test ./... ; true", "--- FAIL: TestX (0.00s)\n", True),
            ("./run.sh", "bash: frobnicate: command not found\n", True),
            ("npm test | cat", "npm ERR! Test failed.\n", True),
            ("pytest -q | tail -3", "5 passed, 1 xfailed in 0.20s\n", False),
            ("pytest -q | tail -3", "0 failed\n", False),
            ("cat test.log", "FAILED tests/test_a.py::test_x\n", False),
            ("grep -r FAILED reports", "FAILED tests/test_a.py::test_x\n", False),
            ("git log -1", "    FAILED to build\nFAILED\n", False),
            (
                "cd build && tail -n 50 out.log",
                "Traceback (most recent call last):\n",
                False,
            ),
            ('pytest "unbalanced', "FAILED x\n", False),
        ):
            with self.subTest(command=command, stdout=stdout):
                self.assertEqual(
                    stuck_search.masked_failure(command, stdout, "") is not None,
                    counted,
                )

    def test_exit0_is_success_evidence_only_for_plain_or_and_chains(self) -> None:
        for command, evidence in (
            ("pytest tests", True),
            ("cd app && pytest tests", True),
            ("pytest tests | tail -5", False),
            ("pytest tests || true", False),
            ("pytest tests; echo done", False),
            ("pytest tests\necho done", False),
            ("pytest tests &", False),
            ('pytest "unbalanced', False),
        ):
            with self.subTest(command=command):
                self.assertEqual(stuck_search.success_is_evidence(command), evidence)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            state_path = common.state_paths(root=root, cwd=cwd)["stuck_search"]
            for _ in range(2):
                stuck_search.process(
                    self._payload(cwd, "PostToolUseFailure", "pytest tests", "x"),
                    root=root,
                )
            for masked in ("pytest tests | tail -5", "pytest tests || true"):
                stuck_search.process(
                    self._payload(cwd, "PostToolUse", masked), root=root
                )
            self.assertEqual(common.read_json(state_path)["clusters"][0]["count"], 2)

    def test_state_file_never_stores_raw_command_or_error_text(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            command = "curl -H 'Authorization: Bearer sk-live-SECRET123' api.example"
            for _ in range(3):
                stuck_search.process(
                    self._payload(
                        cwd,
                        "PostToolUseFailure",
                        command,
                        "Unauthorized: token sk-live-SECRET123",
                    ),
                    root=root,
                )
            raw = paths["stuck_search"].read_text(encoding="utf-8")
            # Only fragments that cannot occur in hex digests or timestamps.
            for fragment in ("SECRET123", "Bearer", "curl", "api.example", "Unauth"):
                self.assertNotIn(fragment, raw)
            self.assertEqual(
                common.read_json(paths["stuck_search"])["clusters"][0]["count"], 3
            )

    def test_legacy_clusters_with_raw_text_are_purged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            now = datetime.now(timezone.utc).isoformat()
            common.write_json(
                paths["stuck_search"],
                {
                    "clusters": [
                        {
                            "tokens": ["curl", "-h", "authorization: bearer s3cr3t"],
                            "count": 2,
                            "first_at": now,
                            "updated_at": now,
                            "last_command": "curl -H 'Authorization: Bearer s3cr3t'",
                            "last_error": "401 for s3cr3t",
                        }
                    ]
                },
            )
            stuck_search.process(self._payload(cwd, "PostToolUse", "ls"), root=root)
            self.assertNotIn(
                "s3cr3t", paths["stuck_search"].read_text(encoding="utf-8")
            )
            self.assertEqual(common.read_json(paths["stuck_search"])["clusters"], [])

    def test_error_signatures_ignore_volatile_detail_and_count_distinct(self) -> None:
        first = 'Exit code 1\nTraceback (most recent call last):\n  File "a.py", line 12\nKeyError: 3'
        second = 'Exit code 2\nTraceback (most recent call last):\n  File "a.py", line 99\nKeyError: 7'
        self.assertEqual(
            stuck_search.error_signature(first), stuck_search.error_signature(second)
        )
        self.assertIsNone(stuck_search.error_signature("Exit code 1\n\n"))
        self.assertNotEqual(
            stuck_search.error_signature("ValueError: bad"),
            stuck_search.error_signature("KeyError: 3"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            for error in (first, second, "Exit code 1\nValueError: bad"):
                stuck_search.process(
                    self._payload(cwd, "PostToolUseFailure", "python a.py", error),
                    root=root,
                )
            record = common.read_ledger(paths["ledger"])[-1]
            self.assertEqual(record["event"], "would_have_blocked")
            self.assertIs(record["masked_failure"], False)
            self.assertEqual(record["failed"][0]["distinct_errors"], 2)


class StuckSearchReviewTests(unittest.TestCase):
    def _process(
        self,
        cwd: Path,
        root: Path,
        event: str,
        command: str,
        *,
        error: str = "",
        run_in_background: bool = False,
        stdout: str = "ok",
    ) -> dict[str, Any] | None:
        tool_input: dict[str, object] = {"command": command}
        if run_in_background:
            tool_input["run_in_background"] = True
        payload: dict[str, object] = {
            "hook_event_name": event,
            "tool_name": "Bash",
            "tool_input": tool_input,
            "cwd": str(cwd),
            "tool_response": {"stdout": stdout, "stderr": ""},
        }
        if error:
            payload["error"] = error
        return stuck_search.process(payload, root=root)

    def test_reader_commands_and_shapes_are_not_masked_failures(self) -> None:
        failing = "FAILED tests/test_a.py::test_x\n==== 1 failed, 4 passed ====\n"
        for command in (
            'for f in logs/*.log; do tail -3 "$f"; done',
            "if grep -q FAILED log; then cat log; fi",
            "[[ -f log ]] && cat log",
            "gh run view 123 --log-failed",
            "gh pr view 7 --comments",
            'git add -A && git commit -m "Fix 2 failing tests"',
            "kubectl -n prod logs api-0",
            "docker logs web | tail -50",
            "tail -n 20 \\\n  build/test-results.log",
        ):
            with self.subTest(command=command):
                self.assertIsNone(stuck_search.masked_failure(command, failing, ""))
        for command in (
            "sed -i s#localhost#db#g settings.py && python -m pytest tests | tail -5",
            "curl -s http://x/#/health && pytest -q",
            "docker exec web pytest -q",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(stuck_search.masked_failure(command, failing, ""))
        for output in (
            "zsh: command not found: frobnicate\n",
            "sh: 1: frobnicate: not found\n",
            "bash: frobnicate: command not found\r\n",
        ):
            with self.subTest(output=output):
                self.assertIsNotNone(
                    stuck_search.masked_failure("./run.sh", output, "")
                )

    def test_success_evidence_survives_formatting(self) -> None:
        for command, evidence in (
            ("pytest tests\n", True),
            ("python -m pytest \\\n  tests/unit -q", True),
            ("pytest tests  # run them", True),
            ("pytest tests &\n", False),
            ("for t in a b; do pytest $t; done", False),
        ):
            with self.subTest(command=command):
                self.assertEqual(stuck_search.success_is_evidence(command), evidence)

    def test_background_launch_leaves_clusters_alone(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            state_path = common.state_paths(root=root, cwd=cwd)["stuck_search"]
            for _ in range(2):
                self._process(
                    cwd, root, "PostToolUseFailure", "pytest tests", error="failed"
                )
            for _ in range(2):
                self._process(
                    cwd,
                    root,
                    "PostToolUse",
                    "pytest tests",
                    run_in_background=True,
                    stdout="Command running in background with ID: bash_1",
                )
            self.assertEqual(common.read_json(state_path)["clusters"][0]["count"], 2)

    def test_corrupt_cluster_state_never_raises(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        digests = sorted(stuck_search.token_digests("pytest tests"))
        for corrupt in (
            {"seed": [["a"]], "tokens": digests},
            {"seed": digests, "tokens": [{}]},
            {"seed": digests, "tokens": digests, "errors": None},
            {"seed": digests, "tokens": digests, "errors": 5},
            {"seed": digests, "tokens": digests, "errors": "abc", "count": "x"},
        ):
            with (
                self.subTest(corrupt=corrupt),
                tempfile.TemporaryDirectory() as temporary,
            ):
                base = Path(temporary)
                root, cwd = base / "state", base / "project"
                cwd.mkdir()
                paths = common.ensure_layout(root=root, cwd=cwd)
                cluster = {"count": 1, "first_at": now, "updated_at": now, **corrupt}
                common.write_json(paths["stuck_search"], {"clusters": [cluster]})
                self._process(cwd, root, "PostToolUse", "pytest tests")
                self._process(
                    cwd, root, "PostToolUseFailure", "pytest tests", error="boom"
                )
                clusters = common.read_json(paths["stuck_search"])["clusters"]
                self.assertTrue(clusters)
                for item in clusters:
                    self.assertTrue(all(isinstance(x, str) for x in item["seed"]))
                    self.assertTrue(all(isinstance(x, str) for x in item["errors"]))
                    self.assertLessEqual(len(item["errors"]), 1)

    def test_search_event_write_purges_legacy_clusters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            now = datetime.now(timezone.utc).isoformat()
            legacy = {
                "tokens": ["s3cr3t"],
                "count": 2,
                "updated_at": now,
                "last_command": "curl -u admin:s3cr3t x",
            }
            pending = {"session_id": "a", "advice_at": now, "finding_id": "f" * 16}
            common.write_json(
                paths["stuck_search"],
                {"clusters": [legacy], "pending_searches": [pending]},
            )
            stuck_search.process_search_event(
                {
                    "hook_event_name": "PostToolUse",
                    "tool_name": "WebSearch",
                    "cwd": str(cwd),
                    "session_id": "a",
                },
                root=root,
            )
            raw = paths["stuck_search"].read_text(encoding="utf-8")
            self.assertNotIn("s3cr3t", raw)
            self.assertEqual(
                [r["event"] for r in common.read_ledger(paths["ledger"])],
                ["search_after_trigger"],
            )

    def test_second_review_reader_and_evidence_cases(self) -> None:
        failing = "==== 1 failed, 2 passed ====\n"
        for command, counted in (
            ('out=$(pytest 2>&1); echo "$out"', True),
            ("x=$(cat log); echo $x", False),
            ("docker compose logs api", False),
            ("docker container logs api", False),
            ("docker compose run api pytest", True),
            (
                "npm test && git commit -m \"$(cat <<'EOF'\nFix the user's login\nEOF\n)\"",
                True,
            ),
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    stuck_search.masked_failure(command, failing, "") is not None,
                    counted,
                )
        self.assertTrue(
            stuck_search.success_is_evidence("npm test &&\n  npm run build")
        )

    def test_out_of_range_timestamps_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            digests = sorted(stuck_search.token_digests("pytest tests"))
            common.write_json(
                paths["stuck_search"],
                {
                    "clusters": [
                        {
                            "seed": digests,
                            "tokens": digests,
                            "count": 2,
                            "updated_at": "0001-01-01T00:00:00+01:00",
                        }
                    ]
                },
            )
            self._process(cwd, root, "PostToolUseFailure", "pytest tests", error="x")
            clusters = common.read_json(paths["stuck_search"])["clusters"]
            self.assertEqual([item["count"] for item in clusters], [1])

    def test_third_review_reader_cases(self) -> None:
        for command, counted in (
            ('cat "$(readlink -f build/latest.log)"', False),
            ("docker compose -f a.yml -p x logs api", False),
            ("docker-compose logs api", False),
            ("kubectl logs -f pod", False),
            ("docker compose -f a.yml run api pytest", True),
        ):
            with self.subTest(command=command):
                self.assertEqual(
                    stuck_search.masked_failure(command, "1 failed\n", "") is not None,
                    counted,
                )
        self.assertFalse(
            stuck_search.success_is_evidence("npm test && ((n++))\necho done")
        )


class ShellShapeTests(unittest.TestCase):
    def test_lexer_handles_shell_syntax_shlex_got_wrong(self) -> None:
        cases: dict[str, tuple[list[list[str]], list[str]]] = {
            # '#' starts a comment only at the start of a word.
            "sed s#a#b#g f && pytest": ([["sed", "s#a#b#g", "f"], ["pytest"]], ["&&"]),
            "echo hi # note; rm -rf x": ([["echo", "hi"]], []),
            # Backslash-newline joins lines; a trailing newline ends the list.
            "tail -n 20 \\\n  out.log": ([["tail", "-n", "20", "out.log"]], []),
            "pytest tests\n": ([["pytest", "tests"]], []),
            "pytest &\n": ([["pytest"]], ["&"]),
            # Quoted or escaped punctuation is a word, not an operator.
            'echo "a;b|c" x\\;y': ([["echo", "a;b|c", "x;y"]], []),
            # `2>` is a descriptor; a standalone number is an argument.
            "sleep 5 > log; cmd 2>&1 | tee x": (
                [["sleep", "5"], ["cmd"], ["tee", "x"]],
                [";", "|"],
            ),
            # `<<` inside quotes or arithmetic is not a here-document.
            'echo "a <<EOF b"\nrm x': ([["echo", "a <<EOF b"], ["rm", "x"]], [";"]),
            "x=$((1<<2)); echo ok": ([["x=$((1<<2))"], ["echo", "ok"]], [";"]),
            "y=${HOME:-a b}": ([["y=${HOME:-a b}"]], []),
            "for f in a; do tail $f; done": (
                [["for", "f", "in", "a"], ["do", "tail", "$f"], ["done"]],
                [";", ";"],
            ),
        }
        for command, (commands, operators) in cases.items():
            with self.subTest(command=command):
                shape = common.shell_shape(command)
                assert shape is not None
                self.assertEqual(
                    (shape.commands, shape.operators), (commands, operators)
                )

    def test_heredocs_and_substitutions(self) -> None:
        shape = common.shell_shape(
            "cat <<'EOF' | sh\nrm $(a)\nEOF\ncat <<-END-X\n\t$(b) `c`\n\tEND-X\n"
            'echo "$(d)" \'$(e)\' > "$(f)" <(g) ${h:-$(i)} <<< "$(j)"'
        )
        assert shape is not None
        self.assertEqual(shape.heredocs, ["rm $(a)", "\t$(b) `c`"])
        self.assertEqual(shape.substitutions, ["b", "c", "d", "f", "g", "i", "j"])
        self.assertEqual(
            [common.command_name(argv)[0] for argv in shape.commands],
            ["cat", "sh", "cat", "echo"],
        )
        for unbalanced in ('echo "x', "echo 'x", "echo $(x", "echo `x"):
            with self.subTest(unbalanced=unbalanced):
                self.assertIsNone(common.shell_shape(unbalanced))

    def test_command_name_skips_reserved_words_and_wrappers(self) -> None:
        cases = {
            ("then", "rm", "-rf", "x"): "rm",
            ("do", "git", "push"): "git",
            ("!", "{", "make"): "make",
            ("for", "f", "in", "a", "b"): "",
            ("done",): "",
            ("timeout", "-s", "KILL", "60", "git", "push"): "git",
            ("flock", "-w", "5", "/tmp/l", "make"): "make",
            ("stdbuf", "-oL", "ionice", "-c3", "nice", "-n", "5", "dd"): "dd",
            ("sudo", "-Eu", "root", "env", "A=1", "/bin/rm"): "rm",
            ("flock", "/tmp/l", "-c", "rm -rf /srv"): "rm -rf /srv",
            ("FOO=1", "a[2]=x", "B+=y", "make"): "make",
        }
        for argv, name in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(common.command_name(argv)[0], name)

    def test_substitutions_lex_like_the_top_level(self) -> None:
        # Claude Code's commit idiom: a quoted here-document inside $(...),
        # with apostrophes and parentheses in the message.
        commit = (
            "git commit -m \"$(cat <<'EOF'\nFix the parser (again)\n\n"
            "It's the last one.\n1) done\nEOF\n)\"\nrm -rf /srv/app"
        )
        shape = common.shell_shape(commit)
        assert shape is not None
        self.assertEqual(
            [common.command_name(argv)[0] for argv in shape.commands], ["git", "rm"]
        )
        self.assertEqual(len(shape.substitutions), 1)
        comment = "x=$(\n  # read the user's config\n  cat config.txt\n)\nrm -rf x"
        shape = common.shell_shape(comment)
        assert shape is not None
        self.assertEqual(
            shape.commands,
            [["x=" + comment[2 : comment.index(")") + 1]], ["rm", "-rf", "x"]],
        )

    def test_arithmetic_subshells_and_parameter_braces(self) -> None:
        cases: dict[str, tuple[list[list[str]], list[str]]] = {
            "((rm -rf x) )": ([["rm", "-rf", "x"]], []),
            "x=$(( 1 + (2*3) )); (( x++ ))": ([["x=$(( 1 + (2*3) ))"]], []),
            "echo ${x:-{a}}; rm y": ([["echo", "${x:-{a}}"], ["rm", "y"]], []),
        }
        for command, (commands, substitutions) in cases.items():
            with self.subTest(command=command):
                shape = common.shell_shape(command)
                assert shape is not None
                self.assertEqual(shape.commands, commands)
                self.assertEqual(shape.substitutions, substitutions)
        shape = common.shell_shape("n=$(( $(git clean -fdx | wc -l) + `date` ))")
        assert shape is not None
        self.assertEqual(shape.substitutions, ["git clean -fdx | wc -l", "date"])

    def test_followers_owners_and_continued_lists(self) -> None:
        shape = common.shell_shape(
            "cat <<EOF | sh\nbody\nEOF\nnpm test &&\n  npm run build\ncat <<A > f; sh x\nA\n"
        )
        assert shape is not None
        self.assertEqual(
            [argv[0] for argv in shape.commands],
            ["cat", "sh", "npm", "npm", "cat", "sh"],
        )
        self.assertEqual(shape.followers, ["|", ";", "&&", ";", ";", None])
        self.assertEqual(shape.heredoc_owners, [0, 4])
        shape = common.shell_shape("npm test &&\n  npm run build")
        assert shape is not None
        self.assertEqual(shape.operators, ["&&"])

    def test_oversized_and_pathological_input_is_shape_unknown(self) -> None:
        self.assertIsNone(common.shell_shape("echo " + "a" * 200_001))
        # Nested past the recursion limit: the RecursionError guard.
        self.assertIsNone(common.shell_shape("echo " + "$(" * 3000 + ")" * 3000))
        started = time.monotonic()
        # Unclosed, 20000 deep: shape unknown (the recursion guard), quickly.
        self.assertIsNone(
            common.shell_shape("cat > f <<EOF\n" + "$(" * 20000 + "\nEOF\n")
        )
        self.assertIsNotNone(
            common.shell_shape("echo " + "${x:-" * 450 + "a" * 50000 + "}" * 450)
        )
        # Both took tens of seconds when every unclosed `$(` rescanned the rest.
        self.assertLess(time.monotonic() - started, 10)

    def test_bundled_options_with_attached_values(self) -> None:
        cases = {
            ("sudo", "-uroot", "rm", "-rf", "x"): "rm",
            ("sudo", "-Eu", "root", "rm"): "rm",
            ("xargs", "-d\n", "rm", "-rf"): "rm",
            ("timeout", "-k5s", "60", "rm", "-rf", "x"): "rm",
            ("stdbuf", "-oL", "make"): "make",
        }
        for argv, name in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(common.command_name(argv)[0], name)

    def test_pipelines_writes_and_group_owners(self) -> None:
        shape = common.shell_shape(
            "( cat <<'A' ) | bash\nx\nA\ncat > run.sh <<'B'; bash run.sh\ny\nB\n"
            "(cd /srv && bash) <<'C'\nz\nC"
        )
        assert shape is not None
        self.assertEqual(
            [argv[0] for argv in shape.commands],
            ["cat", "bash", "cat", "bash", "cd", "bash"],
        )
        self.assertEqual(shape.pipelines, [0, 0, 1, 2, 3, 3])
        self.assertEqual(shape.writes, [[], [], ["run.sh"], [], [], []])
        self.assertEqual(shape.heredoc_owners, [0, 2, 4])

    def test_third_review_lexer_cases(self) -> None:
        # A newline after an arithmetic command ends the list.
        shape = common.shell_shape("npm test && ((n++))\necho done")
        assert shape is not None
        self.assertEqual(shape.operators, ["&&", ";"])
        # `${…}` does not close at a `}` inside a nested `$(…)`.
        shape = common.shell_shape("n=${COUNT:-$(git rev-list --count @{u}..HEAD)}; ls")
        assert shape is not None
        self.assertEqual(shape.substitutions, ["git rev-list --count @{u}..HEAD"])
        self.assertEqual(len(shape.commands), 2)
        # `EOF)` ends both the here-document and the substitution.
        shape = common.shell_shape("git commit -m \"$(cat <<'EOF'\nit's\nEOF)\"\nls")
        assert shape is not None
        self.assertEqual([argv[0] for argv in shape.commands], ["git", "ls"])
        started = time.monotonic()
        for text in (
            "echo hi; " + "((a) && " * 8000 + "true",
            "(( " * 8000,
            "echo " + "$((a) " * 30000,
        ):
            common.shell_shape(text)
        # Each unclosed or non-arithmetic `((` used to rescan to the end.
        self.assertLess(time.monotonic() - started, 10)


class PreflightTests(unittest.TestCase):
    def test_detects_all_six_irreversible_classes(self) -> None:
        cases = {
            "rm_recursive": "rm -rf /srv/app",
            "git_reset_hard": "git reset --hard HEAD~1",
            "git_clean_force": "git clean -fd",
            "drop_database": "psql -c 'DROP DATABASE app'",
            "disk_overwrite": "dd if=image.bin of=/dev/disk9",
            "force_push": "git push --force origin main",
        }
        for expected, command in cases.items():
            with self.subTest(command=command):
                self.assertEqual(preflight.detect_pattern(command), expected)
        # On Windows, /tmp is not a real temp directory, so use %TEMP% instead
        if sys.platform == "win32":
            import os

            temp_dir = os.environ.get("TEMP", "/tmp").replace("\\", "/")
            self.assertIsNone(
                preflight.detect_pattern(f"rm -rf {temp_dir}/build-cache")
            )
        else:
            self.assertIsNone(preflight.detect_pattern("rm -rf /tmp/build-cache"))

    def test_broadened_shapes_and_force_with_lease(self) -> None:
        environ = {"TMPDIR": "/tmp"}
        cases = {
            "git push --force-with-lease origin main": [],
            "git push --force-if-includes --force-with-lease origin main": [],
            "git push origin +main": ["force_push"],
            "git -C repo push -fu origin topic": ["force_push"],
            "/bin/rm -rf /srv/app": ["rm_recursive"],
            "env FOO=1 rm -rf /srv/app": ["rm_recursive"],
            "command rm -rf /srv/app": ["rm_recursive"],
            "sudo -u root rm -rf /srv/app": ["rm_recursive"],
            "rm /srv/app -rf": ["rm_recursive"],
            "ls\nrm -rf /srv/app": ["rm_recursive"],
            "bash -lc 'rm -rf /srv/app'": ["rm_recursive"],
            'eval "git reset --hard"': ["git_reset_hard"],
            'echo "$(rm -rf /srv/app)"': ["rm_recursive"],
            "echo `rm -rf /srv/app`": ["rm_recursive"],
            'echo "`git reset --hard`"': ["git_reset_hard"],
            "find . -name cache -exec rm -rf {} \\;": ["rm_recursive"],
            "ls | xargs rm -rf": ["rm_recursive"],
            "shred -u secrets.key": ["disk_overwrite"],
            "truncate -s 0 app.db": ["disk_overwrite"],
            "truncate --size=0 app.db": ["disk_overwrite"],
            "truncate -s 10M app.img": [],
            "rm -rf /srv/app && git push -f origin main": [
                "rm_recursive",
                "force_push",
            ],
            # Quoted text is an argument, not a command.
            'echo "git reset --hard"': [],
            'git commit -m "rm -rf /srv/app"': [],
            "rm -- -rf": [],
            "rm -r build": [],
            "git clean -n": [],
            # Text that does not tokenize falls back to the raw-text match.
            'rm -rf /srv/app "unbalanced': ["rm_recursive"],
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(
                    preflight.detect_patterns(command, environ=environ), expected
                )

    def test_review_shapes_compound_wrappers_and_scripts(self) -> None:
        # Every case the old raw-text matcher caught must still be caught, and
        # the shell constructs the review found (compound commands, wrappers,
        # scripts on stdin, comments, continuations) must be read as such.
        cases = {
            "if [ -d .git ]; then git reset --hard origin/main; fi": ["git_reset_hard"],
            "for b in a b; do git push -f origin $b; done": ["force_push"],
            "if true; then rm -rf /srv/app; fi": ["rm_recursive"],
            'while read d; do rm -rf "$d"; done < list': ["rm_recursive"],
            "case $x in a) rm -rf /srv;; esac": ["rm_recursive"],
            "timeout 60 git push --force origin main": ["force_push"],
            "timeout 60 rm -rf /srv/app": ["rm_recursive"],
            "stdbuf -oL git clean -fdx": ["git_clean_force"],
            "ionice -c3 dd if=/dev/zero of=/dev/sdb bs=1M": ["disk_overwrite"],
            "flock /tmp/deploy.lock git push --force origin main": ["force_push"],
            "flock /tmp/l -c 'rm -rf /srv/app'": ["rm_recursive"],
            "git submodule foreach --recursive git clean -fdx": ["git_clean_force"],
            "git submodule foreach 'git reset --hard'": ["git_reset_hard"],
            "su -c 'git reset --hard' deploy": ["git_reset_hard"],
            "su deploy -c 'rm -rf /srv/app'": ["rm_recursive"],
            "docker exec web git reset --hard": ["git_reset_hard"],
            "docker exec -it -u root web sh -c 'rm -rf /srv/app'": ["rm_recursive"],
            "kubectl exec pod -c app -- rm -rf /srv/app": ["rm_recursive"],
            "watch git reset --hard": ["git_reset_hard"],
            "watch -n 5 'git reset --hard'": ["git_reset_hard"],
            "ssh host 'git push -f origin main'": ["force_push"],
            "ssh -p 22 -i key host rm -rf /srv/app": ["rm_recursive"],
            "bash <<'EOF'\ngit reset --hard origin/main\nEOF": ["git_reset_hard"],
            "sh -s <<EOF\ngit push --force origin main\nEOF": ["force_push"],
            "cat <<EOF | sh\ngit reset --hard\nEOF": ["git_reset_hard"],
            "ssh host <<'EOF'\nrm -rf /srv/app\nEOF": ["rm_recursive"],
            "cat > notes.md <<'EOF'\nrm -rf /srv/app\nEOF": [],
            "psql <<EOF\nDROP TABLE users;\nEOF": ["drop_database"],
            "echo a#b; rm -rf /srv/app": ["rm_recursive"],
            "curl -s http://x/#top && git push --force origin main": ["force_push"],
            "true \\\n  && rm -rf /srv/app": ["rm_recursive"],
            'echo "a <<EOF b"\nrm -rf /srv/app': ["rm_recursive"],
            "echo '$(rm -rf /srv/app)'": [],
            'echo "price: $(date) rm -rf /srv"': [],
            "cat <(git reset --hard)": ["git_reset_hard"],
            'echo x > "$(rm -rf /srv/app)"': ["rm_recursive"],
            'cat <<< "$(git push -f)"': ["force_push"],
            "x=$(git reset --hard)": ["git_reset_hard"],
            "echo ${a:-$(rm -rf /srv)}": ["rm_recursive"],
            "find . \\( -name a -o -name b \\) -exec rm -rf {} \\;": ["rm_recursive"],
            "find . -name a -exec echo {} \\; -exec rm -rf {} +": ["rm_recursive"],
            "git clean -n -f": [],
            "git clean -fn": [],
            "git push --dry-run --force origin main": [],
            "git push -n -f origin main": [],
            "git rm -rf --cached build": [],
            "man shred": [],
            "which mkfs.ext4": [],
            "rm -rf ~nosuchuser-axiom/x": ["rm_recursive"],
            "rm -rf ~+/x": ["rm_recursive"],
            "rm -rf /srv/a\x00b": ["rm_recursive"],
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(
                    preflight.detect_patterns(command, environ={"TMPDIR": "/tmp"}),
                    expected,
                )

    def test_second_review_shapes(self) -> None:
        commit = (
            "git commit -m \"$(cat <<'EOF'\nDon't call git reset --hard here\n"
            "1) it's destructive\nEOF\n)\""
        )
        cases = {
            "out=$(python3 - <<'PY'\n# don't follow symlinks\nprint(\"b\")\nPY\n)\n"
            'rm -rf "$out"': ["rm_recursive"],
            commit: [],
            commit + "\nrm -rf /srv/app": ["rm_recursive"],
            "sudo -uroot rm -rf /srv/app": ["rm_recursive"],
            "find build -type d | xargs -d'\\n' rm -rf": ["rm_recursive"],
            "timeout -k5s 60 rm -rf /srv/app": ["rm_recursive"],
            "cat > deploy.sh <<'EOF'\ngit reset --hard origin/main\nEOF\n"
            "bash -n deploy.sh": [],
            "cat > notes.txt <<'EOF'\ngit reset --hard\nEOF\nsh build.sh": [],
            "cat <<EOF > x.sh; sh y.sh\ngit reset --hard\nEOF": [],
            "bash -n <<'EOF'\ngit reset --hard\nEOF": [],
            "bash -s -- arg <<'EOF'\ngit reset --hard\nEOF": ["git_reset_hard"],
            "docker exec -i web sh <<'EOF'\nrm -rf /srv/app\nEOF": ["rm_recursive"],
            "kubectl exec -i pod -- bash <<'EOF'\ngit push -f\nEOF": ["force_push"],
            "ssh host bash -s <<'EOF'\nrm -rf /srv/app\nEOF": ["rm_recursive"],
            "((rm -rf /srv/app) )": ["rm_recursive"],
            "n=$(( $(git clean -fdx | wc -l) + 1 ))": ["git_clean_force"],
            "git clean -fdx -enode_modules": ["git_clean_force"],
            "git clean -fdxn": [],
            "echo ${x:-{a}}; rm -rf /srv/app": ["rm_recursive"],
            # Over the lexer cap: the raw-text fallback reads the whole text.
            "echo " + "a" * 200_000 + "; rm -rf /srv/app": ["rm_recursive"],
            'ls\nrm -rf /srv/app "unbalanced': ["rm_recursive"],
        }
        for command, expected in cases.items():
            with self.subTest(command=command[:60]):
                self.assertEqual(
                    preflight.detect_patterns(command, environ={"TMPDIR": "/tmp"}),
                    expected,
                )
        started = time.monotonic()
        self.assertEqual(preflight.detect_patterns("echo " + "git " * 30000), [])
        self.assertLess(time.monotonic() - started, 10)

    def test_third_review_heredoc_and_expansion_cases(self) -> None:
        cases = {
            "bash -euo pipefail <<'EOF'\nrm -rf /srv/app\nEOF": ["rm_recursive"],
            "bash +e <<'EOF'\nrm -rf /srv/app\nEOF": ["rm_recursive"],
            "bash - <<'EOF'\nrm -rf /srv/x\nEOF": ["rm_recursive"],
            "ssh host bash -euo pipefail <<'EOF'\nrm -rf /srv/app\nEOF": [
                "rm_recursive"
            ],
            "docker exec -i web bash -eo pipefail <<'EOF'\nrm -rf /srv/x\nEOF": [
                "rm_recursive"
            ],
            "bash -- deploy.sh <<'EOF'\nrm -rf /srv/x\nEOF": [],
            "(\n  cat <<'EOF'\nrm -rf /srv/x\nEOF\n) | bash": ["rm_recursive"],
            "{ cat <<'EOF'; } | bash\nrm -rf /srv/x\nEOF": ["rm_recursive"],
            "cat <<'EOF' | (cd /srv && bash)\nrm -rf /srv/x\nEOF": ["rm_recursive"],
            "(cd /srv && bash) <<'EOF'\nrm -rf /srv/x\nEOF": ["rm_recursive"],
            # A script written by a here-document and run in the same command.
            "cat > x.sh <<'EOF'\nrm -rf /srv/x\nEOF\nsh x.sh": ["rm_recursive"],
            "cat > /tmp/f.sh <<'EOF'\ngit reset --hard\nEOF\nbash /tmp/f.sh": [
                "git_reset_hard"
            ],
            "cat <<'EOF' > ./run.sh\ngit push -f\nEOF\nchmod +x run.sh && ./run.sh": [
                "force_push"
            ],
            "tee deploy.sh <<'EOF'\nrm -rf /srv/x\nEOF\nsource deploy.sh": [
                "rm_recursive"
            ],
            "cat > d.sh <<'EOF'\ngit reset --hard\nEOF\nbash -n d.sh": [],
            "echo ${X:-$(find /srv -name c -exec rm -rf {} +)}": ["rm_recursive"],
            'echo "${out:-$(git push -f origin HEAD@{1}:main)}"': ["force_push"],
            "git commit -m \"$(cat <<'EOF'\nit's\nEOF)\"": [],
            "git commit -m \"$(cat <<'EOF'\nit's\nEOF)\"\nrm -rf /srv/app": [
                "rm_recursive"
            ],
            "for ((i=0;i<3;i++)); do rm -rf /srv/$i; done": ["rm_recursive"],
        }
        for command, expected in cases.items():
            with self.subTest(command=command[:60]):
                self.assertEqual(
                    preflight.detect_patterns(command, environ={}), expected
                )
        started = time.monotonic()
        # Over the lexer cap, the raw-text patterns are bounded per match.
        preflight.detect_patterns("x " + "dd " * 70000)
        preflight.detect_patterns("git push " + "a " * 110000)
        self.assertLess(time.monotonic() - started, 10)

    def test_enforce_asks_every_time_without_cooldown(self) -> None:
        # PreToolUse context reaches the agent next to the tool result, after
        # the command ran; only a permission decision acts before it runs.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            paths = common.state_paths(root=root, cwd=cwd)
            common.write_config(
                paths["config"],
                {"rules": {"preflight": {"mode": "enforce", "cooldown_minutes": 10}}},
            )
            payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "git reset --hard HEAD"},
                "cwd": str(cwd),
            }
            for _ in range(2):
                output = preflight.process(payload, root=root)["hookSpecificOutput"]
                self.assertEqual(output["permissionDecision"], "ask")
                self.assertNotIn("additionalContext", output)
                reason = output["permissionDecisionReason"]
                self.assertEqual(reason.count("?"), 3)
                self.assertIn("/axiom:enforce preflight off", reason)
            asked = [
                r for r in common.read_ledger(paths["ledger"]) if r["event"] == "asked"
            ]
            self.assertEqual([r["basis"] for r in asked], ["git_reset_hard"] * 2)
            self.assertEqual(len(asked[0]["finding_id"]), 16)
            self.assertFalse(paths["preflight"].exists())
            output = io.StringIO()
            with redirect_stdout(output):
                axiom_cli._render_report(common.get_report_data(paths["ledger"]))
            self.assertIn("asked: 2", output.getvalue())

    def test_observe_records_every_class_of_every_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf /srv/app && git push -f origin main"},
                "cwd": str(cwd),
            }
            for _ in range(2):
                self.assertIsNone(preflight.process(payload, root=root, environ={}))
            records = common.read_ledger(
                common.state_paths(root=root, cwd=cwd)["ledger"]
            )
            self.assertEqual(
                [(r["event"], r["basis"]) for r in records],
                [
                    ("would_have_blocked", "rm_recursive"),
                    ("would_have_blocked", "force_push"),
                ]
                * 2,
            )
            self.assertNotEqual(records[0]["finding_id"], records[1]["finding_id"])
            self.assertEqual(records[0]["finding_id"], records[2]["finding_id"])

    def test_commands_without_a_detection_create_no_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            self.assertIsNone(
                preflight.process(
                    {"tool_input": {"command": "ls -la"}, "cwd": str(cwd)}, root=root
                )
            )
            self.assertFalse(root.exists())


class HookRegistrationTests(unittest.TestCase):
    def test_hooks_json_has_exact_event_matchers(self) -> None:
        payload = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
        hooks = payload["hooks"]

        def commands(event: str, matcher: str | None = None) -> list[str]:
            entries = hooks[event]
            selected = [item for item in entries if item.get("matcher") == matcher]
            return [hook["command"] for item in selected for hook in item["hooks"]]

        self.assertEqual(len(hooks["Stop"]), 1)
        self.assertEqual(len(hooks["PreToolUse"]), 2)
        self.assertEqual(len(hooks["PostToolUse"]), 3)
        self.assertEqual(len(hooks["PostToolUseFailure"]), 1)
        # SessionStart wires BOTH the health check and axiom_common's
        # session_start_main — the only place goal files become registered
        # claims in the Claude Code lane. Dropping either must turn this red.
        self.assertEqual(
            [Path(item.split('"')[1]).name for item in commands("SessionStart")],
            ["health_check.py", "axiom_common.py"],
        )
        self.assertEqual(
            [Path(item.split('"')[1]).name for item in commands("Stop")],
            ["write_verify.py"],
        )
        self.assertEqual(
            [
                Path(item.split('"')[1]).name
                for item in commands("PostToolUse", "Write|Edit")
            ],
            ["write_verify.py"],
        )
        self.assertEqual(
            [
                Path(item.split('"')[1]).name
                for item in commands("PreToolUse", "Write|Edit")
            ],
            ["schema_guard.py"],
        )
        self.assertEqual(
            [
                Path(item.split('"')[1]).name
                for item in commands("PostToolUseFailure", "Bash")
            ],
            ["stuck_search.py"],
        )
        self.assertEqual(
            [Path(item.split('"')[1]).name for item in commands("PostToolUse", "Bash")],
            ["stuck_search.py"],
        )
        self.assertEqual(
            [Path(item.split('"')[1]).name for item in commands("PreToolUse", "Bash")],
            ["preflight.py", "schema_guard.py"],
        )
        search_commands = commands("PostToolUse", ".*")
        self.assertEqual(
            [Path(item.split('"')[1]).name for item in search_commands],
            ["stuck_search.py"],
        )
        self.assertIn("--search-event", search_commands[0])


class HealthCheckTests(unittest.TestCase):
    def test_fail_open_main_derives_the_ledger_from_the_payload_cwd(self) -> None:
        # The seam tests reach fail_open_main only with an unparsable payload
        # (cwd fallback). With a real payload the record must land in THAT
        # project's ledger, not the process's.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(root)}),
                redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = common.fail_open_main(
                    "preflight", "preflight", ValueError("x"), {"cwd": str(cwd)}
                )
            self.assertEqual(code, 0)
            lines = stdout.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            output = json.loads(lines[0])
            self.assertEqual(list(output), ["systemMessage"])
            self.assertIn(
                "preflight: fail-open (ValueError: x)", output["systemMessage"]
            )
            self.assertIn("fail-open", stderr.getvalue())
            records = common.read_ledger(paths["ledger"])
            self.assertEqual(
                [(r["event"], r["hook"], r["rule"], r["action"]) for r in records],
                [("error", "preflight", "preflight", "fail_open")],
            )

    def test_health_check_crash_fails_open_on_every_channel(self) -> None:
        # A health check that dies is the one health failure nobody hears
        # about unless it reports like every other hook: stderr, a ledger
        # `error{action: fail_open}`, and a systemMessage for the host.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            stdin = io.StringIO(
                json.dumps({"hook_event_name": "SessionStart", "cwd": str(cwd)})
            )
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                mock.patch.dict(os.environ, {"CLAUDE_PLUGIN_DATA": str(root)}),
                mock.patch.object(
                    health_check,
                    "health_issues",
                    side_effect=RuntimeError("probe exploded"),
                ),
                mock.patch.object(sys, "stdin", stdin),
                redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                code = health_check.main()
            self.assertEqual(code, 0)
            output = json.loads(stdout.getvalue())
            self.assertNotIn("hookSpecificOutput", output)
            self.assertIn("health_check: fail-open", output["systemMessage"])
            self.assertIn("probe exploded", stderr.getvalue())
            errors = [
                r for r in common.read_ledger(paths["ledger"]) if r["event"] == "error"
            ]
            self.assertEqual(len(errors), 1)
            self.assertEqual(errors[0]["hook"], "health_check")
            self.assertEqual(errors[0]["action"], "fail_open")

    def test_health_issues_accepts_executable_interpreter_and_writable_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "state"
            self.assertEqual(
                health_check.health_issues(root, interpreter=Path(sys.executable)),
                [],
            )

    def test_health_warning_is_fail_open_additional_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            invalid_root = Path(temporary) / "not-a-directory"
            invalid_root.write_text("occupied\n", encoding="utf-8")
            issues = health_check.health_issues(
                invalid_root,
                interpreter=Path(temporary) / "missing-python",
            )
            response = health_check.warning_response(issues)
            self.assertGreaterEqual(len(issues), 2)
            self.assertEqual(
                response["hookSpecificOutput"]["hookEventName"],
                "SessionStart",
            )
            context = response["hookSpecificOutput"]["additionalContext"]
            self.assertIn("AXIOM HEALTH WARNING", context)
            self.assertIn("fail-open", context)


class BatchARegressionTests(unittest.TestCase):
    """Regressions for the three correctness fixes surfaced by dual-track review."""

    def test_a1_compare_and_clear_spares_a_newer_foreign_claim(self) -> None:
        # A1 (TOCTOU): a Stop that evaluated claim A must not delete a claim B
        # that another session registered in the meantime.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            (cwd / "a.txt").write_text("a\n", encoding="utf-8")
            claim_a = common.register_claim_if_absent(
                {
                    "label": "A",
                    "predicates": [{"type": "file_exists", "path": "a.txt"}],
                },
                root=root,
                cwd=cwd,
            ).claim
            token_a = claim_a["claim_id"]
            self.assertTrue(
                common.clear_active_claim(root=root, cwd=cwd, expected_claim_id=token_a)
            )
            claim_b = common.register_claim_if_absent(
                {
                    "label": "B",
                    "predicates": [{"type": "file_exists", "path": "b.txt"}],
                },
                root=root,
                cwd=cwd,
            ).claim
            claim_b["registered_at"] = claim_a["registered_at"]
            common.write_json(
                common.state_paths(root=root, cwd=cwd)["active_claim"], claim_b
            )
            cleared = common.clear_active_claim(
                root=root, cwd=cwd, expected_claim_id=token_a
            )
            self.assertFalse(cleared)
            survivor = common.read_active_claim(root=root, cwd=cwd)
            self.assertIsNotNone(survivor)
            self.assertEqual(survivor["label"], "B")

    def test_a1_compare_and_clear_removes_its_own_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            (cwd / "a.txt").write_text("a\n", encoding="utf-8")
            claim = common.register_claim_if_absent(
                {
                    "label": "A",
                    "predicates": [{"type": "file_exists", "path": "a.txt"}],
                },
                root=root,
                cwd=cwd,
            ).claim
            cleared = common.clear_active_claim(
                root=root,
                cwd=cwd,
                expected_claim_id=claim["claim_id"],
            )
            self.assertTrue(cleared)
            self.assertIsNone(common.read_active_claim(root=root, cwd=cwd))

    def test_a2_runtime_reads_hyphen_config_key(self) -> None:
        # A2: config schema and runtime must agree on hyphenated rule keys.
        cfg = {"rules": {"write-verify": {"mode": "enforce"}}}
        self.assertEqual(common.rule_mode(cfg, write_verify.RULE), "enforce")
        self.assertEqual(
            common.rule_mode(
                {"rules": {"write_verify": {"mode": "enforce"}}}, write_verify.RULE
            ),
            "observe",
            "underscore key must NOT silently enable enforce",
        )

    def test_a3_malformed_predicate_fails_whole_claim(self) -> None:
        # A3: a malformed predicate is failed evidence; a passing sibling must
        # not rescue the claim.
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            cwd = Path(base) / "project"
            cwd.mkdir()
            (cwd / "a.txt").write_text("a\n", encoding="utf-8")
            result = write_verify.evaluate_claim(
                {
                    "label": "mixed",
                    "predicates": [
                        {"type": "file_exists", "path": "a.txt"},
                        "not-an-object",
                        {"type": "no_such_type"},
                    ],
                },
                cwd=cwd,
            )
            self.assertFalse(result["passed"])
            failed = [e for e in result["evidence"] if not e.get("passed")]
            self.assertGreaterEqual(len(failed), 2)


class DualTrackHighRegressionTests(unittest.TestCase):
    """Regressions for the two HIGH defects an independent audit found."""

    def test_high33_register_keeps_malformed_so_claim_fails(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            root, cwd = Path(t) / "s", Path(t) / "p"
            cwd.mkdir()
            (cwd / "a.txt").write_text("a\n", encoding="utf-8")
            common.register_claim_if_absent(
                {
                    "label": "m",
                    "predicates": [
                        {"type": "file_exists", "path": "a.txt"},
                        "not-a-mapping",
                    ],
                },
                root=root,
                cwd=cwd,
            )
            claim = common.read_active_claim(root=root, cwd=cwd)
            self.assertIn("not-a-mapping", claim["predicates"])
            self.assertFalse(write_verify.evaluate_claim(claim, cwd=cwd)["passed"])

    def test_high32_locked_compare_and_clear_spares_foreign_claim(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            root, cwd = Path(t) / "s", Path(t) / "p"
            cwd.mkdir()
            (cwd / "a.txt").write_text("a\n", encoding="utf-8")
            a = common.register_claim_if_absent(
                {
                    "label": "A",
                    "predicates": [{"type": "file_exists", "path": "a.txt"}],
                },
                root=root,
                cwd=cwd,
            ).claim
            self.assertTrue(
                common.clear_active_claim(
                    root=root, cwd=cwd, expected_claim_id=a["claim_id"]
                )
            )
            common.register_claim_if_absent(
                {
                    "label": "B",
                    "predicates": [{"type": "file_exists", "path": "b.txt"}],
                },
                root=root,
                cwd=cwd,
            )
            cleared = common.clear_active_claim(
                root=root,
                cwd=cwd,
                expected_claim_id=a["claim_id"],
            )
            self.assertFalse(cleared)
            self.assertEqual(common.read_active_claim(root=root, cwd=cwd)["label"], "B")

    def test_legacy_format_claim_is_safely_cleared_end_to_end_through_process_stop(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, cwd = Path(temporary) / "state", Path(temporary) / "project"
            cwd.mkdir()
            (cwd / "done.txt").write_text("done\n", encoding="utf-8")
            paths = common.ensure_layout(root=root, cwd=cwd)
            legacy_claim = {
                "label": "legacy",
                "predicates": [{"type": "file_exists", "path": "done.txt"}],
                "baseline": {
                    "git_head": None,
                    "registered_at": "2026-01-01T00:00:00+00:00",
                    "files": {},
                },
            }
            common.write_json(paths["active_claim"], legacy_claim)

            self.assertIsNone(write_verify.process_stop({"cwd": str(cwd)}, root=root))

            self.assertIsNone(common.read_active_claim(root=root, cwd=cwd))
            record = common.read_ledger(paths["ledger"])[-1]
            self.assertEqual(record["event"], "verified")
            self.assertTrue(record["cleared"])
            # No claim_id: the legacy `registered_at` token names the finding
            # (CONTRACTS §6), through the real Stop path, not only the helper.
            self.assertEqual(
                record["finding_id"],
                common.finding_id("write-verify", "2026-01-01T00:00:00+00:00"),
            )

    def test_legacy_claim_mismatch_is_never_auto_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, cwd = Path(temporary) / "state", Path(temporary) / "project"
            cwd.mkdir()
            paths = common.ensure_layout(root=root, cwd=cwd)
            legacy_claim = {
                "label": "legacy",
                "predicates": [{"type": "file_exists", "path": "done.txt"}],
                "baseline": {
                    "registered_at": "2026-01-01T00:00:00+00:00",
                    "files": {},
                },
            }
            common.write_json(paths["active_claim"], legacy_claim)

            cleared = common.clear_active_claim(
                root=root,
                cwd=cwd,
                expected_claim_id="foreign-claim-id",
                expected_legacy_registered_at="2025-01-01T00:00:00+00:00",
            )

            self.assertFalse(cleared)
            self.assertEqual(common.read_active_claim(root=root, cwd=cwd), legacy_claim)


if __name__ == "__main__":
    unittest.main(verbosity=2)
