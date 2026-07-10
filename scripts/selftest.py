#!/usr/bin/env python3
"""Self-tests for the Axiom shared state library."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
import unittest
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


def _append_worker(ledger_path: str, index: int) -> None:
    common.append_ledger(Path(ledger_path), {"index": index})


class AxiomCommonTests(unittest.TestCase):
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
                multiprocessing.Process(target=_append_worker, args=(str(ledger), index))
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
            self.assertEqual(common.read_config(config_path)["revision"], 50)
            self.assertEqual(list(config_path.parent.glob(f".{config_path.name}.*.tmp")), [])

    def test_payload_parser_tolerates_missing_fields(self) -> None:
        self.assertEqual(
            common.parse_payload({"hook_event_name": "PostToolUseFailure", "error": "bad"}),
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

    def test_observe_mode_records_would_have_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "ledger.jsonl"
            self.assertEqual(common.rule_mode({}, "write_verify"), "observe")
            self.assertEqual(
                common.rule_mode({"rules": {"write_verify": {"mode": "enforce"}}}, "write_verify"),
                "enforce",
            )
            common.record_would_have_blocked(
                ledger,
                rule="write_verify",
                basis="missing verification",
                summary="write completed without read-back",
            )
            record = json.loads(ledger.read_text(encoding="utf-8"))
            self.assertEqual(record["event"], "would_have_blocked")
            self.assertEqual(record["rule"], "write_verify")

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
                    str(project / "ledger.jsonl"),
                    str(project / "config.json"),
                    str(project / "lessons.md"),
                    str(project / "claims" / "active.json"),
                    str(project / "stuck-search.json"),
                    str(project / "preflight.json"),
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
            claim = common.register_claim(
                {
                    "label": "change result",
                    "predicates": [{"type": "file_changed", "path": "result.txt"}],
                },
                root=root,
                cwd=cwd,
            )
            loaded = common.read_active_claim(root=root, cwd=cwd)
            self.assertEqual(loaded, claim)
            self.assertTrue(claim["baseline"]["files"]["result.txt"]["exists"])
            self.assertRegex(claim["baseline"]["files"]["result.txt"]["sha256"], r"^[0-9a-f]{64}$")

    def test_report_contract_and_calibration_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            ledger = Path(temporary) / "ledger.jsonl"
            start = datetime.now(timezone.utc) - timedelta(days=7)
            common.append_ledger(
                ledger,
                {"event": "heartbeat", "hook": "session_start", "timestamp": start.isoformat()},
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
            claim = common.register_goal_claim(root=root, cwd=cwd)
            self.assertEqual(claim["label"], "release")
            self.assertEqual(
                common.read_active_claim(root=root, cwd=cwd)["predicates"][0]["type"],
                "file_exists",
            )


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
            claim = common.register_claim(
                {
                    "label": "stale file",
                    "predicates": [
                        {"type": "file_exists", "path": "artifact.txt"},
                        {"type": "file_changed", "path": "artifact.txt"},
                    ],
                },
                root=root,
                cwd=cwd,
            )
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
            claim = common.register_claim(
                {
                    "label": "target-only",
                    "predicates": [{"type": "file_changed", "path": "target.txt"}],
                },
                root=root,
                cwd=cwd,
            )
            (cwd / "other.txt").write_text("dirty\n", encoding="utf-8")
            result = write_verify.evaluate_claim(claim, cwd=cwd)
            self.assertFalse(result["passed"])
            self.assertEqual(result["evidence"][0]["actual"], "unchanged")
            print("A4-2 unrelated dirty diff: observed FAIL (declared target unchanged)")

    def test_a4_3_tests_not_run_is_decided_by_fresh_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary)
            command = subprocess.list2cmdline([sys.executable, "-c", "raise SystemExit(7)"])
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
            common.register_claim(
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
            print("A4-4 old log: observed FAIL (missing file blocked despite old success text)")

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
            common.register_claim(
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
            records = common.read_ledger(common.state_paths(root=root, cwd=linked)["ledger"])
            self.assertEqual(records[-1]["event"], "unverified_completion")
            print("A4-5 wrong worktree: observed FAIL/no-claim (foreign claim untouched)")


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
            record = common.read_ledger(common.state_paths(root=root, cwd=cwd)["ledger"])[-1]
            self.assertEqual(record["event"], "write_readback")
            self.assertTrue(record["verified"])
            self.assertGreater(record["stat"]["size"], 0)

    def test_reentrant_second_failure_escalates_and_allows_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            common.register_claim(
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
            records = common.read_ledger(common.state_paths(root=root, cwd=cwd)["ledger"])
            self.assertEqual(records[-1]["event"], "escalation")

    def test_successful_predicates_clear_active_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            target = cwd / "output.txt"
            target.write_text("before\n", encoding="utf-8")
            common.register_claim(
                {
                    "label": "complete output",
                    "predicates": [
                        {"type": "file_exists", "path": "output.txt"},
                        {"type": "file_contains", "path": "output.txt", "pattern": "aft.r"},
                        {"type": "file_changed", "path": "output.txt"},
                    ],
                },
                root=root,
                cwd=cwd,
            )
            target.write_text("after\n", encoding="utf-8")
            self.assertIsNone(write_verify.process_stop({"cwd": str(cwd)}, root=root))
            self.assertIsNone(common.read_active_claim(root=root, cwd=cwd))
            record = common.read_ledger(common.state_paths(root=root, cwd=cwd)["ledger"])[-1]
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
            with mock.patch.object(common, "read_active_claim", side_effect=RuntimeError("boom")):
                response = write_verify.process_stop({"cwd": str(cwd)}, root=root)
            self.assertIsNone(response)
            record = common.read_ledger(common.state_paths(root=root, cwd=cwd)["ledger"])[-1]
            self.assertEqual(record["event"], "error")
            self.assertEqual(record["action"], "fail_open")


class SchemaGuardTests(unittest.TestCase):
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
            record = common.read_ledger(common.state_paths(root=root, cwd=cwd)["ledger"])[-1]
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
            self.assertIn("/axiom:enforce off schema-guard", output["permissionDecisionReason"])


class StuckSearchTests(unittest.TestCase):
    def _payload(self, cwd: Path, event: str, command: str, error: str = "") -> dict[str, object]:
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
                    self._payload(cwd, event, "npm test unit", "failed" if "Failure" in event else ""),
                    root=root,
                )
                self.assertIsNone(response)
            records = common.read_ledger(common.state_paths(root=root, cwd=cwd)["ledger"])
            self.assertFalse(any(item.get("event") == "would_have_blocked" for item in records))

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
                    self._payload(cwd, "PostToolUseFailure", command, "same root cause"),
                    root=root,
                )
                for command in commands
            ]
            self.assertIsNone(responses[0])
            self.assertIsNone(responses[1])
            self.assertIn("additionalContext", responses[2]["hookSpecificOutput"])
            state = common.read_json(common.state_paths(root=root, cwd=cwd)["stuck_search"])
            self.assertEqual(state["clusters"][0]["count"], 3)

    def test_success_after_failures_clears_matching_cluster(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            for command in ("npm test alpha", "npm test beta"):
                stuck_search.process(
                    self._payload(cwd, "PostToolUseFailure", command, "failure"), root=root
                )
            stuck_search.process(
                self._payload(cwd, "PostToolUse", "npm test gamma"), root=root
            )
            state = common.read_json(common.state_paths(root=root, cwd=cwd)["stuck_search"])
            self.assertEqual(state["clusters"], [])


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
        self.assertIsNone(preflight.detect_pattern("rm -rf /tmp/build-cache"))

    def test_enforce_injects_three_questions_and_cooldown_suppresses_repeat(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, cwd = base / "state", base / "project"
            cwd.mkdir()
            common.write_config(
                common.state_paths(root=root, cwd=cwd)["config"],
                {"rules": {"preflight": {"mode": "enforce"}}},
            )
            payload = {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "git reset --hard HEAD"},
                "cwd": str(cwd),
            }
            first = preflight.process(payload, root=root)
            second = preflight.process(payload, root=root)
            context = first["hookSpecificOutput"]["additionalContext"]
            self.assertEqual(context.count("?"), 3)
            self.assertIn("/axiom:enforce off preflight", context)
            self.assertIsNone(second)


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
        self.assertEqual(len(hooks["PostToolUse"]), 2)
        self.assertEqual(len(hooks["PostToolUseFailure"]), 1)
        self.assertEqual(
            [Path(item.split('"')[1]).name for item in commands("Stop")],
            ["write_verify.py"],
        )
        self.assertEqual(
            [Path(item.split('"')[1]).name for item in commands("PostToolUse", "Write|Edit")],
            ["write_verify.py"],
        )
        self.assertEqual(
            [Path(item.split('"')[1]).name for item in commands("PreToolUse", "Write|Edit")],
            ["schema_guard.py"],
        )
        self.assertEqual(
            [Path(item.split('"')[1]).name for item in commands("PostToolUseFailure", "Bash")],
            ["stuck_search.py"],
        )
        self.assertEqual(
            [Path(item.split('"')[1]).name for item in commands("PostToolUse", "Bash")],
            ["stuck_search.py"],
        )
        self.assertEqual(
            [Path(item.split('"')[1]).name for item in commands("PreToolUse", "Bash")],
            ["preflight.py"],
        )


class HealthCheckTests(unittest.TestCase):
    def test_health_issues_accepts_executable_interpreter_and_writable_root(self) -> None:
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
            claim_a = common.register_claim(
                {"label": "A", "predicates": [{"type": "file_exists", "path": "a.txt"}]},
                root=root,
                cwd=cwd,
            )
            token_a = claim_a["baseline"]["registered_at"]
            # Second session overwrites the active claim with B.
            common.register_claim(
                {"label": "B", "predicates": [{"type": "file_exists", "path": "b.txt"}]},
                root=root,
                cwd=cwd,
            )
            cleared = common.clear_active_claim(
                root=root, cwd=cwd, expected_registered_at=token_a
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
            claim = common.register_claim(
                {"label": "A", "predicates": [{"type": "file_exists", "path": "a.txt"}]},
                root=root,
                cwd=cwd,
            )
            cleared = common.clear_active_claim(
                root=root, cwd=cwd,
                expected_registered_at=claim["baseline"]["registered_at"],
            )
            self.assertTrue(cleared)
            self.assertIsNone(common.read_active_claim(root=root, cwd=cwd))

    def test_a2_runtime_reads_hyphen_config_key(self) -> None:
        # A2: config schema and runtime must agree on hyphenated rule keys.
        cfg = {"rules": {"write-verify": {"mode": "enforce"}}}
        self.assertEqual(common.rule_mode(cfg, write_verify.RULE), "enforce")
        self.assertEqual(
            common.rule_mode({"rules": {"write_verify": {"mode": "enforce"}}}, write_verify.RULE),
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
            common.register_claim(
                {"label": "m", "predicates": [
                    {"type": "file_exists", "path": "a.txt"}, "not-a-mapping"]},
                root=root, cwd=cwd)
            claim = common.read_active_claim(root=root, cwd=cwd)
            self.assertIn("not-a-mapping", claim["predicates"])
            self.assertFalse(write_verify.evaluate_claim(claim, cwd=cwd)["passed"])

    def test_high32_locked_compare_and_clear_spares_foreign_claim(self) -> None:
        with tempfile.TemporaryDirectory() as t:
            root, cwd = Path(t) / "s", Path(t) / "p"
            cwd.mkdir()
            (cwd / "a.txt").write_text("a\n", encoding="utf-8")
            a = common.register_claim(
                {"label": "A", "predicates": [{"type": "file_exists", "path": "a.txt"}]},
                root=root, cwd=cwd)
            common.register_claim(
                {"label": "B", "predicates": [{"type": "file_exists", "path": "b.txt"}]},
                root=root, cwd=cwd)
            cleared = common.clear_active_claim(
                root=root, cwd=cwd,
                expected_registered_at=a["baseline"]["registered_at"])
            self.assertFalse(cleared)
            self.assertEqual(common.read_active_claim(root=root, cwd=cwd)["label"], "B")


if __name__ == "__main__":
    unittest.main(verbosity=2)
