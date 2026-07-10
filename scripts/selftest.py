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
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = REPO_ROOT / "hooks"
sys.path.insert(0, str(HOOKS_DIR))

import axiom_common as common  # noqa: E402
import health_check  # noqa: E402


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
                },
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
